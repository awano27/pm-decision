import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kimeru import events, pull
from kimeru.backends import StubBackend
from kimeru.cli import process
from kimeru import graph

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)

WI = {"id": 4812, "fields": {"System.WorkItemType": "Bug", "System.Title": "本番で全ユーザーがログインできない",
                             "System.CreatedBy": {"displayName": "Tanaka"}, "System.CreatedDate": "2026-09-25T00:30:00Z",
                             "Microsoft.VSTS.TCM.ReproSteps": "<div>再現: ログイン → 500</div>"}}
ALERT = {"id": "/subscriptions/s1/providers/Microsoft.AlertsManagement/alerts/abc-123", "name": "checkout-api 5xx rate",
         "properties": {"essentials": {"severity": "Sev1", "monitorCondition": "Fired", "alertState": "New",
                                       "startDateTime": "2026-09-25T00:50:00Z", "alertRule": "checkout-api 5xx rate",
                                       "targetResource": "/subscriptions/s1/resourceGroups/prod/providers/Microsoft.Web/sites/checkout-api",
                                       "description": "5xx 23%; customers cannot check out (service down)"}}}


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, token, body=None):
        self.calls.append((method, url, body))
        for key, resp in self.routes:
            if key in url:
                return resp(url, body) if callable(resp) else resp
        raise AssertionError("unexpected url " + url)


class TestAdo(unittest.TestCase):
    def test_new_items_dropped_once_and_since_advances(self):
        http = FakeHttp([("/wiql", {"workItems": [{"id": 4812}]}), ("/workitems?ids=4812", {"value": [WI]})])
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 1)
            self.assertIn("2026-09-24T01:00:00Z", http.calls[0][2]["query"])  # first run looks back 24h
            self.assertIn("timePrecision=true", http.calls[0][1])
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 0)  # seen
            self.assertIn("2026-09-25T01:00:00Z", http.calls[2][2]["query"])  # since advanced
            files = list(inbox.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertFalse(list(inbox.glob("*.tmp")))
            ev = events.normalize(json.loads(files[0].read_text(encoding="utf-8")))[0]
            self.assertEqual((ev["kind"], ev["id"], ev["created_by"]), ("ado.workitem.created", "4812", "Tanaka"))

    def test_batches_of_200(self):
        ids = list(range(1, 451))
        http = FakeHttp([("/wiql", {"workItems": [{"id": i} for i in ids]}),
                         ("/workitems?ids=", lambda url, b: {"value": [{"id": int(x), "fields": {}} for x in
                                                                       url.split("ids=")[1].split("&")[0].split(",")]})])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pull.pull_ado("o", "p", Path(d) / "i", Path(d) / "o", http=http, token="t", now=NOW), 450)
            self.assertEqual(sum(1 for c in http.calls if "/workitems?" in c[1]), 3)


class TestFindAz(unittest.TestCase):
    def test_order_env_then_path_then_zip(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            env_az, zip_az = Path(d) / "env" / "az.cmd", Path(d) / "zip" / "az.cmd"
            for p in (env_az, zip_az):
                p.parent.mkdir()
                p.write_text("@echo off", encoding="ascii")
            with mock.patch.object(pull, "AZ_FALLBACKS", (str(zip_az),)), mock.patch("shutil.which", return_value=None):
                with mock.patch.dict(os.environ, {"KIMERU_AZ": str(env_az)}):
                    self.assertEqual(pull.find_az(), str(env_az))
                with mock.patch.dict(os.environ, {"KIMERU_AZ": ""}):
                    self.assertEqual(pull.find_az(), str(zip_az))          # no PATH az: the ZIP location
                with mock.patch.dict(os.environ, {"KIMERU_AZ": str(Path(d) / "missing.cmd")}):
                    self.assertEqual(pull.find_az(), str(zip_az))          # a stale KIMERU_AZ is skipped
            with mock.patch.object(pull, "AZ_FALLBACKS", ()), mock.patch("shutil.which", return_value=None), \
                    mock.patch.dict(os.environ, {"KIMERU_AZ": ""}):
                self.assertIsNone(pull.find_az())
                with self.assertRaises(RuntimeError):
                    pull.az_token(pull.ADO_RESOURCE)


class TestFailures(unittest.TestCase):
    def test_failed_later_chunk_does_not_redeliver_earlier(self):
        calls = {"n": 0}

        def items(url, body):
            calls["n"] += 1
            if calls["n"] == 2:
                raise pull.PullError("HTTP 503")
            return {"value": [{"id": int(x), "fields": {}} for x in url.split("ids=")[1].split("&")[0].split(",")]}
        http = FakeHttp([("/wiql", {"workItems": [{"id": i} for i in range(1, 301)]}), ("/workitems?ids=", items)])
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "i", Path(d) / "o"
            with self.assertRaises(pull.PullError):
                pull.pull_ado("o", "p", inbox, out, http=http, token="t", now=NOW)
            self.assertEqual(len(list(inbox.glob("*.json"))), 200)
            st = json.loads((out / "pull_state.json").read_text(encoding="utf-8"))["ado:o/p"]
            self.assertIsNone(st["since"])            # since not advanced on failure
            self.assertEqual(len(st["seen"]), 200)     # but delivered ids remembered
            http2 = FakeHttp([("/wiql", {"workItems": [{"id": i} for i in range(1, 301)]}),
                              ("/workitems?ids=", lambda url, b: {"value": [{"id": int(x), "fields": {}} for x in
                                                                            url.split("ids=")[1].split("&")[0].split(",")]})])
            self.assertEqual(pull.pull_ado("o", "p", inbox, out, http=http2, token="t", now=NOW), 100)

    def test_http_error_mapping(self):
        import io
        import urllib.error
        from unittest import mock
        err = urllib.error.HTTPError("https://x/a?b=1", 401, "unauth", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(pull.PullError) as cm:
                pull.http_json("GET", "https://x/a?secret=1", "t")
        self.assertIn("az login", str(cm.exception))
        self.assertNotIn("secret", str(cm.exception))


def chat(cid, kind, preview, time="10:00", mention=False, title="Sato"):
    return {"id": cid, "kind": kind, "title": title, "preview": preview, "time": time, "unread": True, "mention": mention}


class TestTeams(unittest.TestCase):
    ONE = "19:aaa_bbb@unq.gbl.spaces"
    GRP = "19:ccc@thread.v2"

    def test_first_poll_is_baseline_then_diff(self):
        sec = {}
        base = [chat(self.ONE, "oneOnOne", "リリースの件どうしますか"), chat("48:notes", "self", "[kimeru #1] x")]
        self.assertEqual(pull.teams_events(base, sec), [])
        self.assertEqual(pull.teams_events(base, sec), [])                     # unchanged
        evs = pull.teams_events([chat(self.ONE, "oneOnOne", "至急判断お願いします", "10:05")], sec)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual((ev["kind"], ev["chat_kind"], ev["author"], ev["text"]), ("teams.chat", "oneOnOne", "Sato", "至急判断お願いします"))
        self.assertEqual(events.normalize(ev)[0], ev)                          # passes straight into the graphs

    def test_filters(self):
        sec = {"sigs": {"x": "y"}}  # not first poll
        evs = pull.teams_events([
            chat(self.GRP, "group", "雑談です"),                                # group without mention: skip
            chat(self.GRP + "2", "group", "@粟野 確認お願いします", mention=True),   # mention: keep
            chat(self.ONE, "oneOnOne", "あなた: 了解です"),                     # my own last message: skip
            chat("48:notes", "self", "OK 3"),                                  # self chat: skip
            chat("19:meeting_x@thread.v2", "meeting", "議事録を共有しました"),    # meeting without mention: skip
        ], sec)
        self.assertEqual([e["chat_kind"] for e in evs], ["group"])
        self.assertTrue(evs[0]["mentions_me"])

    def test_include_existing_and_inbox_drop(self):
        class B:
            def chats(self):
                return [chat(TestTeams.ONE, "oneOnOne", "見積もりの承認をお願いします")]
        with tempfile.TemporaryDirectory() as d:
            n = pull.pull_teams(Path(d) / "inbox", Path(d) / "out", bridge=B(), include_existing=True)
            self.assertEqual(n, 1)
            f = list((Path(d) / "inbox").glob("*.json"))
            self.assertEqual(len(f), 1)
            self.assertNotIn(":", f[0].name)
            self.assertEqual(pull.pull_teams(Path(d) / "inbox", Path(d) / "out", bridge=B()), 0)


class TestAlerts(unittest.TestCase):
    def test_rule_arm_id_becomes_readable_name(self):
        live_shape = json.loads(json.dumps(ALERT))
        live_shape.pop("name")
        live_shape["properties"]["essentials"]["alertRule"] = (
            "/subscriptions/s1/resourceGroups/rg/providers/microsoft.alertsmanagement/smartdetectoralertrules/Failure Anomalies - app1")
        ev = events.normalize(pull.alert_payload(live_shape))[0]
        self.assertEqual(ev["rule"], "Failure Anomalies - app1")

    def test_resolved_is_decided_by_rule_without_a_model(self):
        g = graph.load_dir(ROOT / "graphs")["monitor.alert"][0]
        ev = {"kind": "monitor.alert", "id": "r", "rule": "x", "severity": "Sev3", "condition": "Resolved", "description": "back to normal"}
        from kimeru.backends import ReplayBackend
        r = graph.run(g, ev, ReplayBackend({}))       # would raise if any model question were asked
        self.assertEqual(r["node"], "log_resolved")

    def test_payload_maps_to_common_schema(self):
        ev = events.normalize(pull.alert_payload(ALERT))[0]
        self.assertEqual(ev["kind"], "monitor.alert")
        self.assertEqual((ev["rule"], ev["severity"], ev["condition"]), ("checkout-api 5xx rate", "Sev1", "Fired"))
        self.assertTrue(ev["resources"][0].endswith("checkout-api"))

    def test_paging_dedup_and_closed_skipped(self):
        closed = json.loads(json.dumps(ALERT))
        closed["id"] += "-closed"
        closed["properties"]["essentials"]["alertState"] = "Closed"
        http = FakeHttp([("page2", {"value": [closed]}),
                         ("/alerts?", {"value": [ALERT], "nextLink": "https://management.azure.com/x/alerts/page2"})])
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pull.pull_alerts("s1", Path(d) / "i", Path(d) / "o", http=http, token="t"), 1)
            self.assertEqual(pull.pull_alerts("s1", Path(d) / "i", Path(d) / "o", http=http, token="t"), 0)

    def test_pulled_alert_runs_through_graph(self):
        pbs_graphs = graph.load_dir(ROOT / "graphs")
        with tempfile.TemporaryDirectory() as d:
            r = process(pull.alert_payload(ALERT), pbs_graphs, StubBackend(), Path(d))
            self.assertEqual(r[0]["graph"], "alert-triage")
            self.assertIn(r[0]["node"], ("page_planned", "page"))


if __name__ == "__main__":
    unittest.main()

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


class TestAlerts(unittest.TestCase):
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

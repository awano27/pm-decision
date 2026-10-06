"""Regressions for trustworthy intake and Teams delivery evidence."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from kimeru import notify, pull


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)
REC = {
    "graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "4812",
    "node": "triage_pm", "outcome": "advise", "needs_human": True,
    "advice": "優先度を判定できないチケット #4812", "actions": [],
}
CONFIRMED = {"ok": True, "typed": True, "sent": True,
             "readback": {"matched": True, "message_id": "uia:42-7"}}


class DeliveryBridge:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def post(self, text, send):
        self.calls.append((text, send))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def write_queue(out):
    (Path(out) / "queue.jsonl").write_text(json.dumps(REC, ensure_ascii=False) + "\n", encoding="utf-8")


class TestDeliveryEvidence(unittest.TestCase):
    def test_only_typed_sent_and_matched_readback_marks_the_item_posted(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d)
            bridge = DeliveryBridge(CONFIRMED)
            self.assertEqual(notify.notify(d, bridge, send=True), [1])
            item = notify.Approvals(d).data["items"]["1"]
            self.assertTrue(item["posted"])
            self.assertNotIn("delivery_unknown", item)

    def test_unknown_delivery_is_held_for_recovery_without_an_automatic_resend(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d)
            bridge = DeliveryBridge({"ok": True, "typed": True, "sent": True})
            with self.assertRaises(RuntimeError):
                notify.notify(d, bridge, send=True)
            item = notify.Approvals(d).data["items"]["1"]
            self.assertFalse(item["posted"])
            self.assertTrue(item["delivery_unknown"])
            self.assertEqual(notify.notify(d, bridge, send=True), [])
            self.assertEqual(len(bridge.calls), 1)

    def test_a_confirmed_not_sent_result_remains_retryable(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d)
            class RetryBridge:
                def __init__(self):
                    self.calls = 0

                def post(self, text, send):
                    self.calls += 1
                    return ({"ok": False, "typed": False, "sent": False}
                            if self.calls == 1 else CONFIRMED)

            bridge = RetryBridge()
            with self.assertRaises(RuntimeError):
                notify.notify(d, bridge, send=True)
            item = notify.Approvals(d).data["items"]["1"]
            self.assertFalse(item["posted"])
            self.assertFalse(item.get("delivery_unknown", False))
            self.assertEqual(notify.notify(d, bridge, send=True), [1])
            self.assertEqual(bridge.calls, 2)
            self.assertTrue(notify.Approvals(d).data["items"]["1"]["posted"])

    def test_inconsistent_typed_false_sent_true_evidence_is_unknown(self):
        self.assertTrue(notify._delivery_unknown({"ok": False, "typed": False, "sent": True}, True))

    def test_powershell_failure_contract_distinguishes_pre_send_and_attempted(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        for state, expected in (("pre-send", {"typed": False, "sent": False}),
                                ("attempted", {"typed": False, "sent": None}),
                                ("sent", {"typed": True, "sent": True})):
            with self.subTest(state=state):
                result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                         "-Action", "test-failure", "-TestDeliveryState", state],
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                data = json.loads(result.stdout.lstrip("\ufeff"))
                self.assertEqual({key: data[key] for key in expected}, expected)

    def test_delivery_recovery_requires_explicit_unsent_confirmation_and_only_releases_the_hold(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d)
            bridge = DeliveryBridge({"ok": True, "typed": True, "sent": True})
            with self.assertRaises(RuntimeError):
                notify.notify(d, bridge, send=True)
            pending = notify.delivery_pending(d)
            self.assertEqual([(r["kind"], r["target"], r["part"]) for r in pending], [("case", "1", "post")])
            self.assertEqual(notify.retry_delivery(d, "case", "1"), {"status": "confirmation_required"})
            self.assertTrue(notify.Approvals(d).data["items"]["1"]["delivery_unknown"])
            self.assertEqual(notify.retry_delivery(d, "case", "1", confirm_not_sent=True)["status"], "released")
            item = notify.Approvals(d).data["items"]["1"]
            self.assertFalse(item["posted"])
            self.assertFalse(item.get("delivery_unknown", False))
            log = (Path(d) / "delivery-recovery.log.jsonl").read_text(encoding="utf-8")
            self.assertIn('"target": "1"', log)
            self.assertNotIn(REC["advice"], log)

    def test_delivery_recovery_lists_and_releases_notice_and_outbox_holds(self):
        with tempfile.TemporaryDirectory() as d:
            ap = notify.Approvals(d)
            ap.data.update({"notice_delivery_unknown": {"notice-key": "2026-10-03T00:00:00+00:00"},
                            "outbox_delivery_unknown": True,
                            "outbox_delivery_unknown_at": "2026-10-03T00:01:00+00:00",
                            "outbox": ["private result body"]})
            ap.save()
            pending = notify.delivery_pending(d)
            self.assertEqual({(r["kind"], r["target"]) for r in pending},
                             {("notice", "notice-key"), ("outbox", "outbox")})
            self.assertEqual(notify.retry_delivery(d, "notice", "notice-key")["status"], "confirmation_required")
            self.assertEqual(notify.retry_delivery(d, "outbox", "outbox")["status"], "confirmation_required")
            self.assertEqual(notify.retry_delivery(d, "notice", "notice-key", confirm_not_sent=True)["status"], "released")
            self.assertEqual(notify.retry_delivery(d, "outbox", "outbox", confirm_not_sent=True)["status"], "released")
            after = notify.Approvals(d).data
            self.assertEqual(after["outbox"], ["private result body"])
            self.assertNotIn("notice-key", after["notice_delivery_unknown"])
            self.assertFalse(after.get("outbox_delivery_unknown", False))
            log = (Path(d) / "delivery-recovery.log.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("private result body", log)


class TestAdoWatermark(unittest.TestCase):
    def test_more_than_2000_ids_inside_the_overlap_remain_deduped(self):
        count = 2001

        class Http:
            def __init__(self):
                self.get_batches = []

            def __call__(self, method, url, token, body=None):
                if "/wiql" in url:
                    return {"asOf": "2026-10-03T00:59:00Z",
                            "workItems": [{"id": i} for i in range(1, count + 1)]}
                ids = [int(value) for value in url.split("ids=", 1)[1].split("&", 1)[0].split(",")]
                self.get_batches.append(ids)
                return {"value": [{"id": i, "fields": {"System.CreatedDate": "2026-10-03T00:58:30Z"}}
                                  for i in ids]}

        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            http = Http()
            dropped = []
            with mock.patch.object(pull, "_drop", side_effect=lambda *_args: dropped.append(True)):
                self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), count)
                self.assertEqual(len(dropped), count)
                state = json.loads((out / "pull_state.json").read_text(encoding="utf-8"))["ado:org/proj"]
                self.assertEqual(len(state["seen"]), count)
                self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 0)
                self.assertEqual(len(dropped), count)
                after = json.loads((out / "pull_state.json").read_text(encoding="utf-8"))["ado:org/proj"]
                self.assertEqual(len(after["seen"]), count)
                self.assertEqual(sum(len(batch) for batch in http.get_batches), count)

    def test_uses_server_asof_minus_a_bounded_overlap_and_dedupes_the_overlap(self):
        class Http:
            def __init__(self):
                self.queries = []

            def __call__(self, method, url, token, body=None):
                if "/wiql" in url:
                    self.queries.append(body["query"])
                    return {"asOf": "2026-10-03T00:59:00Z", "workItems": [{"id": 9}]}
                return {"value": [{"id": 9, "fields": {"System.CreatedDate": "2026-10-03T00:58:00Z"}}]}

        with tempfile.TemporaryDirectory() as d:
            http = Http()
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 1)
            state = json.loads((out / "pull_state.json").read_text(encoding="utf-8"))["ado:org/proj"]
            since = datetime.fromisoformat(state["since"].replace("Z", "+00:00"))
            asof = datetime(2026, 10, 3, 0, 59, tzinfo=timezone.utc)
            self.assertLessEqual((asof - since).total_seconds(), 15 * 60)
            self.assertGreater((asof - since).total_seconds(), 0)
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 0)
            self.assertIn(state["since"], http.queries[1])

    def test_missing_or_invalid_server_asof_does_not_advance_the_watermark_to_client_now(self):
        class Http:
            def __init__(self, asof):
                self.asof = asof

            def __call__(self, method, url, token, body=None):
                if "/wiql" in url:
                    return {**({"asOf": self.asof} if self.asof is not None else {}), "workItems": []}
                return {"value": []}

        for asof in (None, "not-a-time", "2026-10-03T02:00:00Z"):
            with self.subTest(asof=asof), tempfile.TemporaryDirectory() as d:
                out = Path(d) / "out"
                state_path = out / "pull_state.json"
                out.mkdir()
                state_path.write_text(json.dumps({"ado:org/proj": {"since": "2026-10-02T12:00:00Z", "seen": []}}), encoding="utf-8")
                pull.pull_ado("org", "proj", Path(d) / "inbox", out, http=Http(asof), token="t", now=NOW)
                state = json.loads(state_path.read_text(encoding="utf-8"))["ado:org/proj"]
                self.assertEqual(state["since"], "2026-10-02T12:00:00Z")

    def test_asof_overlap_keeps_ids_without_created_date_deduped(self):
        class Http:
            def __call__(self, method, url, token, body=None):
                if "/wiql" in url:
                    return {"asOf": "2026-10-03T00:59:00Z", "workItems": [{"id": 17}]}
                return {"value": [{"id": 17, "fields": {}}]}

        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            http = Http()
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 1)
            self.assertEqual(pull.pull_ado("org", "proj", inbox, out, http=http, token="t", now=NOW), 0)


class TestMentionEligibility(unittest.TestCase):
    def test_unprocessed_group_message_is_emitted_when_mention_eligibility_changes(self):
        sec = {}
        chat = {"id": "19:g@thread.v2", "kind": "group", "preview": "同じ本文です", "time": "10:00",
                "mention": False, "title": "Group"}
        self.assertEqual(pull.teams_events([chat], sec), [])
        self.assertEqual(pull.teams_events([chat], sec), [])
        mentioned = {**chat, "mention": True}
        events = pull.teams_events([mentioned], sec)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["mentions_me"])
        self.assertEqual(pull.teams_events([mentioned], sec), [])

    def test_first_baseline_and_own_message_rules_still_apply(self):
        sec = {}
        group = {"id": "19:g@thread.v2", "kind": "group", "preview": "@Me 確認ください", "time": "10:00",
                 "mention": True, "title": "Group"}
        self.assertEqual(pull.teams_events([group], sec), [])
        own = {**group, "preview": "You: 確認しました"}
        self.assertEqual(pull.teams_events([own], sec), [])


class TestUiMessageIdentity(unittest.TestCase):
    def test_new_message_fits_visible_limit_when_baseline_is_last_99(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        rows = [{"container_id": f"old-{i}", "runtime_id": f"body-{i}", "y": i,
                 "text": f"prior message {i}"} for i in range(99)]
        rows.append({"container_id": "new-message", "runtime_id": "new-body", "y": 99,
                     "text": "[kimeru #1] 内容"})
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            prior_all = ",".join(f"old-{i}" for i in range(99))
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-readback", "-Text", "[kimeru #1] 内容", "-TestRows", str(rows_path),
                                     "-PriorCount", "0", "-PriorAllIds", prior_all],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["readback"],
                             {"matched": True, "message_id": "new-message"})

    def test_same_message_representations_collapse_but_identical_distinct_messages_remain(self):
        rows = [
            {"container_id": "msg-1", "runtime_id": "body-1", "y": 10, "text": "同じ本文"},
            {"container_id": "msg-1", "runtime_id": "text-1", "y": 10, "text": "同じ本文"},
            {"container_id": "msg-2", "runtime_id": "body-2", "y": 20, "text": "同じ本文"},
        ]
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            script = ROOT / "tools" / "teams-self.ps1"
            runner = "powershell" if os.name == "nt" else "pwsh"
            if os.name != "nt":
                self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-messages", "-TestRows", str(rows_path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            decoded = json.loads(result.stdout.lstrip("\ufeff"))
            self.assertEqual([m["text"] for m in decoded["messages"]], ["同じ本文", "同じ本文"])
            self.assertEqual([m["message_id"] for m in decoded["messages"]], ["msg-1", "msg-2"])

    def test_collection_ancestor_falls_back_to_element_runtime_identity(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        snapshot = {"fallback": "body-runtime", "ancestors": [
            {"automation_id": "conversation-pane", "kind": "Group", "runtime_id": "whole-pane"}
        ]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "container.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-container", "-TestRows", str(path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["identity"], "body-runtime")

    def test_individual_message_container_identity_is_used(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        snapshot = {"fallback": "body-runtime", "ancestors": [
            {"automation_id": "message-container-abc", "kind": "Group", "runtime_id": "msg-runtime"}
        ]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "container.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-container", "-TestRows", str(path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["identity"], "msg-runtime")

    def test_meaningful_whitespace_comparison_preserves_word_boundaries(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        for sent, readback, expected in (("a  b\r\nc", "a b c", True), ("ab c", "a bc", False)):
            with self.subTest(sent=sent, readback=readback):
                result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                         "-Action", "test-compare", "-Text", sent, "-ReadbackText", readback],
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["matches"], expected)

    def test_old_identical_message_is_not_evidence_for_a_new_send(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        rows = [{"container_id": "old-msg", "runtime_id": "body-old", "y": 10, "text": "[kimeru #1] 内容"}]
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-readback", "-Text", "[kimeru #1] 内容", "-TestRows", str(rows_path),
                                     "-PriorIds", "old-msg", "-PriorCount", "1"], capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["readback"], {"matched": False, "message_id": ""})

    def test_runtime_id_change_without_a_new_matching_message_is_not_readback(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        rows = [{"container_id": "new-runtime-id", "runtime_id": "body-new", "y": 10, "text": "[kimeru #1] 内容"}]
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-readback", "-Text", "[kimeru #1] 内容", "-TestRows", str(rows_path),
                                     "-PriorIds", "old-runtime-id", "-PriorCount", "1"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["readback"], {"matched": False, "message_id": ""})

    def test_one_additional_matching_message_with_a_new_identity_confirms_readback(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        rows = [
            {"container_id": "old-msg", "runtime_id": "body-old", "y": 10, "text": "[kimeru #1] 内容"},
            {"container_id": "new-msg", "runtime_id": "body-new", "y": 20, "text": "[kimeru #1] 内容"},
        ]
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-readback", "-Text", "[kimeru #1] 内容", "-TestRows", str(rows_path),
                                     "-PriorIds", "old-msg", "-PriorCount", "1"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["readback"], {"matched": True, "message_id": "new-msg"})

    def test_visible_range_change_makes_readback_unknown_even_when_a_new_id_appears(self):
        script = ROOT / "tools" / "teams-self.ps1"
        runner = "powershell" if os.name == "nt" else "pwsh"
        if os.name != "nt":
            self.skipTest("Windows PowerShell only: tools/teams-self.ps1 loads the Windows UI Automation types")
        rows = [
            {"container_id": "old-match", "runtime_id": "body-old", "y": 10, "text": "[kimeru #1] 内容"},
            {"container_id": "new-match", "runtime_id": "body-new", "y": 20, "text": "[kimeru #1] 内容"},
        ]
        with tempfile.TemporaryDirectory() as d:
            rows_path = Path(d) / "rows.json"
            rows_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([runner, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                     "-Action", "test-readback", "-Text", "[kimeru #1] 内容", "-TestRows", str(rows_path),
                                     "-PriorIds", "old-match", "-PriorCount", "1", "-PriorAllIds", "old-match,old-other"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(json.loads(result.stdout.lstrip("\ufeff"))["readback"], {"matched": False, "message_id": ""})


if __name__ == "__main__":
    unittest.main()

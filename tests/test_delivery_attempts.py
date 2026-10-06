"""Durable synthetic send-attempt and exact recovery-snapshot regressions."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path

from kimeru import cli, fulltext, notify


REC = {"graph": "g", "event_kind": "sample", "event_id": "e1", "node": "n",
       "needs_human": True, "advice": "original decision", "actions": []}


def write_queue(out, record=REC):
    (Path(out) / "queue.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def sent_result(message_id="synthetic-1"):
    return {"ok": True, "typed": True, "sent": True,
            "readback": {"matched": True, "message_id": message_id}}


class TestDurableDeliveryAttempts(unittest.TestCase):
    def test_case_interruption_after_send_is_held_and_never_automatically_reposted(self):
        with tempfile.TemporaryDirectory() as directory:
            write_queue(directory)

            class SentThenInterrupted:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append((text, send))
                    raise KeyboardInterrupt()

            bridge = SentThenInterrupted()
            with self.assertRaises(KeyboardInterrupt):
                notify.notify(directory, bridge, send=True)
            attempted = notify.Approvals(directory).data["items"]["1"]
            snapshot = attempted["delivery_attempt"]
            self.assertTrue(attempted["delivery_unknown"])
            self.assertEqual(snapshot["body"], bridge.calls[0][0])
            self.assertEqual((snapshot["part"], snapshot["revision"], snapshot["identity"]),
                             ("post", 1, attempted["key"]))
            self.assertEqual(notify.delivery_pending(directory)[0]["part"], "post")
            self.assertEqual(notify.notify(directory, bridge, send=True), [])
            self.assertEqual(len(bridge.calls), 1)

    def test_known_unsent_failure_can_retry_and_verified_send_clears_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            write_queue(directory)

            class RetryOnce:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append((text, send))
                    if len(self.calls) == 1:
                        return {"ok": True, "typed": True, "sent": False}
                    return sent_result()

            bridge = RetryOnce()
            with self.assertRaisesRegex(RuntimeError, "not posted"):
                notify.notify(directory, bridge, send=True)
            first = notify.Approvals(directory).data["items"]["1"]
            self.assertNotIn("delivery_attempt", first)
            self.assertFalse(first.get("delivery_unknown", False))
            self.assertEqual(notify.notify(directory, bridge, send=True), [1])
            saved = notify.Approvals(directory).data["items"]["1"]
            self.assertTrue(saved["posted"])
            self.assertNotIn("delivery_attempt", saved)
            self.assertFalse(saved.get("delivery_unknown", False))
            self.assertEqual(len(bridge.calls), 2)

    def test_paste_only_never_registers_a_send_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            write_queue(directory)

            class Paste:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append((text, send))
                    return {}

            bridge = Paste()
            self.assertEqual(notify.notify(directory, bridge, send=False), [1])
            item = notify.Approvals(directory).data["items"]["1"]
            self.assertFalse(any(send for _, send in bridge.calls))
            self.assertNotIn("delivery_attempt", item)
            self.assertFalse(item.get("delivery_unknown", False))

    def test_recovery_shows_attempted_revision_body_after_case_is_revised(self):
        with tempfile.TemporaryDirectory() as directory:
            record = {**REC, "event": {"kind": "teams.chat", "id": "old", "chat_id": "chat-1",
                                       "author": "A", "text": "old body"}}
            write_queue(directory, record)

            class Interrupt:
                def __init__(self):
                    self.body = None

                def post(self, text, send):
                    self.body = text
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                notify.notify(directory, Interrupt(), send=True)
            new_event = {"kind": "teams.chat", "id": "new", "chat_id": "chat-1", "author": "A",
                         "text": "follow-up"}
            new_result = {"graph": "g", "event_id": "new", "node": "n", "outcome": "decide",
                          "needs_human": False, "event": new_event,
                          "advice": "new current decision", "actions": [{"type": "log.only"}]}
            self.assertTrue(cli._merge_locked(Path(directory), new_event, new_result, notify, source_event=new_event))
            item = notify.Approvals(directory).data["items"]["1"]
            self.assertEqual(item["revision"], 2)
            self.assertEqual(item["delivery_attempt"]["revision"], 1)
            shown = notify.delivery_show(directory, "case", "1")
            self.assertIn("original decision", shown)
            self.assertNotIn("new current decision", shown)
            self.assertEqual(notify.confirm_delivery(directory, "case", "1", confirm_delivered=True)["status"],
                             "confirmed")
            saved = notify.Approvals(directory).data["items"]["1"]
            self.assertFalse(saved["posted"])
            self.assertEqual(saved["revision"], 2)

    def test_outbox_confirmation_uses_attempt_revision_and_rejects_changed_head(self):
        with tempfile.TemporaryDirectory() as directory:
            ap = notify.Approvals(directory)
            ap.data["items"]["1"] = {"key": "g:e1:n", "revision": 1, "status": "pending", "posted": False,
                                       "record": {"actions": [{"type": "teams.reply", "text": "reply"}]}}
            old_body = "[kimeru 送信用 #1] old body"
            ap.data["outbox"] = [old_body]
            ap.save()

            class Interrupt:
                def post(self, text, send):
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                notify.flush_outbox(ap, Interrupt(), True)
            current = notify.Approvals(directory)
            current.data["items"]["1"]["revision"] = 2
            current.save()
            self.assertEqual(notify.confirm_delivery(directory, "outbox", "outbox", confirm_delivered=True)["status"],
                             "confirmed")
            saved = notify.Approvals(directory).data
            evidence = saved["items"]["1"]["handoff_evidence"][-1]
            self.assertEqual((evidence["revision"], evidence["identity"]), (1, "1"))
            self.assertEqual(saved["items"]["1"]["revision"], 2)
            self.assertFalse(saved["items"]["1"].get("posted", False))

            other = Path(directory) / "mismatch"
            other.mkdir()
            ap = notify.Approvals(other)
            ap.data["outbox"] = ["[kimeru 送信用 #1] original"]
            ap.save()
            with self.assertRaises(KeyboardInterrupt):
                notify.flush_outbox(ap, Interrupt(), True)
            changed = notify.Approvals(other)
            changed.data["outbox"][0] = "[kimeru 送信用 #1] replaced head"
            changed.save()
            self.assertEqual(notify.confirm_delivery(other, "outbox", "outbox", confirm_delivered=True)["status"],
                             "attempt_mismatch")
            self.assertTrue(notify.Approvals(other).data["outbox_delivery_unknown"])

    def test_copilot_unknown_snapshot_keeps_full_prompt_not_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {**REC, "copilot_request": "short excerpt only"}
            write_queue(directory, request)
            ap = notify.Approvals(directory)
            ap.add("g:e1:n", request)
            item = ap.data["items"]["1"]
            item.update({"posted": True, "revision": 4})
            ap.save()
            full_prompt = "complete prompt including the tail that was not in the excerpt"
            fulltext.save(directory, item["key"], {"text": "source"}, request=full_prompt)

            class Interrupt:
                def post(self, text, send):
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                notify.notify(directory, Interrupt(), send=True)
            saved = notify.Approvals(directory).data["items"]["1"]
            self.assertEqual(saved["delivery_attempt"]["part"], "copilot_request")
            self.assertEqual(notify.delivery_show(directory, "case", "1"),
                             f"[kimeru #1 Copilot 用]\n{full_prompt}")

    def test_notice_attempt_is_durable_and_blocks_second_automatic_call(self):
        with tempfile.TemporaryDirectory() as directory:
            record = {"graph": "g", "event_id": "notice-1", "node": "n", "summary": "notice"}
            (Path(directory) / "notices.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

            class Interrupt:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append(text)
                    raise KeyboardInterrupt()

            bridge = Interrupt()
            with self.assertRaises(KeyboardInterrupt):
                notify.notify_notices(directory, bridge, send=True)
            key = "g:notice-1:n"
            attempt = notify.Approvals(directory).data["notice_delivery_attempts"][key]
            self.assertEqual(attempt["body"], bridge.calls[0])
            self.assertEqual(notify.delivery_show(directory, "notice", key), attempt["body"])
            self.assertEqual(notify.notify_notices(directory, bridge, send=True), [])
            self.assertEqual(len(bridge.calls), 1)

    def test_outbox_attempt_is_durable_and_blocks_second_automatic_call(self):
        with tempfile.TemporaryDirectory() as directory:
            ap = notify.Approvals(directory)
            ap.data["outbox"] = ["exact outbox body"]
            ap.save()

            class Interrupt:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append(text)
                    raise KeyboardInterrupt()

            bridge = Interrupt()
            with self.assertRaises(KeyboardInterrupt):
                notify.flush_outbox(ap, bridge, True)
            saved = notify.Approvals(directory)
            self.assertEqual(saved.data["outbox_delivery_attempt"]["body"], "exact outbox body")
            self.assertEqual(notify.delivery_show(directory, "outbox", "outbox"), "exact outbox body")
            self.assertFalse(notify.flush_outbox(saved, bridge, True))
            self.assertEqual(len(bridge.calls), 1)

    def test_brief_attempt_is_durable_and_blocks_second_automatic_call(self):
        with tempfile.TemporaryDirectory() as directory:

            class Interrupt:
                def __init__(self):
                    self.calls = []

                def post(self, text, send):
                    self.calls.append(text)
                    raise KeyboardInterrupt()

            bridge = Interrupt()
            with self.assertRaises(KeyboardInterrupt):
                notify.deliver_brief(directory, bridge, "exact brief body", True, "2026-10-07")
            state = json.loads((Path(directory) / "daily_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["brief_delivery_unknown"]["body"], "exact brief body")
            self.assertEqual(notify.delivery_show(directory, "brief", "brief"), "exact brief body")
            self.assertEqual(notify.deliver_brief(directory, bridge, "new brief", True, "2026-10-07")["status"],
                             "delivery_unknown")
            self.assertEqual(len(bridge.calls), 1)

    def test_brief_show_returns_none_when_not_pending_and_after_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(notify.delivery_show(directory, "brief", "brief"))

            class Delivered:
                def post(self, text, send):
                    return sent_result()

            self.assertEqual(notify.deliver_brief(directory, Delivered(), "brief body", True, "2026-10-07")["status"],
                             "delivered")
            self.assertIsNone(notify.delivery_show(directory, "brief", "brief"))

            state_path = Path(directory) / "daily_state.json"
            state_path.write_text(json.dumps({"brief_delivery_unknown": {"date": "2026-10-08", "text": "old body"}}),
                                  encoding="utf-8")
            self.assertEqual(notify.confirm_delivery(directory, "brief", "brief", confirm_delivered=True)["status"],
                             "confirmed")
            self.assertIsNone(notify.delivery_show(directory, "brief", "brief"))

    def test_pending_brief_without_valid_snapshot_body_is_labeled_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "daily_state.json"
            state_path.write_text(json.dumps({"brief_delivery_unknown": {"date": "2026-10-07"}}), encoding="utf-8")
            self.assertIn("attempted body unavailable", notify.delivery_show(directory, "brief", "brief"))
            state_path.write_text(json.dumps({"brief_delivery_unknown": {"date": "2026-10-07", "body": None,
                                                                          "text": "not a trustworthy snapshot"}}),
                                  encoding="utf-8")
            shown = notify.delivery_show(directory, "brief", "brief")
            self.assertIn("attempted body unavailable", shown)
            self.assertNotIn("not a trustworthy snapshot", shown)

    def test_legacy_unknown_states_do_not_claim_current_text_is_attempted_text(self):
        with tempfile.TemporaryDirectory() as directory:
            ap = notify.Approvals(directory)
            ap.add("g:e1:n", {**REC, "advice": "new current decision"})
            item = ap.data["items"]["1"]
            item.update({"revision": 8, "delivery_unknown": True, "delivery_unknown_part": "post",
                         "delivery_unknown_revision": 3})
            ap.data["outbox"] = ["new current outbox head"]
            ap.data["outbox_delivery_unknown"] = True
            ap.data["notice_delivery_unknown"] = {"g:e1:n": "2026-10-07T00:00:00Z"}
            ap.save()
            (Path(directory) / "notices.jsonl").write_text(
                json.dumps({"graph": "g", "event_id": "e1", "node": "n", "summary": "current notice"}) + "\n",
                encoding="utf-8")
            for kind, target, current in (("case", "1", "new current decision"),
                                          ("outbox", "outbox", "new current outbox head"),
                                          ("notice", "g:e1:n", "current notice")):
                with self.subTest(kind=kind):
                    shown = notify.delivery_show(directory, kind, target)
                    self.assertIn("attempted body unavailable", shown)
                    self.assertNotIn(current, shown)
            self.assertIn("revision 3", notify.delivery_show(directory, "case", "1"))
            self.assertEqual(notify.retry_delivery(directory, "case", "1", confirm_not_sent=True)["status"],
                             "released")
            self.assertEqual(notify.retry_delivery(directory, "outbox", "outbox", confirm_not_sent=True)["status"],
                             "released")
            self.assertEqual(notify.retry_delivery(directory, "notice", "g:e1:n", confirm_not_sent=True)["status"],
                             "released")


if __name__ == "__main__":
    unittest.main()

"""Explicit local work progress is separate from approval and execution."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import brief, cli, notify, work


class TestWorkProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        ap = notify.Approvals(self.out)
        for n, status, actions in (
            (1, "approved", [{"type": "teams.reply", "text": "reply"}]),
            (2, "pending", []),
            (3, "approved", []),
        ):
            ap.data["items"][str(n)] = {"key": f"g:{n}:n", "status": status, "posted": True,
                                         "record": {"graph": "g", "event_id": str(n), "node": "n",
                                                    "advice": f"case {n}", "actions": actions}}
        ap.data["next"] = 4
        ap.save()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_approved_cases_can_be_tracked_and_missing_fields_stay_unknown(self):
        self.assertEqual(work.set_state(self.out, 1, "in_progress"), "updated")
        with self.assertRaises(work.WorkError):
            work.set_state(self.out, 2, "done")
        rows = work.list_work(self.out)
        first = next(row for row in rows if row["case"] == 1)
        self.assertEqual(first["state"], "in_progress")
        self.assertEqual(first["owner"], "Unknown")
        self.assertEqual(first["due"], "Unknown")
        self.assertEqual(first["completion_condition"], "Unknown")

    def test_metadata_updates_and_transition_log_are_atomic_and_body_free(self):
        self.assertEqual(work.set_state(self.out, 1, "approved", owner="Aya", due="2026-10-10",
                                        completion_condition="review passes"), "updated")
        self.assertEqual(work.set_state(self.out, 1, "done"), "updated")
        ap = notify.Approvals(self.out)
        entry = ap.data["items"]["1"]["work"]
        self.assertEqual(entry["state"], "done")
        self.assertEqual(entry["owner"], "Aya")
        self.assertEqual(entry["due"], "2026-10-10")
        log = [json.loads(line) for line in (self.out / "work-state.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r["from"], r["to"]) for r in log], [("Unknown", "approved"), ("approved", "done")])
        self.assertTrue(all("reply" not in json.dumps(row) for row in log))

    def test_rejects_invalid_state_and_due_date(self):
        with self.assertRaises(work.WorkError):
            work.set_state(self.out, 1, "completed")
        with self.assertRaises(work.WorkError):
            work.set_state(self.out, 1, "blocked", due="2026-02-30")

    def test_evidence_distinguishes_legacy_unknown_and_confirmed_handoff(self):
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["handoff_evidence"] = [{"source": "bridge_readback", "message_id": "uia:1"}]
        ap.data["items"]["1"]["work"] = {"state": "done"}
        ap.data["items"]["3"]["record"]["actions"] = [{"type": "teams.post", "text": "draft"}]
        ap.save()
        evidence = work.evidence(self.out)
        self.assertEqual(evidence["approved_cases"], 2)
        self.assertEqual(evidence["tracked_cases"], 1)
        self.assertEqual(evidence["unknown_progress_cases"], 1)
        self.assertEqual(evidence["states"]["done"], 1)
        self.assertEqual(evidence["handoff_verified_cases"], 1)
        self.assertEqual(evidence["handoff_unknown_cases"], 1)

    def test_cli_work_set_and_list_accept_global_out_after_subcommand(self):
        with mock.patch.object(cli.config, "apply", return_value=[]), mock.patch.object(cli.config, "problems", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["work", "set", "1", "blocked", "--owner", "Aya", "--out", str(self.out)]), 0)
            self.assertIn("blocked", printed.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["work", "list", "--out", str(self.out)]), 0)
            self.assertIn("担当: Aya", printed.getvalue())
            self.assertIn("進捗 blocked", printed.getvalue())

    def test_pass_through_command_dispatches_before_graph_loading(self):
        with mock.patch.object(cli.config, "apply", return_value=[]), mock.patch.object(cli.config, "problems", return_value=[]), \
                mock.patch("kimeru.onboarding.dispatch", return_value=9) as dispatch, \
                mock.patch.object(cli.graph, "load_dir", side_effect=AssertionError("graphs should not load")):
            self.assertEqual(cli.main(["--out", str(self.out), "onboarding", "status"]), 9)
        dispatch.assert_called_once_with(["status"], self.out)

    def test_brief_shows_unfinished_work_separately_without_an_extra_rank_call(self):
        with mock.patch.object(brief, "rank", return_value=[]) as rank:
            text, ranked = brief.build(self.out, object(), now=None, date="2026-10-03")
        self.assertEqual(ranked, [])
        self.assertIn("承認済み・未完了の作業（確認待ちとは別の進捗管理）", text)
        self.assertIn("#1 改訂 1 / Unknown", text)
        self.assertIn("担当: Unknown / 期限: Unknown / 完了条件: Unknown", text)
        rank.assert_called_once()

    def test_outbox_handoff_requires_identity_backed_bridge_readback(self):
        ap = notify.Approvals(self.out)
        ap.data["outbox"] = ["[kimeru 送信用 #1] reply text"]
        class Bridge:
            def post(self, text, send):
                return {"ok": True, "typed": True, "sent": True,
                        "readback": {"matched": True, "message_id": "uia-msg-1"}}
        self.assertTrue(notify.flush_outbox(ap, Bridge(), True))
        saved = notify.Approvals(self.out).data["items"]["1"]
        self.assertEqual(saved["handoff_evidence"][0]["source"], "bridge_readback")
        self.assertEqual(saved["handoff_evidence"][0]["message_id"], "uia-msg-1")

    def test_delivery_confirmation_requires_explicit_flag_and_does_not_forge_readback(self):
        ap = notify.Approvals(self.out)
        ap.data["outbox"] = ["[kimeru 送信用 #1] exact body"]
        ap.data["outbox_delivery_unknown"] = True
        ap.save()
        before = (self.out / "approvals.json").read_text(encoding="utf-8")
        self.assertEqual(notify.confirm_delivery(self.out, "outbox", "outbox")["status"], "confirmation_required")
        self.assertEqual((self.out / "approvals.json").read_text(encoding="utf-8"), before)
        self.assertEqual(notify.delivery_show(self.out, "outbox", "outbox"), "[kimeru 送信用 #1] exact body")
        self.assertEqual(notify.confirm_delivery(self.out, "outbox", "outbox", confirm_delivered=True)["status"], "confirmed")
        evidence = work.evidence(self.out)
        self.assertEqual(evidence["handoff_verified_cases"], 1)
        saved = notify.Approvals(self.out).data["items"]["1"]["handoff_evidence"][-1]
        self.assertEqual(saved["source"], "user_confirmation")
        self.assertNotIn("message_id", saved)  # human confirmation is not bridge/UI readback
        row = json.loads((self.out / "delivery-recovery.log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["source"], "user_confirmation")

    def test_cli_delivery_confirmation_without_flag_does_not_change_state(self):
        ap = notify.Approvals(self.out)
        ap.data["outbox"] = ["[kimeru 送信用 #1] exact body"]
        ap.data["outbox_delivery_unknown"] = True
        ap.save()
        before = (self.out / "approvals.json").read_text(encoding="utf-8")
        with mock.patch.object(cli.config, "apply", return_value=[]), mock.patch.object(cli.config, "problems", return_value=[]):
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(["delivery", "confirm", "outbox", "outbox", "--out", str(self.out)]), 0)
        self.assertIn("confirmation_required", printed.getvalue())
        self.assertEqual((self.out / "approvals.json").read_text(encoding="utf-8"), before)

    def test_confirmed_notice_is_marked_done_and_not_reposted(self):
        record = {"graph": "g", "event_id": "notice-1", "node": "n", "summary": "notice"}
        key = "g:notice-1:n"
        (self.out / "notices.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        ap = notify.Approvals(self.out)
        ap.data["notice_delivery_unknown"] = {key: "2026-10-03T00:00:00Z"}
        ap.save()
        self.assertEqual(notify.confirm_delivery(self.out, "notice", key, confirm_delivered=True)["status"], "confirmed")
        posted = []
        class Bridge:
            def post(self, text, send):
                posted.append((text, send))
                return {"ok": True, "typed": True, "sent": True,
                        "readback": {"matched": True, "message_id": "uia-msg"}}
        self.assertEqual(notify.notify_notices(self.out, Bridge(), send=True), [])
        self.assertEqual(posted, [])
        self.assertIn(key, notify.Approvals(self.out).data["notices"])

    def test_brief_delivery_lock_prevents_two_posts_for_same_day(self):
        import concurrent.futures
        calls = []
        class Bridge:
            def post(self, text, send):
                calls.append(text)
                return {"ok": True, "typed": True, "sent": True,
                        "readback": {"matched": True, "message_id": f"uia-{len(calls)}"}}
        bridge = Bridge()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: notify.deliver_brief(self.out, bridge, "brief", True, "2026-10-03"), range(2)))
        statuses = [row["status"] for row in results]
        self.assertEqual(statuses.count("delivered"), 1)
        self.assertEqual(sum(status in ("busy", "already_delivered") for status in statuses), 1)
        self.assertEqual(calls, ["brief"])

    def test_definitely_unsent_brief_is_reported_and_does_not_set_daily_date(self):
        class Bridge:
            def post(self, text, send):
                return {"ok": True, "typed": True, "sent": False}
        result = notify.deliver_brief(self.out, Bridge(), "brief", True, "2026-10-03")
        self.assertEqual(result["status"], "not_sent")
        state_path = self.out / "daily_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        self.assertNotIn("brief_date", state)
        self.assertNotIn("brief_delivery_unknown", state)

    def test_cli_brief_reports_unknown_and_only_says_sent_after_positive_evidence(self):
        class Bridge:
            def __init__(self, result):
                self.result = result
            def post(self, text, send):
                return self.result
        base = ("brief", "--post", "--send", "--out", str(self.out))
        setup = (mock.patch.object(cli.config, "apply", return_value=[]),
                 mock.patch.object(cli.config, "problems", return_value=[]),
                 mock.patch.object(cli.planner, "load_playbooks", return_value={}),
                 mock.patch.object(cli.graph, "load_dir", return_value={}),
                 mock.patch("kimeru.brief.build", return_value=("brief body", [])))
        with setup[0], setup[1], setup[2], setup[3], setup[4], \
                mock.patch("kimeru.notify.PowerShellBridge", return_value=Bridge({"ok": True, "typed": True, "sent": True,
                                                                                 "readback": {"matched": True}})):
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(list(base)), 1)
        self.assertIn("delivery unknown", printed.getvalue())
        self.assertNotIn("sent\n", printed.getvalue())
        self.assertIn("brief_delivery_unknown", json.loads((self.out / "daily_state.json").read_text(encoding="utf-8")))

        second = Path(self.tmp.name) / "positive"
        second.mkdir()
        argv = ["brief", "--post", "--send", "--out", str(second)]
        with setup[0], setup[1], setup[2], setup[3], setup[4], \
                mock.patch("kimeru.notify.PowerShellBridge", return_value=Bridge({"ok": True, "typed": True, "sent": True,
                                                                                 "readback": {"matched": True, "message_id": "uia-1"}})):
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli.main(argv), 0)
        self.assertIn("sent (verified self-chat readback)", printed.getvalue())


if __name__ == "__main__":
    unittest.main()

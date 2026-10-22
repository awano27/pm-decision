"""The unattended approval read can be postponed or leave the screen changed: neither may pass for "read, no replies", for a success,
or move an approval forward; what is recorded holds fixed words only. Fake bridges only (no Teams)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import daily, fsutil, notify
from kimeru.backends import StubBackend
from kimeru.cli import process
from tests.test_daily import GRAPHS, PBS
from tests.test_notify import REC, FakeBridge, write_queue

SECRET = "社外秘の本文 OK 1"


class ScriptedBridge(FakeBridge):
    """read() answers from a script: a dict (a read) or a BridgeError data dict with "error" (a failure / postponement)."""

    def __init__(self, reads):
        super().__init__()
        self.reads, self.read_calls, self.chat_list = list(reads), 0, []

    def chats(self):
        return self.chat_list

    def read(self):
        self.read_calls += 1
        r = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if "error" in r:
            raise notify.BridgeError(r["error"], r)
        return r


def deferred(reason, restore="none"):
    return {"ok": False, "deferred": True, "reason": reason, "error": f"approval read postponed ({reason})", "restore": restore,
            "returned": restore in ("none", "unchanged", "restored"), "foreground": "unchanged"}


def read_ok(timeline, restore="restored"):
    return {"ok": True, "timeline": timeline,
            "screen": {"restore": restore, "returned": restore in ("none", "unchanged", "restored"), "foreground": "unchanged"}}


def posted(d):
    """One item #1 posted and waiting for the answer."""
    write_queue(d, REC)
    notify.notify(d, FakeBridge(), send=True)
    return json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8"))


class TestCollectPostponed(unittest.TestCase):
    def test_postponed_read_is_not_no_replies_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            before = posted(d)
            b = ScriptedBridge([deferred("user_active")])
            with mock.patch("kimeru.execute.run_approved", side_effect=AssertionError("run")):
                res = notify.collect(d, b, real=True)
            self.assertEqual(list(res), [])
            self.assertEqual(res.deferred, "user_active")
            self.assertFalse(res.busy)
            self.assertEqual(res.screen, {"restore": "none", "returned": True, "foreground": "unchanged"})
            self.assertEqual(json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8")), before)
            self.assertFalse((Path(d) / "approvals.log.jsonl").exists())

    def test_deferred_read_precedes_pending_write_and_outbox_flush(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            ap = notify.Approvals(d)
            ap.data["items"]["1"]["status"] = "approved"
            ap.data["outbox"] = ["queued result"]
            ap.save()
            before = ap.path.read_bytes()
            with mock.patch("kimeru.notify.unwritten", return_value=True), \
                    mock.patch("kimeru.notify.flush_outbox", side_effect=AssertionError("flushed")), \
                    mock.patch("kimeru.execute.run_approved", side_effect=AssertionError("executed")):
                res = notify.collect(d, ScriptedBridge([deferred("user_active")]), real=True, send=True)
            self.assertEqual(res.deferred, "user_active")
            self.assertEqual(ap.path.read_bytes(), before)

    def test_every_postponement_reason_is_kept_and_an_unknown_one_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            for reason in notify.READ_DEFERRED:
                self.assertEqual(notify.collect(d, ScriptedBridge([deferred(reason)])).deferred, reason)
            res = notify.collect(d, ScriptedBridge([deferred(SECRET, restore=SECRET)]))
            self.assertEqual(res.deferred, "other")
            self.assertEqual(res.screen["restore"], "failed")   # an unknown word about the screen is never taken as "put back"

    def test_postponed_then_read_twice_approves_and_runs_once(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            tl = ["P:1", "R:OK 1"]
            b = ScriptedBridge([deferred("teams_in_use"), read_ok(tl), read_ok(tl)])
            with mock.patch("kimeru.execute.run_approved", return_value=[]) as run:
                first = notify.collect(d, b, real=True)
                second = notify.collect(d, b, real=True)
                third = notify.collect(d, b, real=True)
            self.assertEqual((first.deferred, list(first)), ("teams_in_use", []))
            self.assertEqual([c["status"] for c in second], ["approved"])
            self.assertIsNone(second.deferred)
            self.assertEqual(list(third), [])   # the same reply read again: nothing more
            self.assertEqual(run.call_count, 1)
            self.assertEqual(notify.Approvals(d).data["items"]["1"]["status"], "approved")

    def test_a_broken_read_raises_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            before = posted(d)
            b = ScriptedBridge([{"error": "the script did not finish in time", "restore": "failed", "returned": False}])
            with self.assertRaises(notify.BridgeError):
                notify.collect(d, b, real=True)
            self.assertEqual(json.loads((Path(d) / "approvals.json").read_text(encoding="utf-8")), before)

    def test_unsafe_success_payload_cannot_apply_any_reply(self):
        for restore in ("left_self", "failed", "skipped_user_input", "screen_changed"):
            for command in ("OK 1", "NG 1", "済 1", "再実行 1"):
                with self.subTest(restore=restore, command=command), tempfile.TemporaryDirectory() as d:
                    before = posted(d)
                    with mock.patch("kimeru.execute.run_approved", side_effect=AssertionError("executed")):
                        res = notify.collect(d, ScriptedBridge([read_ok(["P:1", "R:" + command], restore)]), real=True)
                    self.assertEqual(list(res), [])
                    self.assertIsNotNone(res.deferred)
                    self.assertEqual(notify.Approvals(d).data, before)

    def test_restore_failure_then_safe_retrieval_applies_once(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            tl = ["P:1", "R:OK 1"]
            b = ScriptedBridge([read_ok(tl, "failed"), read_ok(tl), read_ok(tl)])
            with mock.patch("kimeru.execute.run_approved", return_value=[]) as run:
                first = notify.collect(d, b, real=True)
                self.assertEqual(first.deferred, "restore_failed")
                second = notify.collect(d, b, real=True)
                third = notify.collect(d, b, real=True)
            self.assertEqual([c["status"] for c in second], ["approved"])
            self.assertEqual(list(third), [])
            self.assertEqual(run.call_count, 1)

    def test_foreground_restore_failure_cannot_apply_reply(self):
        with tempfile.TemporaryDirectory() as d:
            before = posted(d)
            payload = read_ok(["P:1", "R:OK 1"])
            payload["screen"]["foreground"] = "not_restored"
            res = notify.collect(d, ScriptedBridge([payload]), real=True)
            self.assertEqual(res.deferred, "restore_failed")
            self.assertEqual(notify.Approvals(d).data, before)

    def test_lock_held_by_another_run_reads_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            b = ScriptedBridge([read_ok(["P:1", "R:OK 1"])])
            with fsutil.exclusive(notify.lock_path(d)) as got:
                self.assertTrue(got)
                res = notify.collect(d, b, real=True)
            self.assertTrue(res.busy)
            self.assertEqual(b.read_calls, 0)
            self.assertEqual(notify.Approvals(d).data["items"]["1"]["status"], "pending")

    def test_nothing_waiting_never_reads(self):
        with tempfile.TemporaryDirectory() as d:
            b = ScriptedBridge([read_ok(["R:OK 1"])])
            res = notify.collect(d, b)
            self.assertEqual((list(res), res.deferred, b.read_calls), ([], None, 0))

    def test_the_manual_command_asks_for_the_interactive_read(self):
        class Interactive(ScriptedBridge):
            def read(self, interactive=False):
                self.interactive = interactive
                return super().read()
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            b = Interactive([read_ok(["P:1", "R:NG 1"])])
            notify.collect(d, b, interactive=True)
            self.assertTrue(b.interactive)

    def test_bridge_passes_the_interactive_switch_only_when_asked(self):
        b = notify.PowerShellBridge(script="x.ps1")
        with mock.patch.object(b, "_run", return_value={"ok": True}) as run:
            b.read()
            b.read(interactive=True)
        self.assertEqual(run.call_args_list[0].args, ("-Action", "read"))
        self.assertEqual(run.call_args_list[1].args, ("-Action", "read", "-Interactive"))


class TestBridgeReport(unittest.TestCase):
    def test_an_unattended_read_that_breaks_silently_reports_the_screen_as_not_put_back(self):
        b = notify.PowerShellBridge(script="x.ps1")
        done = mock.Mock(stdout="", stderr="boom", returncode=1)
        with mock.patch("kimeru.notify.subprocess.run", return_value=done):
            with self.assertRaises(notify.BridgeError) as e:
                b.read()
            self.assertEqual((e.exception.data.get("restore"), e.exception.data.get("returned")), ("failed", False))
            with self.assertRaises(notify.BridgeError) as e:
                b.read(interactive=True)   # the person ran it: the self chat is meant to stay open
            self.assertNotIn("restore", e.exception.data)


class TestDailyPostponed(unittest.TestCase):
    def cycle(self, d, bridge, toaster):
        return daily.cycle(Path(d), Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process, bridge=bridge, send=True,
                           now=datetime(2026, 9, 28, 7, 0), toaster=toaster)

    def alerts(self, shown):
        return [b for t, b in shown if t == "kimeru: チャットの表示"]

    def test_postponed_approvals_are_reported_apart_from_failures_and_no_replies(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            shown = []
            r = self.cycle(d, ScriptedBridge([deferred("user_active")]), lambda t, b: shown.append((t, b)))
            self.assertEqual(r["approvals"], [])
            self.assertEqual(r["approvals_deferred"], "user_active")
            self.assertNotIn("approvals_screen", r)
            self.assertEqual(self.alerts(shown), [])
            lines = daily.status_lines(d)
            self.assertTrue(any(l.startswith("approval replies not read in the last cycle") and "user_active" in l for l in lines))
            self.assertTrue(any("failed steps: none" in l for l in lines))
            self.assertIn("waiting for your answer: 1", lines)

    def test_screen_left_changed_is_recorded_and_told(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            shown = []
            r = self.cycle(d, ScriptedBridge([read_ok(["P:1"], restore="left_self")]), lambda t, b: shown.append((t, b)))
            self.assertEqual(r["approvals_screen"], "left_self")
            self.assertEqual(len(self.alerts(shown)), 1)
            self.assertTrue(any("not put back (left_self)" in l for l in daily.status_lines(d)))

    def test_the_persons_own_use_is_recorded_but_not_alerted(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            shown = []
            r = self.cycle(d, ScriptedBridge([read_ok(["P:1", "R:OK 1"], restore="skipped_user_input")]),
                           lambda t, b: shown.append((t, b)))
            self.assertEqual(r["approvals"], [])
            self.assertEqual(r["approvals_deferred"], "user_input_during_read")
            self.assertEqual(r["approvals_screen"], "skipped_user_input")
            self.assertEqual(self.alerts(shown), [])

    def test_a_broken_read_is_a_failure_and_the_screen_is_told(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            shown = []
            b = ScriptedBridge([{"error": "the script did not finish in time", "restore": "failed", "returned": False}])
            r = self.cycle(d, b, lambda t, b_: shown.append((t, b_)))
            self.assertTrue(r["approvals"].startswith("error: BridgeError"))
            self.assertNotIn("approvals_deferred", r)
            self.assertEqual(r["approvals_screen"], "failed")
            self.assertEqual(len(self.alerts(shown)), 1)
            self.assertEqual(notify.Approvals(d).data["items"]["1"]["status"], "pending")

    def test_foreground_failure_is_reported_apart_from_chat_restoration(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            shown = []
            payload = read_ok(["P:1", "R:OK 1"])
            payload["screen"]["foreground"] = "not_restored"
            r = self.cycle(d, ScriptedBridge([payload]), lambda t, b: shown.append((t, b)))
            self.assertEqual(r["approvals_deferred"], "restore_failed")
            self.assertEqual(r["approvals_screen"], "foreground_not_restored")
            self.assertEqual(len(self.alerts(shown)), 1)
            self.assertEqual(notify.Approvals(d).data["items"]["1"]["status"], "pending")

    def test_the_log_holds_fixed_words_only(self):
        with tempfile.TemporaryDirectory() as d:
            posted(d)
            self.cycle(d, ScriptedBridge([deferred(SECRET, restore=SECRET)]), lambda t, b: None)
            self.cycle(d, ScriptedBridge([read_ok(["P:1"], restore=SECRET)]), lambda t, b: None)
            log = (Path(d) / "daily.log.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(SECRET, log)
            self.assertIn('"reason": "other"', log)


if __name__ == "__main__":
    unittest.main()

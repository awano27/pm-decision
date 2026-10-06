"""Crash recovery for work history and single-flight push state updates."""
try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import config, fsutil, notify, push, work


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"] = {"key": "g:1:n", "status": "approved", "posted": True,
                                  "record": {"summary": "private", "actions": []},
                                  "delivery_snapshot": {"text": "keep"}}
        ap.data["other"] = {"keep": True}
        ap.save()

    def tearDown(self):
        self.tmp.cleanup()

    def history(self):
        path = self.out / "work-state.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_initial_approvals_save_failure_leaves_no_transition(self):
        with mock.patch.object(notify.Approvals, "save", side_effect=OSError("injected")):
            with self.assertRaisesRegex(work.WorkError, "progress was not saved"):
                work.set_state(self.out, 1, "done")
        saved = notify.Approvals(self.out).data
        self.assertNotIn("work", saved["items"]["1"])
        self.assertFalse(saved.get("work_history_pending"))
        self.assertEqual(self.history(), [])

    def test_journal_failure_keeps_pending_and_retry_recovers_once(self):
        with mock.patch.object(work, "_write_history", side_effect=OSError("injected journal failure")):
            with self.assertRaisesRegex(work.WorkError, "progress is saved.*recovery is still pending"):
                work.set_state(self.out, 1, "done")
        committed = notify.Approvals(self.out).data
        pending = committed["work_history_pending"]
        self.assertEqual(committed["items"]["1"]["work"]["state"], "done")
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.history(), [])
        self.assertEqual(work.set_state(self.out, 1, "done"), "unchanged")
        rows = self.history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transition_id"], pending[0]["transition_id"])
        self.assertEqual((rows[0]["from"], rows[0]["to"]), ("Unknown", "done"))
        self.assertFalse(notify.Approvals(self.out).data.get("work_history_pending"))

    def test_interrupt_before_journal_replace_preserves_prior_rows_and_recovers_all_transitions(self):
        work.set_state(self.out, 1, "approved", now=datetime(2026, 1, 1))
        original_replace = fsutil._replace

        def interrupt_journal_replace(src, dst, *args, **kwargs):
            if Path(dst).name == "work-state.jsonl":
                raise KeyboardInterrupt
            return original_replace(src, dst, *args, **kwargs)

        with mock.patch.object(fsutil, "_replace", side_effect=interrupt_journal_replace):
            with self.assertRaises(KeyboardInterrupt):
                work.set_state(self.out, 1, "in_progress", now=datetime(2026, 1, 2))
        self.assertEqual([(row["from"], row["to"]) for row in self.history()], [("Unknown", "approved")])
        saved = notify.Approvals(self.out).data
        self.assertEqual(saved["items"]["1"]["work"]["state"], "in_progress")
        self.assertEqual(len(saved["work_history_pending"]), 1)
        work.set_state(self.out, 1, "done", now=datetime(2026, 1, 3))
        rows = self.history()
        self.assertEqual([(row["from"], row["to"]) for row in rows],
                         [("Unknown", "approved"), ("approved", "in_progress"), ("in_progress", "done")])
        self.assertEqual(len({row["transition_id"] for row in rows}), 3)

    def test_missing_primary_uses_valid_backup_with_pending_transition(self):
        work.set_state(self.out, 1, "approved", now=datetime(2026, 1, 1))
        journal = self.out / "work-state.jsonl"
        backup = journal.with_name(journal.name + ".bak")
        journal.replace(backup)
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["work"] = {"state": "in_progress", "updated_at": "2026-01-02T00:00:00+00:00"}
        ap.data["work_history_pending"] = [{"transition_id": "transition-2", "at": "2026-01-02T00:00:00+00:00",
                                             "case": "1", "from": "approved", "to": "in_progress"}]
        ap.save()
        work.set_state(self.out, 1, "done", now=datetime(2026, 1, 3))
        rows = self.history()
        self.assertEqual([(row["from"], row["to"]) for row in rows],
                         [("Unknown", "approved"), ("approved", "in_progress"), ("in_progress", "done")])
        self.assertEqual(rows[1]["transition_id"], "transition-2")

    def test_corrupt_backup_fails_closed_when_primary_is_missing(self):
        (self.out / "work-state.jsonl.bak").write_text('{"broken"\n', encoding="utf-8")
        before = notify.Approvals(self.out).data
        with self.assertRaisesRegex(work.WorkError, "work history is damaged"):
            work.set_state(self.out, 1, "done")
        self.assertEqual(notify.Approvals(self.out).data, before)

    def test_malformed_transition_ids_fail_closed(self):
        ap = notify.Approvals(self.out)
        ap.data["work_history_pending"] = [{"transition_id": ["not", "hashable"]}]
        ap.save()
        with self.assertRaisesRegex(work.WorkError, "pending work history is damaged"):
            work.set_state(self.out, 1, "done")

    def test_cleanup_save_failure_after_journal_commit_does_not_duplicate_on_retry(self):
        original = notify.Approvals.save
        calls = {"n": 0}

        def fail_second_save(ap, compact=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected cleanup failure")
            return original(ap, compact=compact)

        with mock.patch.object(notify.Approvals, "save", fail_second_save):
            with self.assertRaisesRegex(work.WorkError, "progress is saved"):
                work.set_state(self.out, 1, "done")
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(len(notify.Approvals(self.out).data["work_history_pending"]), 1)
        work.set_state(self.out, 1, "done")
        rows = self.history()
        self.assertEqual(len(rows), 1)
        self.assertFalse(notify.Approvals(self.out).data.get("work_history_pending"))

    def test_interrupt_after_transition_commit_leaves_recoverable_pending(self):
        original = notify.Approvals.save
        calls = {"n": 0}

        def interrupt_after_commit(ap, compact=False):
            calls["n"] += 1
            original(ap, compact=compact)
            if calls["n"] == 1:
                raise KeyboardInterrupt

        with mock.patch.object(notify.Approvals, "save", interrupt_after_commit):
            with self.assertRaises(KeyboardInterrupt):
                work.set_state(self.out, 1, "done")
        self.assertEqual(notify.Approvals(self.out).data["items"]["1"]["work"]["state"], "done")
        self.assertEqual(len(notify.Approvals(self.out).data["work_history_pending"]), 1)
        work.set_state(self.out, 1, "done")
        self.assertEqual(len(self.history()), 1)

    def test_same_state_retry_does_not_change_timestamp_or_duplicate_history(self):
        work.set_state(self.out, 1, "done", now=datetime(2026, 1, 1))
        before = notify.Approvals(self.out).data["items"]["1"]["work"]["updated_at"]
        self.assertEqual(work.set_state(self.out, 1, "done", now=datetime(2027, 1, 1)), "unchanged")
        self.assertEqual(notify.Approvals(self.out).data["items"]["1"]["work"]["updated_at"], before)
        self.assertEqual(len(self.history()), 1)

    def test_corrupt_history_fails_closed_and_lock_contention_changes_nothing(self):
        journal = self.out / "work-state.jsonl"
        journal.with_name(journal.name + ".bak").write_text(
            json.dumps({"at": "legacy", "case": "9", "from": "Unknown", "to": "approved"}) + "\n",
            encoding="utf-8")
        journal.write_text('{"broken"\n', encoding="utf-8")
        before = notify.Approvals(self.out).data
        with self.assertRaisesRegex(work.WorkError, "work history is damaged"):
            work.set_state(self.out, 1, "done")
        self.assertEqual(notify.Approvals(self.out).data, before)
        journal.unlink()
        with fsutil.exclusive(notify.lock_path(self.out)):
            with self.assertRaisesRegex(work.WorkError, "approvals are busy"):
                work.set_state(self.out, 1, "done")
        self.assertNotIn("work", notify.Approvals(self.out).data["items"]["1"])

    def test_unrelated_delivery_snapshot_and_state_fields_survive_recovery(self):
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"]["work"] = {"state": "approved", "owner": "Aya", "updated_at": "old"}
        ap.data["items"]["1"]["delivery_snapshot"]["attempt_id"] = "delivery-7"
        ap.save()
        work.set_state(self.out, 1, "in_progress")
        saved = notify.Approvals(self.out).data
        self.assertEqual(saved["other"], {"keep": True})
        self.assertEqual(saved["items"]["1"]["delivery_snapshot"], {"text": "keep", "attempt_id": "delivery-7"})


class PushSingleFlight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        clean = {key: value for key, value in os.environ.items() if not key.startswith("KIMERU_")}
        clean["KIMERU_STATE_DIR"] = self.tmp.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        os.environ["KIMERU_PUSH"] = "webhook"
        config.apply([])
        ap = notify.Approvals(self.out)
        ap.data["items"]["1"] = {"status": "pending", "posted": True}
        ap.save()
        push.begin_cycle(self.out, now=0)
        # Begin-cycle captured the current pending item, so add the new one after baseline.
        ap = notify.Approvals(self.out)
        ap.data["items"]["2"] = {"status": "pending", "posted": True}
        ap.save()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_competing_run_is_busy_then_nothing_new_after_first_send(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def sender(route, text):
            calls.append((route, text))
            entered.set()
            self.assertTrue(release.wait(3), "test sender was not released")

        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("first", push.run(self.out, now=1000, sender=sender)))
        thread.start()
        self.assertTrue(entered.wait(3), "first sender did not start")
        relative_alias = Path(os.path.relpath(self.out, Path.cwd()))
        second = push.run(relative_alias, now=1000, sender=lambda r, t: calls.append((r, t)))
        self.assertEqual(second, {"webhook": "busy; retry later"})
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "push run deadlocked")
        self.assertEqual(result["first"], {"webhook": "sent"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(push.run(self.out, now=1100, sender=lambda r, t: calls.append((r, t))), {"webhook": "nothing new"})
        self.assertEqual(len(calls), 1)

    def test_process_local_gate_closes_file_lock_registration_gap(self):
        entered_sender, release_sender = threading.Event(), threading.Event()
        append_paused, release_append = threading.Event(), threading.Event()
        calls, errors = [], []
        original_held = fsutil._held

        class PausingHeld(list):
            def append(self, item):
                if not append_paused.is_set():
                    append_paused.set()
                    if not release_append.wait(3):
                        raise AssertionError("lock registration was not released")
                return super().append(item)

        def sender(route, text):
            calls.append("first")
            entered_sender.set()
            if not release_sender.wait(3):
                raise AssertionError("first sender was not released")

        def first_run():
            try:
                push.run(self.out, now=1000, sender=sender)
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=first_run)
        try:
            fsutil._held = PausingHeld(original_held)
            first.start()
            self.assertTrue(append_paused.wait(3), "first run did not pause before lock registration")
            second = push.run(self.out, now=1000, sender=lambda route, text: calls.append("second"))
            self.assertEqual(second, {"webhook": "busy; retry later"})
            self.assertEqual(calls, [])
        finally:
            release_append.set()
            release_sender.set()
            if first.ident is not None:
                first.join(3)
            fsutil._held = original_held
        self.assertFalse(first.is_alive(), "first push run deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(calls, ["first"])
        self.assertEqual(push.run(self.out, now=1100, sender=lambda r, t: calls.append("unexpected")),
                         {"webhook": "nothing new"})
        self.assertEqual(calls, ["first"])

    def test_different_output_directories_can_send_concurrently(self):
        entered, release = threading.Event(), threading.Event()
        other = self.out / "other"
        other.mkdir()
        ap = notify.Approvals(other)
        ap.data["items"]["1"] = {"status": "pending", "posted": True}
        ap.save()
        push.begin_cycle(other, now=0)
        ap = notify.Approvals(other)
        ap.data["items"]["2"] = {"status": "pending", "posted": True}
        ap.save()
        first = threading.Thread(target=lambda: push.run(
            self.out, now=1000,
            sender=lambda route, text: (entered.set(), release.wait(3))))
        first.start()
        try:
            self.assertTrue(entered.wait(3), "first output sender did not start")
            sent = []
            result = push.run(other, now=1000, sender=lambda route, text: sent.append(route))
            self.assertEqual(result, {"webhook": "sent"})
            self.assertEqual(sent, ["webhook"])
        finally:
            release.set()
            first.join(3)
        self.assertFalse(first.is_alive(), "first output run deadlocked")

    def test_begin_cycle_racing_a_send_returns_busy_without_state_write(self):
        entered, release = threading.Event(), threading.Event()

        def sender(route, text):
            entered.set()
            self.assertTrue(release.wait(3), "test sender was not released")

        thread = threading.Thread(target=lambda: push.run(self.out, now=1000, sender=sender))
        thread.start()
        self.assertTrue(entered.wait(3), "first sender did not start")
        state_path = self.out / "push_state.json"
        before = state_path.read_bytes()
        self.assertEqual(push.begin_cycle(self.out, now=2000), "busy; retry later")
        self.assertEqual(state_path.read_bytes(), before)
        release.set()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "push run deadlocked")


if __name__ == "__main__":
    unittest.main()

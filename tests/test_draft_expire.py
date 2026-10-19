"""T7: draft_expire_hours closes a draft check nobody answered as 期限切れ. At the default (0) nothing changes; a judgment, a severe
case, a held item and an item that can write outside are never closed; a closed item is not applied by a later `OK N`, and the
trial week and the week digest still run."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kimeru import config, expire, graph, notify, plan, report, stats, trial
from tests.test_notify import FakeBridge
from tests.test_post_kind import ADVICE, DRAFT, queue_of

ROOT = Path(__file__).resolve().parent.parent
GRAPHS = graph.load_dir(ROOT / "graphs", plan.load_playbooks(ROOT / "playbooks"))
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ON = {"KIMERU_DRAFT_EXPIRE_HOURS": "24"}
GATED = {"type": "ado.comment", "id": "4812", "text": "確認します", "drafted_by": "fake", "exec_text": "確認します"}


def setup(out):
    """#1 old draft check, #2 fresh draft check, #3 judgment, #4 old draft check that can write an ADO comment, #5 old severe draft."""
    queue_of(out, {**DRAFT, "event_id": "1"}, {**DRAFT, "event_id": "2"}, {**ADVICE, "event_id": "3"},
             {**DRAFT, "event_id": "4", "actions": [GATED]}, {**DRAFT, "event_id": "5", "severe_guard": True})
    notify.notify(out, FakeBridge(), send=True)
    ap = notify.Approvals(out)
    old = (NOW - timedelta(hours=30)).isoformat(timespec="milliseconds")
    new = (NOW - timedelta(hours=2)).isoformat(timespec="milliseconds")
    for n, at in (("1", old), ("2", new), ("3", old), ("4", old), ("5", old)):
        notify.measurement_for(ap.data["items"][n])["posted_at"] = at
    ap.save()
    return notify.Approvals(out)


def status(out):
    return {n: it["status"] for n, it in notify.Approvals(out).data["items"].items()}


class TestDraftExpire(unittest.TestCase):
    def test_the_setting_is_off_by_default_and_listed(self):
        self.assertEqual(config.SETTINGS["draft_expire_hours"], ("KIMERU_DRAFT_EXPIRE_HOURS", "0"))
        self.assertEqual(config.int_value("draft_expire_hours"), 0)
        self.assertIn("draft_expire_hours", config.LOGGABLE)
        self.assertIn("draft_expire_hours", config.SHARE_VALUES)
        self.assertIn("KIMERU_DRAFT_EXPIRE_HOURS", (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"))

    def test_at_zero_nothing_is_closed_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ):
            os.environ.pop("KIMERU_DRAFT_EXPIRE_HOURS", None)
            setup(d)
            before = (Path(d) / "approvals.json").read_bytes()
            self.assertEqual(list(expire.close_expired(d, NOW)), [])
            self.assertEqual((Path(d) / "approvals.json").read_bytes(), before)
            self.assertTrue(all(s == "pending" for s in status(d).values()))
            self.assertFalse((Path(d) / "approvals.log.jsonl").exists())

    def test_only_an_old_draft_check_without_outside_writes_is_closed(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            setup(d)
            self.assertEqual(list(expire.close_expired(d, NOW)), ["1"])
            self.assertEqual(status(d), {"1": "expired", "2": "pending", "3": "pending", "4": "pending", "5": "pending"})
            rows = [json.loads(l) for l in (Path(d) / "approvals.log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(r["id"], r["status"]) for r in rows], [(1, "expired")])
            self.assertEqual(list(expire.close_expired(d, NOW)), [])   # once

    def test_a_held_item_is_not_closed(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            ap = setup(d)
            ap.data["items"]["1"]["status"] = "held"
            ap.save()
            self.assertEqual(list(expire.close_expired(d, NOW)), [])

    def test_ok_after_the_expiry_is_neither_approved_nor_executed(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            setup(d)
            expire.close_expired(d, NOW)
            with mock.patch("kimeru.execute.run_approved") as run:
                for kwargs in ({}, {"real": True}, {"real": True, "send": True}):
                    got = notify.collect(d, FakeBridge(["OK 1", "修正 1 短く", "保留 1"]), **kwargs)
                    self.assertEqual([c for c in got if c.get("id") == 1], [])
                run.assert_not_called()
            self.assertEqual(status(d)["1"], "expired")
            self.assertNotIn("approval_observed_at", notify.Approvals(d).data["items"]["1"]["measurement"])

    def test_control_without_the_expiry_the_same_ok_is_applied(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            setup(d)
            got = notify.collect(d, FakeBridge(["OK 1"]))
            self.assertEqual([c["status"] for c in got if c.get("id") == 1], ["approved"])

    def test_the_trial_the_week_digest_and_the_report_still_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            setup(d)
            expire.close_expired(d, NOW)
            data = trial.collect(d, now=NOW.timestamp())
            self.assertIn("measurement", data)
            trial.render(data)
            self.assertEqual(trial.approval_latencies(d, now=NOW.timestamp()), [])
            text = stats.week(d, now=NOW)
            self.assertIn("期限切れ 1", text.replace("期限切れ  ", "期限切れ "))   # the week's approval results name it
            html = report.build(d, GRAPHS)
            self.assertIn("期限切れ", html)
            self.assertNotIn("状態不明（expired）", html)


if __name__ == "__main__":
    unittest.main()

try:
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import daily, graph, notify
from kimeru.backends import StubBackend
from kimeru.cli import process
from kimeru import plan
from tests.test_daily import FakeTeams, ROOT

PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
NOW = datetime(2026, 9, 28, 9, 0)


class TestDailySourceCoverage(unittest.TestCase):
    def run_cycle(self, root, bridge, send=True, **kwargs):
        root = Path(root)
        return daily.cycle(root / "out", root / "inbox", GRAPHS, StubBackend(), PBS, process,
                           bridge=bridge, send=send, now=NOW, **kwargs)

    def assert_held_for_sources(self, root, report, bridge, *sources):
        self.assertEqual(report["brief"], "error: brief source_incomplete; missing " + ", ".join(sources))
        state_path = Path(root) / "out" / "daily_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        self.assertNotIn("brief_date", state)
        sent_briefs = [p for p, sent in bridge.posts if sent and p.startswith("[kimeru brief")]
        self.assertEqual(sent_briefs, [])
        self.assertIn("source_incomplete", (Path(root) / "out" / "daily.log.jsonl").read_text(encoding="utf-8"))
        lines = daily.status_lines(Path(root) / "out")
        held_lines = [line for line in lines if line.startswith("morning brief held: incomplete sources:")]
        self.assertTrue(held_lines)
        self.assertTrue(all(source in held_lines[0] for source in sources))

    def test_failed_teams_with_empty_inbox_holds_brief_then_recovers_once(self):
        with tempfile.TemporaryDirectory() as d:
            bridge = FakeTeams()
            with mock.patch("kimeru.pull.pull_teams", side_effect=RuntimeError("Teams offline")):
                report = self.run_cycle(d, bridge)
            self.assert_held_for_sources(d, report, bridge, "pull_teams")
            recovered = self.run_cycle(d, bridge)
            self.assertEqual(recovered["brief"], 0)
            self.assertEqual(len([p for p, sent in bridge.posts if sent and p.startswith("[kimeru brief")]), 1)
            again = self.run_cycle(d, bridge)
            self.assertNotIn("brief", again)
            self.assertEqual(len([p for p, sent in bridge.posts if sent and p.startswith("[kimeru brief")]), 1)

    def test_incomplete_sources_do_not_paste_brief_when_send_is_false(self):
        with tempfile.TemporaryDirectory() as d:
            bridge = FakeTeams()
            with mock.patch("kimeru.pull.pull_teams", side_effect=RuntimeError("Teams offline")):
                report = self.run_cycle(d, bridge, send=False)
            self.assertTrue(report["brief"].startswith("error: brief source_incomplete"))
            self.assertFalse(any(p.startswith("[kimeru brief") for p, sent in bridge.posts))
            state_path = Path(d) / "out" / "daily_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            self.assertNotIn("brief_date", state)

    def test_one_optional_source_failure_holds_brief_but_processes_known_inbox(self):
        for failed_source in ("pull_ado", "pull_alerts"):
            with self.subTest(source=failed_source), tempfile.TemporaryDirectory() as d:
                bridge = FakeTeams()
                inbox = Path(d) / "inbox"
                inbox.mkdir()
                (inbox / "known.json").write_text(json.dumps({"id": "m1", "chatId": "19:c", "createdDateTime": "2026-09-28T08:00:00Z",
                    "from": {"user": {"displayName": "X"}}, "body": {"contentType": "text", "content": "これは何ですか"}}), encoding="utf-8")
                options = {"ado": ("org", "project")} if failed_source == "pull_ado" else {"subscription": "sub"}
                with mock.patch("kimeru.pull." + failed_source, side_effect=RuntimeError("source unavailable")):
                    report = self.run_cycle(d, bridge, **options)
                self.assertTrue(report["pull_teams"] == 0)
                self.assertTrue(report["judge"] >= 1)
                self.assertEqual(report["notify"], [1])
                self.assert_held_for_sources(d, report, bridge, failed_source)
                self.assertTrue((inbox / "done" / "known.json").exists())

    def test_empty_healthy_sources_deliver_brief_and_unknown_delivery_stays_held_after_recovery(self):
        class UnknownBrief(FakeTeams):
            def post(self, text, send):
                self.posts.append((text, send))
                if text.startswith("[kimeru brief") and send:
                    return {"ok": True, "typed": True, "sent": True, "readback": {"matched": True}}
                return {"ok": True, "typed": True, "sent": bool(send), "readback": {"matched": bool(send), "message_id": "fake"}}
        with tempfile.TemporaryDirectory() as d:
            bridge = FakeTeams()
            report = self.run_cycle(d, bridge)
            self.assertEqual(report["brief"], 0)
            self.assertEqual(sum(1 for p, sent in bridge.posts if sent and p.startswith("[kimeru brief")), 1)
        with tempfile.TemporaryDirectory() as d:
            bridge = UnknownBrief()
            with mock.patch("kimeru.pull.pull_teams", side_effect=RuntimeError("Teams offline")):
                self.run_cycle(d, bridge)
            # Establish an unresolved delivery from a later complete cycle, then a source failure must preserve that hold.
            self.run_cycle(d, bridge)
            with mock.patch("kimeru.pull.pull_teams", side_effect=RuntimeError("Teams offline")):
                held = self.run_cycle(d, bridge)
            self.assertTrue(held["brief"].startswith("error: brief delivery_unknown"))
            n = sum(1 for p, sent in bridge.posts if sent and p.startswith("[kimeru brief"))
            self.run_cycle(d, bridge)
            self.assertEqual(sum(1 for p, sent in bridge.posts if sent and p.startswith("[kimeru brief")), n)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kimeru import daily, graph, plan
from kimeru.backends import StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)


class FakeTeams:
    """chats() -> chat list; post/read -> in-memory self chat timeline."""

    def __init__(self):
        self.chat_list = []
        self.timeline, self.posts = [], []

    def chats(self):
        return self.chat_list

    def post(self, text, send):
        self.posts.append((text, send))
        if send and text.startswith("[kimeru #"):
            self.timeline.append("P:" + text.split("#", 1)[1].split("]", 1)[0])
        return {"ok": True}

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}


def one_on_one(preview, time):
    return {"id": "19:a_b@unq.gbl.spaces", "kind": "oneOnOne", "title": "Sato", "preview": preview,
            "time": time, "unread": True, "mention": False}


class TestDaily(unittest.TestCase):
    def test_full_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True, now=now)

            t.chat_list = [one_on_one("おはようございます", "8:00")]
            r = run(datetime(2026, 9, 28, 7, 0))                      # baseline, before brief hour
            self.assertEqual((r["pull_teams"], r["judge"]), (0, 0))
            self.assertNotIn("brief", r)

            t.chat_list = [one_on_one("これは何ですか", "8:05")]        # unclear intent -> human queue
            r = run(datetime(2026, 9, 28, 8, 10))
            self.assertEqual((r["pull_teams"], r["judge"]), (1, 1))
            self.assertEqual(len(r["notify"]), 1)
            self.assertEqual(r["brief"], 1)
            n = r["notify"][0]

            t.timeline.append(f"R:OK {n}")                            # reply from the phone
            r = run(datetime(2026, 9, 28, 8, 15))
            self.assertEqual(r["approvals"], [f"#{n}:approved"])
            self.assertNotIn("brief", r)                              # once a day
            self.assertEqual(sum(1 for p, _ in t.posts if p.startswith("[kimeru brief")), 1)

    def test_broken_step_does_not_stop_cycle(self):
        class Broken(FakeTeams):
            def chats(self):
                raise RuntimeError("Teams window not found")
        with tempfile.TemporaryDirectory() as d:
            r = daily.cycle(Path(d) / "out", Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process,
                            bridge=Broken(), send=True, now=datetime(2026, 9, 28, 7, 0))
            self.assertTrue(r["pull_teams"].startswith("error: RuntimeError"))
            self.assertEqual(r["judge"], 0)
            log = (Path(d) / "out" / "daily.log.jsonl").read_text(encoding="utf-8")
            self.assertIn("Teams window not found", log)


if __name__ == "__main__":
    unittest.main()

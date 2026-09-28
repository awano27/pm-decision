import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kimeru import daily, events, graph, plan
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
            self.assertTrue(any(p.startswith("[kimeru brief 2026-09-28]") for p, _ in t.posts))  # cycle's date

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


class TestNotices(unittest.TestCase):
    def test_confident_severe_decision_reaches_the_pm_once(self):
        alert = {"schemaId": "azureMonitorCommonAlertSchema", "data": {"essentials": {
            "alertId": "a1", "alertRule": "checkout-api 5xx", "severity": "Sev1", "monitorCondition": "Fired",
            "description": "customers cannot pay; checkout returns 500 for all users"}, "alertContext": {}}}
        with tempfile.TemporaryDirectory() as d:
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            inbox.mkdir()
            (inbox / "a.json").write_text(json.dumps(alert), encoding="utf-8")
            r = daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True,
                            now=datetime(2026, 9, 28, 7, 0))
            self.assertEqual(r["notices"], 1)
            notice = [p for p, _ in t.posts if p.startswith("[kimeru 通知]")]
            self.assertEqual(len(notice), 1)
            self.assertIn("customers cannot pay", notice[0])
            self.assertNotIn("OK ", notice[0])                       # no number, no reply expected
            r = daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True,
                            now=datetime(2026, 9, 28, 7, 5))
            self.assertEqual(r["notices"], 0)                         # posted once


class TestToast(unittest.TestCase):
    def test_pc_notification_when_something_new_is_posted_only(self):
        shown = []
        with tempfile.TemporaryDirectory() as d:
            out, inbox, t = Path(d) / "out", Path(d) / "inbox", FakeTeams()
            run = lambda now: daily.cycle(out, inbox, GRAPHS, StubBackend(), PBS, process, bridge=t, send=True,
                                          now=now, toaster=lambda title, body: shown.append((title, body)) or "shown")
            t.chat_list = [one_on_one("おはようございます", "8:00")]
            run(datetime(2026, 9, 28, 7, 0))
            self.assertEqual(shown, [])                                   # nothing new: no notification
            t.chat_list = [one_on_one("これは何ですか", "8:05")]
            r = run(datetime(2026, 9, 28, 7, 5))
            self.assertEqual(r["toast"], "shown")
            self.assertEqual(shown[-1][0], "kimeru: 確認待ち 1 件")
            self.assertTrue(shown[-1][1].startswith("#1 "))
            run(datetime(2026, 9, 28, 7, 10))
            self.assertEqual(len(shown), 1)                               # once per new post

    def test_no_notification_without_the_real_bridge_or_send(self):
        with tempfile.TemporaryDirectory() as d:
            r = daily.cycle(Path(d) / "out", Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process,
                            bridge=FakeTeams(), send=True, now=datetime(2026, 9, 28, 7, 0))
            self.assertNotIn("toast", r)


class TestTextMinutes(unittest.TestCase):
    def test_txt_in_inbox_is_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            out, inbox = Path(d) / "out", Path(d) / "inbox"
            inbox.mkdir()
            text = "週次定例 2026/9/30\n\n- 決定: v2.3 のリリースは 10/1 とする\n- 担当 田中: 10/3 までに負荷試験の結果を共有する\n"
            (inbox / "teirei.txt").write_bytes(text.encode("cp932"))
            n = daily.process_inbox(inbox, out, GRAPHS, StubBackend(), PBS, process)
            self.assertEqual(n, 2)
            self.assertTrue((inbox / "done" / "teirei.txt").exists())
            p = events.read_inbox_file(inbox / "done" / "teirei.txt")
            self.assertEqual((p["title"], p["date"]), ("週次定例 2026/9/30", "2026-09-30"))

    def test_title_falls_back_to_file_name(self):
        p = events.minutes_text("- 決定: A とする", "2026-10-02 レビュー")
        self.assertEqual((p["title"], p["date"]), ("2026-10-02 レビュー", "2026-10-02"))

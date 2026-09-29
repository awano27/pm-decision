import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from kimeru import daily, events, graph, plan
from kimeru.backends import BackendUnavailable, StubBackend
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

    def readchat(self, chat_id, count=5):
        """Opening a chat to read it in full: the messages set in `chat_messages` (nothing is opened when it is unknown)."""
        self.opened = getattr(self, "opened", []) + [chat_id]
        msgs = getattr(self, "chat_messages", {}).get(chat_id)
        if msgs is None:
            raise RuntimeError("the chat is not in the list on screen; nothing was opened")
        return {"ok": True, "opened": True, "returned": True, "messages": [{"text": m, "sender": "", "time": ""} for m in msgs[-count:]]}

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
            self.assertEqual(shown[-1][1], "#1")                          # counts and numbers, no message text
            run(datetime(2026, 9, 28, 7, 10))
            self.assertEqual(len(shown), 1)                               # once per new post

    def test_no_notification_without_the_real_bridge_or_send(self):
        with tempfile.TemporaryDirectory() as d:
            r = daily.cycle(Path(d) / "out", Path(d) / "inbox", GRAPHS, StubBackend(), PBS, process,
                            bridge=FakeTeams(), send=True, now=datetime(2026, 9, 28, 7, 0))
            self.assertNotIn("toast", r)


class TestKnownIssues(unittest.TestCase):
    def cycle(self, d, t, backend=None, now=datetime(2026, 9, 28, 9, 0), toaster=None, inbox=None):
        return daily.cycle(Path(d) / "out", inbox or Path(d) / "inbox", GRAPHS, backend or StubBackend(), PBS, process,
                           bridge=t, send=True, now=now, toaster=toaster)

    def test_toast_survives_a_failed_second_post(self):
        shown = []
        with tempfile.TemporaryDirectory() as d:
            t = FakeTeams()
            inbox = Path(d) / "inbox"
            inbox.mkdir()
            for i, txt in enumerate(("これは何ですか", "あれは何ですか")):
                (inbox / f"m{i}.json").write_text(json.dumps({"id": f"m{i}", "chatId": f"19:c{i}", "createdDateTime": "2026-09-28T08:00:00Z",
                                                            "from": {"user": {"displayName": "X"}}, "body": {"contentType": "text", "content": txt}}), encoding="utf-8")
            calls = {"n": 0}
            orig = t.post

            def flaky(text, send):
                calls["n"] += 1
                if calls["n"] in (2, 3):   # the post right after judging, and the cycle's own retry of it
                    raise RuntimeError("compose box busy")
                return orig(text, send)
            t.post = flaky
            r = self.cycle(d, t, toaster=lambda a, b: shown.append((a, b)))
            self.assertTrue(str(r["notify"]).startswith("error"))          # #2 failed after #1 was posted ...
            self.assertEqual(shown[-1], ("kimeru: 確認待ち 1 件 / 投稿できていない確認待ち 1 件", "#1"))   # ... #1 is still announced, #2 counted apart
            t.post = orig
            r = self.cycle(d, t, toaster=lambda a, b: shown.append((a, b)), now=datetime(2026, 9, 28, 9, 5))
            self.assertEqual(shown[-1], ("kimeru: 確認待ち 1 件", "#2"))       # only the new one, once

    def test_failing_toast_is_not_a_failed_cycle(self):
        def boom(a, b):
            raise RuntimeError("no toast")
        with tempfile.TemporaryDirectory() as d:
            t = FakeTeams()
            t.chat_list = [one_on_one("おはようございます", "8:00")]
            self.cycle(d, t, toaster=boom, now=datetime(2026, 9, 28, 7, 0))
            t.chat_list = [one_on_one("これは何ですか", "8:05")]
            r = self.cycle(d, t, toaster=boom, now=datetime(2026, 9, 28, 7, 5))
            self.assertTrue(r["toast"].startswith("failed:"))
            self.assertFalse(any(isinstance(v, str) and v.startswith("error:") for v in r.values()))

    def test_replies_are_acknowledged_on_the_pc(self):
        shown = []
        with tempfile.TemporaryDirectory() as d:
            t = FakeTeams()
            t.chat_list = [one_on_one("おはようございます", "8:00")]
            self.cycle(d, t, toaster=lambda a, b: shown.append((a, b)), now=datetime(2026, 9, 28, 7, 0))
            t.chat_list = [one_on_one("これは何ですか", "8:05")]
            self.cycle(d, t, toaster=lambda a, b: shown.append((a, b)), now=datetime(2026, 9, 28, 7, 5))
            t.timeline.append("R:OK 1。")                                    # a full stop after the number
            self.cycle(d, t, toaster=lambda a, b: shown.append((a, b)), now=datetime(2026, 9, 28, 7, 10))
            self.assertEqual(shown[-1], ("kimeru: 返事を受け付けました", "#1 承認"))

    def test_brief_waits_while_the_judge_is_down(self):
        class Down(StubBackend):
            def ask(self, state, questions):
                raise BackendUnavailable("Kev not reachable")
        with tempfile.TemporaryDirectory() as d:
            t = FakeTeams()
            inbox = Path(d) / "inbox"
            inbox.mkdir()
            (inbox / "m.json").write_text(json.dumps({"id": "m1", "chatId": "19:c", "createdDateTime": "2026-09-28T08:00:00Z",
                                                    "from": {"user": {"displayName": "X"}}, "body": {"contentType": "text", "content": "これは何ですか"}}), encoding="utf-8")
            r = self.cycle(d, t, backend=Down())
            self.assertEqual(r["waiting"], 1)
            self.assertNotIn("brief", r)                                       # not final for the day
            r = self.cycle(d, t, now=datetime(2026, 9, 28, 9, 5))             # judge is back
            self.assertEqual(r["waiting"], 0)
            self.assertIn("brief", r)

    def test_retry_puts_parked_files_back(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = Path(d)
            (inbox / "done").mkdir()
            (inbox / "done" / "a.json.error").write_text("{}", encoding="utf-8")
            (inbox / "done" / "b.json.error").write_text("{}", encoding="utf-8")
            (inbox / "b.json").write_text("{}", encoding="utf-8")            # already there: not overwritten
            self.assertEqual(daily.retry_errors(inbox), 1)
            self.assertTrue((inbox / "a.json").exists())
            self.assertTrue((inbox / "done" / "b.json.error").exists())


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

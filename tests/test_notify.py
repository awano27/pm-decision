import json
import tempfile
import unittest
from pathlib import Path

from kimeru import notify


class FakeBridge:
    def __init__(self, replies=()):
        self.posts, self.replies = [], list(replies)

    def post(self, text, send):
        self.posts.append((text, send))
        return {"ok": True}

    def read(self):
        return {"ok": True, "posts": [], "replies": self.replies}


REC = {"graph": "workitem-intake", "event_kind": "ado.workitem.created", "event_id": "4812", "node": "triage_pm",
       "outcome": "advise", "needs_human": True, "advice": "優先度を判定できないチケット #4812",
       "actions": [{"type": "ado.update", "id": "4812", "fields": {"Microsoft.VSTS.Common.Priority": 2}}]}


def write_queue(d, *recs):
    (Path(d) / "queue.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")


class TestNotify(unittest.TestCase):
    def test_post_format_is_parseable_by_bridge(self):
        text = notify.format_post(3, REC)
        self.assertTrue(text.startswith("[kimeru #3]"))
        self.assertIn("OK 3 / NG 3 / 保留 3", text)
        self.assertIn("ado.update", text)

    def test_paste_only_does_not_mark_posted(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=False), [1])
            self.assertEqual(notify.notify(d, b, send=False), [1])  # still unposted
            self.assertFalse(b.posts[0][1])

    def test_send_posts_once_and_dedups_queue(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC, REC)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=True), [1])
            self.assertEqual(notify.notify(d, b, send=True), [])

    def test_approve_executes_proposed_actions(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, FakeBridge(["OK 1"]))
            self.assertEqual(ch[0]["status"], "approved")
            self.assertEqual(ch[0]["executed"][0]["status"], "planned")
            self.assertEqual(notify.collect(d, FakeBridge(["OK 1", "NG 1"])), [])  # final

    def test_hold_then_reject_and_unknown_ids_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, FakeBridge(["保留 1", "OK 99", "hello"]))
            self.assertEqual([c["status"] for c in ch], ["held"])
            ch = notify.collect(d, FakeBridge(["保留 1", "NG 1"]))
            self.assertEqual([c["status"] for c in ch], ["rejected"])
            self.assertNotIn("executed", ch[0])


class TimelineBridge(FakeBridge):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline

    def read(self):
        return {"ok": True, "timeline": self.timeline}


class TestFreshReplies(unittest.TestCase):
    def test_stale_reply_before_new_post_is_ignored(self):
        tl = ["P:1", "R:OK 1", "P:1"]  # old run approved #1, then a new #1 was posted
        self.assertEqual(notify.fresh_replies({"timeline": tl}), [])
        self.assertEqual(notify.fresh_replies({"timeline": tl + ["R:NG 1"]}), ["NG 1"])

    def test_reply_without_visible_post_is_ignored(self):
        self.assertEqual(notify.fresh_replies({"timeline": ["R:OK 7"]}), [])

    def test_legacy_bridge_falls_back_to_replies(self):
        self.assertEqual(notify.fresh_replies({"replies": ["OK 1"]}), ["OK 1"])

    def test_collect_uses_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            self.assertEqual(notify.collect(d, TimelineBridge(["P:1", "R:OK 1", "P:1"])), [])
            ch = notify.collect(d, TimelineBridge(["P:1", "R:OK 1", "P:1", "R:OK 1"]))
            self.assertEqual([c["status"] for c in ch], ["approved"])

    def test_unposted_item_ignores_replies(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=False)  # pasted only
            self.assertEqual(notify.collect(d, FakeBridge(["OK 1"])), [])


class TestPartialSend(unittest.TestCase):
    def test_failure_on_second_post_keeps_first_marked_sent(self):
        rec2 = {**REC, "event_id": "4813"}

        class FailSecond(FakeBridge):
            def post(self, text, send):
                if len(self.posts) == 1:
                    raise RuntimeError("compose box not found")
                return super().post(text, send)
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC, rec2)
            with self.assertRaises(RuntimeError):
                notify.notify(d, FailSecond(), send=True)
            b = FakeBridge()
            self.assertEqual(notify.notify(d, b, send=True), [2])  # #1 is not re-sent
            self.assertEqual(len(b.posts), 1)


class TestReplyNormalization(unittest.TestCase):
    def test_phone_variants(self):
        for s in ("OK 3", "ok 3", "Ok3", "ＯＫ　３", "ｏｋ３", "OK #3", " OK 3 "):
            self.assertEqual(notify.parse_reply(s), ("OK", "3"), s)
        self.assertEqual(notify.parse_reply("ＮＧ　１２"), ("NG", "12"))
        self.assertEqual(notify.parse_reply("保留　４"), ("保留", "4"))
        for s in ("OK", "OK 3 thanks", "返信: OK 3 / NG 3", "OKAY 3"):
            self.assertIsNone(notify.parse_reply(s), s)

    def test_collect_accepts_full_width_reply(self):
        with tempfile.TemporaryDirectory() as d:
            write_queue(d, REC)
            notify.notify(d, FakeBridge(), send=True)
            ch = notify.collect(d, TimelineBridge(["P:1", "R:ＯＫ　１"]))
            self.assertEqual([c["status"] for c in ch], ["approved"])


class TestAdviseNotExecuted(unittest.TestCase):
    def test_advise_actions_are_not_run_by_process(self):
        from kimeru import graph
        from kimeru.backends import ReplayBackend
        from kimeru.cli import process
        g = {"name": "t", "event": "teams.chat", "start": "a", "nodes": {
            "a": {"kind": "advise", "queue": True, "advice": "x", "actions": [{"type": "teams.reply", "text": "y"}]}}}
        graph.validate(g)
        with tempfile.TemporaryDirectory() as d:
            r = process({"kind": "teams.chat", "id": "1", "text": "hi"}, {"teams.chat": [g]}, ReplayBackend({}), Path(d))
            self.assertEqual(r[0]["executed"], [])
            self.assertTrue((Path(d) / "queue.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

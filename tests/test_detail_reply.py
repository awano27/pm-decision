"""`詳細 N`: the approval post is short ("[kimeru #N]", at most 5 lines); the full text comes back as "[kimeru 詳細 #N]" when the PM asks.
The reply is read once, is not an approval, and the detail post is neither an approval post nor a result post for any reader.
Every Teams is a fake."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kimeru import diagnose, notify

try:
    from .test_notify import REC, FakeBridge, write_queue
except ImportError:
    from test_notify import REC, FakeBridge, write_queue

ROOT = Path(__file__).resolve().parent.parent
REAL_READER = ROOT / "tools" / "teams-self.ps1"
FAKE_READER = ROOT / "tests" / "fake-teams-self.ps1"
HEAD = re.compile(r"^\[kimeru (詳細 )?#(\d+)\]")


class SelfChat(FakeBridge):
    """A self chat that is a timeline: kimeru's posts are classified like the reader does ("P:N" approval post, "D:N" detail post)."""

    def __init__(self):
        super().__init__()
        self.timeline = []

    def post(self, text, send):
        r = super().post(text, send)
        m = HEAD.match(text)
        if m and send:
            self.timeline.append(("D:" if m.group(1) else "P:") + m.group(2))
        return r

    def read(self):
        return {"ok": True, "timeline": list(self.timeline)}

    def say(self, text):
        self.timeline.append("R:" + text)

    def sent(self):
        return [t for t, s in self.posts if s]


class TestTheReplyIsRead(unittest.TestCase):
    def test_the_forms_of_the_reply(self):
        for line, want in (("詳細 3", ("詳細", "3")), ("詳細3", ("詳細", "3")), ("詳細 #3", ("詳細", "3")), ("詳細　３", ("詳細", "3")),
                           ("詳細 3。", ("詳細", "3")), ("詳細", None), ("詳細 3 お願い", None)):
            self.assertEqual(notify.parse_reply(line), want, line)
        self.assertEqual(notify.STATUS["詳細"], "detail")
        self.assertEqual(notify._reply_number("詳細 3"), "3")

    def test_it_is_fresh_once_and_never_blocks_the_other_replies(self):
        fe = notify.fresh_entries
        self.assertEqual(fe({"timeline": ["P:1", "R:詳細 1"]}), [("詳細 1", 0)])
        self.assertEqual(fe({"timeline": ["P:1", "R:詳細 1", "D:1"]}), [])                       # answered by the detail post
        self.assertEqual(fe({"timeline": ["P:1", "R:詳細 1", "D:1", "R:詳細 1"]}), [("詳細 1", 0)])   # asked again
        self.assertEqual(fe({"timeline": ["P:1", "R:OK 1", "D:1"]}), [("OK 1", 0)])               # an OK typed meanwhile is not lost
        self.assertEqual(fe({"timeline": ["P:1", "R:詳細 1", "R:OK 1", "D:1"]}), [("OK 1", 0)])
        self.assertEqual(fe({"timeline": ["P:1", "R:詳細 2", "D:1"]}), [])                       # (no post of #2 on the screen: unproven)
        self.assertEqual(fe({"timeline": ["D:1", "R:詳細 1"]}), [])                              # no approval post of #1: unproven
        self.assertEqual(fe({"timeline": ["P:1", "D:1", "R:詳細 1"]}), [("詳細 1", 0)])
        self.assertEqual(notify.redo_k_entries({"timeline": ["P:1", "X:1:1", "D:1", "R:再実行 1-1"]}), [("1", 1)])   # not a boundary of a redo

    def test_a_detail_post_is_not_an_approval_or_a_result_post(self):
        tl = {"timeline": ["P:1", "D:1", "R:OK 1"]}
        self.assertEqual(notify.fresh_entries(tl), [("OK 1", 0)])
        self.assertIsNone(notify.parse_result_entry("D:1"))
        self.assertEqual(notify.scoped_timeline(tl, test=False), ["P:1", "D:1", "R:OK 1"])
        self.assertEqual(notify.scoped_timeline({"timeline": ["T:1", "D:1", "R:OK 1"]}, test=True), ["P:1", "R:OK 1"])   # a test reader does not see it

    def test_a_test_run_marks_the_detail_post_too(self):
        self.assertEqual(notify.to_test_post("[kimeru 詳細 #5] x"), "[kimeru 試験 詳細 #5] x")
        self.assertEqual(notify.to_test_post("[kimeru #5] x"), "[kimeru 試験 #5] x")

    def test_diagnose_knows_the_post(self):
        self.assertEqual(diagnose._head("[kimeru 詳細 #3] 判断が必要"), ("ready", ""))


class TestTheShortPost(unittest.TestCase):
    def test_a_teams_decision(self):
        rec = {"graph": "teams-chat-triage", "event_kind": "teams.chat", "event_id": "1", "revision": 3,
               "advice": "スケジュール変更の判断", "event": {"author": "山田", "text": "仕様変更してもよいですか。来週の予定に響きます"},
               "memo": {"summary": "仕様変更の可否", "next": "影響範囲を確認して返事する", "missing": ["期限"]},
               "actions": [{"type": "teams.reply", "text": "確認して今日中に返信します。" * 20, "drafted_by": "fake"}]}
        text = notify.format_post(3, rec)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("[kimeru #3] （更新）山田さん: "))
        self.assertLessEqual(len(lines[0]) - len("[kimeru #3] （更新）"), 40)
        self.assertEqual(lines[1], "→ 影響範囲を確認して返事する")
        self.assertTrue(lines[2].startswith("案: ") and len(lines[2]) <= 125)
        self.assertEqual(lines[-1], "OK 3 / NG 3 / 修正 3 <点> / 詳細 3")
        self.assertIsNotNone(re.match(r"^\[kimeru #(\d+)\]", text))       # what the reader takes for an approval post
        for word in ("teams-chat-triage", "改訂", "teams.reply", "不足情報", "OK の効果", "判断メモなし", "承認の対象"):
            self.assertNotIn(word, text)

    def test_no_memo_leaves_no_placeholder(self):
        text = notify.format_post(4, {"graph": "g", "event_kind": "teams.chat", "event_id": "1", "advice": "確認が必要",
                                      "event": {"author": "田中", "text": "相談です"}, "actions": []})
        self.assertEqual(text.splitlines(), ["[kimeru #4] 田中さん: 相談です", "→ 確認が必要", "OK 4 / NG 4 / 詳細 4"])

    def test_the_detail_is_the_long_form(self):
        text = notify.format_detail(3, REC)
        self.assertTrue(text.startswith("[kimeru 詳細 #3] "))
        self.assertIsNone(re.match(r"^\[kimeru #(\d+)\]", text))
        self.assertIn("判断メモなし", text)
        self.assertIn("返信: OK 3 / NG 3 / 保留 3", text)


class TestAskingForTheDetail(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name)
        write_queue(self.out, REC)
        self.chat = SelfChat()
        self.assertEqual(notify.notify(self.out, self.chat, send=True), [1])

    def tearDown(self):
        self.dir.cleanup()

    def status(self):
        return notify.Approvals(self.out).data["items"]["1"]["status"]

    def test_the_detail_is_posted_once_per_reply_and_nothing_is_decided(self):
        self.chat.say("詳細 1")
        self.assertEqual(notify.collect(self.out, self.chat, real=False, send=True), [{"id": 1, "status": "detail"}])
        details = [t for t in self.chat.sent() if t.startswith("[kimeru 詳細 #1]")]
        self.assertEqual(len(details), 1)
        self.assertIn("ado.update", details[0])
        self.assertEqual(self.status(), "pending")
        self.assertEqual(self.chat.timeline, ["P:1", "R:詳細 1", "D:1"])
        self.assertEqual(notify.collect(self.out, self.chat, real=False, send=True), [])          # the same reply is not answered again
        self.assertEqual(len([t for t in self.chat.sent() if t.startswith("[kimeru 詳細")]), 1)
        self.chat.say("詳細 1")                                                                      # asked again: answered again
        self.assertEqual(notify.collect(self.out, self.chat, real=False, send=True), [{"id": 1, "status": "detail"}])
        self.assertEqual(len([t for t in self.chat.sent() if t.startswith("[kimeru 詳細")]), 2)

    def test_the_item_is_still_answerable_after_the_detail(self):
        self.chat.say("詳細 1")
        self.chat.say("詳細 1")                                                                      # two in one step: one answer
        self.chat.say("OK 1")
        ch = notify.collect(self.out, self.chat, real=False, send=True)
        self.assertEqual([c["status"] for c in ch], ["detail", "approved"])
        self.assertEqual(len([t for t in self.chat.sent() if t.startswith("[kimeru 詳細")]), 1)
        self.assertEqual(self.status(), "approved")

    def test_the_number_of_another_item_gets_nothing(self):
        self.chat.say("詳細 9")
        self.assertEqual(notify.collect(self.out, self.chat, real=False, send=True), [])
        self.assertEqual([t for t in self.chat.sent() if t.startswith("[kimeru 詳細")], [])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheFakeReader(unittest.TestCase):
    def read(self, messages):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                return notify.PowerShellBridge(script=FAKE_READER).read()

    def test_the_reply_and_the_detail_post_are_on_the_timeline(self):
        read = self.read(["[kimeru #1] x\nOK 1 / NG 1 / 詳細 1", "詳細 1", "[kimeru 詳細 #1] 全文\n返信: OK 1", "ＯＫ　１", "詳細　#1"])
        self.assertEqual(read["timeline"], ["P:1", "R:詳細 1", "D:1", "R:OK 1", "R:詳細 1"])
        self.assertEqual(notify.fresh_entries(read), [("OK 1", 0), ("詳細 1", 0)])

    def test_a_test_post_of_the_detail_is_ignored(self):
        self.assertEqual(self.read(["[kimeru #1] x", "[kimeru 試験 詳細 #1] x", "詳細 1"])["timeline"], ["P:1", "R:詳細 1"])


class TestTheRealReaderScript(unittest.TestCase):
    """tools/teams-self.ps1 needs the Teams window, so its read is checked in the text: the same lines as the fake reader."""

    def test_it_reads_the_reply_and_the_post(self):
        real = REAL_READER.read_text(encoding="utf-8-sig")
        fake = FAKE_READER.read_text(encoding="utf-8-sig")
        for text in (real, fake):
            self.assertIn("(OK|NG|保留|聞き返し|詳細|再実行|済)", text)
            self.assertRegex(text, r"-match '\^\\\[kimeru 詳細 #\(\\d\+\)\\\]'")
            self.assertIn('"D:" + $Matches[1]', text)


if __name__ == "__main__":
    unittest.main()

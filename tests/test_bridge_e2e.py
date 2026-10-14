"""End-to-end T9 path through the real PowerShellBridge and CLI, with a fake Teams.

Covers what the managed PC exercises except the Teams UI: PowerShell argument
passing of long Japanese multi-line posts, JSON round-trips, notify -> reply -> approvals.
"""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from kimeru import notify

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "tests" / "fake-teams-self.ps1"


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestBridgeE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.chat = self.dir / "chat.json"
        os.environ["KIMERU_FAKE_CHAT"] = str(self.chat)
        self.out = self.dir / "out"
        # the same sample the managed-PC check uses: produces queue items with long Japanese advice
        r = subprocess.run([sys.executable, "-m", "kimeru", "--out", str(self.out), "run",
                            str(ROOT / "examples" / "meeting_minutes.json")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        (self.out / "approvals.json").write_text('{"next": 278, "items": {}}', encoding="utf-8")

    def tearDown(self):
        os.environ.pop("KIMERU_FAKE_CHAT", None)
        self.tmp.cleanup()

    def reply(self, text):
        chat = json.loads(self.chat.read_text(encoding="utf-8-sig"))
        chat["messages"].append(text)
        self.chat.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")

    def test_a_send_reply_is_handed_back_below_the_approval_post_and_counts_once(self):   # R28: the reader hands `送った N` back
        bridge = notify.PowerShellBridge(script=FAKE)
        notify.notify(self.out, bridge, send=True)
        self.reply("送った 278")
        self.reply("送った　２７８")
        read = bridge.read()
        self.assertIn("R:送った 278", read["timeline"])
        self.assertEqual(notify.fresh_sent(read), ["278"])

    def test_a_send_reply_below_the_nudge_counts_when_the_approval_post_is_off_the_screen(self):   # R28: the nudge is handed back as U:
        bridge = notify.PowerShellBridge(script=FAKE)
        notify.notify(self.out, bridge, send=True)
        self.reply("[kimeru 未送信 #278]\n承認した返信が、自分とのチャットの一覧にまだ見えません。")
        self.reply("送った 278")
        with mock.patch.dict(os.environ, {"KIMERU_FAKE_TAIL": "2"}):   # only the nudge and the reply are on the screen
            read = bridge.read()
        self.assertIn("U:278", read["timeline"])
        self.assertNotIn("P:278", read["timeline"])
        self.assertEqual(notify.fresh_sent(read), ["278"])

    def test_the_reader_hands_back_the_deadline_post_and_the_work_replies(self):   # R30: K: is the boundary, 完了 / 延期 are handed back
        bridge = notify.PowerShellBridge(script=FAKE)
        notify.notify(self.out, bridge, send=True)
        self.reply("[kimeru 期限 #278]\n期限の作業があります。")
        self.reply("完了 278 2")
        self.reply("延期　２７８　金")
        read = bridge.read()
        self.assertIn("K:278", read["timeline"])
        self.assertIn("R:完了 278 2", read["timeline"])
        self.assertIn("R:延期 278 金", read["timeline"])
        self.assertEqual(notify.fresh_work(read), [("278", ("完了", "278", 2), "完了 278 2"), ("278", ("延期", "278", "金"), "延期 278 金")])

    def test_notify_reply_approvals(self):
        bridge = notify.PowerShellBridge(script=FAKE)
        ids = notify.notify(self.out, bridge, send=True)
        self.assertEqual(ids[:2], [278, 279])
        posted = json.loads(self.chat.read_text(encoding="utf-8-sig"))["messages"]
        self.assertTrue(posted[0].startswith("[kimeru #278]"))
        self.assertIn("\r\nOK 278 / NG 278 / 詳細 278", posted[0])   # multi-line, Japanese intact

        self.reply("ＯＫ　２７８")          # phone-style full-width
        self.reply("NG 279")
        changes = notify.collect(self.out, bridge)
        self.assertEqual(sorted((c["id"], c["status"]) for c in changes), [(278, "approved"), (279, "rejected")])
        self.assertEqual(notify.collect(self.out, bridge), [])                 # no double processing

    def test_a_dated_hold_reply_reaches_the_reader_and_is_read_once(self):
        # R09: `保留 N <いつ>` is a line the reader hands over (the fake reads it like the real one) and kimeru applies once
        bridge = notify.PowerShellBridge(script=FAKE)
        notify.notify(self.out, bridge, send=True)
        self.reply("保留 278 金曜")
        changes = notify.collect(self.out, bridge, now=datetime(2026, 10, 14, 10, 0))   # a Wednesday
        self.assertEqual(changes, [{"id": 278, "status": "held", "until": "2026-10-16"}])
        self.assertEqual(notify.collect(self.out, bridge, now=datetime(2026, 10, 15, 7, 0)), [])

    def test_cli_commands(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # the CLI resolves tools/teams-self.ps1; point it at the fake through the bridge default
        code = ("import sys; from kimeru import notify; from pathlib import Path; "
                f"notify.PowerShellBridge.__init__.__defaults__ = (Path(r'{FAKE}'),); "
                "from kimeru.cli import main; sys.exit(main(sys.argv[1:]))")
        run = lambda *a: subprocess.run([sys.executable, "-c", code, "--out", str(self.out), *a], cwd=ROOT,
                                        capture_output=True, text=True, encoding="utf-8", env=env)
        r = run("notify", "--send")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("posted: [278, 279", r.stdout)
        self.reply("OK 278")
        r = run("approvals")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("#278 -> approved", r.stdout)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheLabelReply(unittest.TestCase):
    """`違う N` / `違う N k` are handed back by the reader as replies; a word inside a sentence is not a reply."""

    def read(self, messages):
        with tempfile.TemporaryDirectory() as d:
            chat = Path(d) / "chat.json"
            chat.write_text(json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8")
            with mock.patch.dict(os.environ, {"KIMERU_FAKE_CHAT": str(chat)}):
                return notify.PowerShellBridge(script=FAKE).read()

    def test_the_label_is_on_the_timeline_and_a_word_in_a_sentence_is_not(self):
        read = self.read(["[kimeru #12] x\nOK 12 / NG 12", "違う 12 3", "この判断は違う 12", "違う　１２", "違う 12 3 お願い"])
        self.assertEqual(read["timeline"], ["P:12", "R:違う 12 3", "R:違う 12"])
        self.assertEqual(notify.fresh_entries(read), [("違う 12 3", 0), ("違う 12", 0)])


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestSelfOpenFromTheReader(unittest.TestCase):   # N5: status says whether the self chat is open, for the automatic check
    def status(self, teams):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIMERU_FAKE_TEAMS": teams,
                                                                               "KIMERU_FAKE_CHAT": str(Path(d) / "chat.json")}):
            return notify.PowerShellBridge(script=FAKE)._run("-Action", "status")

    def test_the_self_chat_counts_as_open_only_while_the_window_is_shown(self):
        for teams, open_now in (("window", True), ("tray", False), ("none", False)):
            st = self.status(teams)
            self.assertEqual(st["self_open"], open_now, teams)
            self.assertEqual(st["self_open"], st["selfChatOpen"], teams)


if __name__ == "__main__":
    unittest.main()

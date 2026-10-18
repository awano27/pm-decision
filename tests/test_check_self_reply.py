"""run-check.cmd -SelfReply: the reader's `testreply` types only a fixed whitelist of short words (with a number of this run) into the
self chat. The fake reader has the same refusals; the whitelist string is the same in both files."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "tools" / "teams-self.ps1"
FAKE = ROOT / "tests" / "fake-teams-self.ps1"
WHITELIST = re.compile(r"^\$ReplyWhitelist = '([^\r\n]*)'", re.M)


def _whitelist(path):
    return WHITELIST.search(path.read_text(encoding="utf-8-sig")).group(1)


class TestTheWhitelistIsOneString(unittest.TestCase):
    def test_the_real_and_the_fake_reader_have_the_identical_whitelist(self):
        real, fake = _whitelist(REAL), _whitelist(FAKE)
        self.assertEqual(real, fake)
        self.assertTrue(real.startswith("^(OK|NG|保留|詳細|聞き返し|違う|不要|着手|完了|延期|送った|再実行|済) ?(#)?([1-9][0-9]{2})"))
        self.assertTrue(real.endswith("|^(停止|再開)$"))

    def test_the_real_reader_refuses_before_touching_teams(self):
        text = REAL.read_text(encoding="utf-8-sig")
        refuse = text.index("$Text -cnotmatch $ReplyWhitelist")
        self.assertLess(refuse, text.index("Enter-UiLock $(if"))   # the whitelist is checked before the UI lock and the Teams window
        self.assertLess(refuse, text.index(chr(10) + "$w = Get-TeamsWindow" + chr(10) + "if ($Action -eq 'diag')"))
        self.assertIn("-Numbers", text)
        self.assertIn("'testreply'", text)

    def test_the_real_reader_checks_the_self_chat_the_box_and_the_readback_as_send_does(self):
        text = REAL.read_text(encoding="utf-8-sig")
        self.assertIn("$Action -in 'post', 'send', 'testreply') { Wait-UserIdle", text)
        self.assertIn("Test-SelfTitleStrict (Get-TeamsWindow)", text)
        self.assertIn("testreply: the compose box is not empty", text)
        self.assertIn("Find-PostedReadback $Text", text)
        self.assertIn("$w = Open-SelfChat $w", text)
        self.assertLess(text.index("$w = Open-SelfChat $w"), text.index("if ($Action -in 'post', 'testreply') {"))


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "needs Windows PowerShell")
class TestTheFakeReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = Path(self.tmp.name) / "chat.json"
        self.env = {**os.environ, "KIMERU_FAKE_CHAT": str(self.chat)}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, reader, text, numbers="529"):
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(reader), "-Action", "testreply", "-Text", text]
        if numbers is not None:
            args += ["-Numbers", numbers]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=self.env, cwd=ROOT)
        return r.returncode, json.loads(r.stdout.strip().lstrip("﻿").splitlines()[-1])

    def _messages(self):
        return json.loads(self.chat.read_text(encoding="utf-8-sig"))["messages"] if self.chat.exists() else []

    def test_allowed_texts_are_appended_to_the_fake_chat(self):
        for text in ("OK 529", "NG 530", "OK529", "OK #529", "保留 529 明日", "完了 529 1", "聞き返し 529", "送った 529", "停止", "再開"):
            code, out = self._run(FAKE, text, "529,530")
            self.assertEqual((code, out["ok"]), (0, True), text)
        self.assertEqual(self._messages()[0], "OK 529")
        self.assertEqual(len(self._messages()), 10)

    def test_forbidden_texts_are_refused_and_nothing_is_written(self):
        for text in ("OK 99", "OK 999", "[kimeru #1]", "hello", "停止です", "OK 529\nNG 530", "OK 529\n", "ok 529", "OK 529 \\n",
                     "保留 529 0123456789012", "下書き 529 本文", "[kimeru 試験 #529]"):
            code, out = self._run(FAKE, text, "529,530")
            self.assertEqual((code, out["ok"]), (2, False), text)
        self.assertEqual(self._messages(), [])

    def test_a_number_this_run_did_not_post_is_refused(self):
        self.assertFalse(self._run(FAKE, "OK 531", "529,530")[1]["ok"])
        self.assertFalse(self._run(FAKE, "OK 529", None)[1]["ok"])
        self.assertFalse(self._run(FAKE, "OK 529", "12")[1]["ok"])
        self.assertEqual(self._messages(), [])

    def test_the_real_reader_refuses_the_same_texts_without_a_teams_window(self):
        # the refusal comes before Teams is looked at, so this is safe to run anywhere; an allowed text is not tried on the real reader
        for text in ("OK 99", "OK 999", "[kimeru #1]", "hello", "停止です"):
            code, out = self._run(REAL, text, "529")
            self.assertEqual((code, out["ok"]), (2, False), text)
            self.assertIn("nothing typed", out["error"], text)


if __name__ == "__main__":
    unittest.main()

"""What leaves the PC is asked for first, and said where the user sees it:
the T13 reachability probe of the managed-PC check, the always-send scheduled task, and the
list of check items that send anything (docs/managed-pc-check.md)."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, config

ROOT = Path(__file__).resolve().parent.parent


def t13_block():
    text = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8")
    start = text.index("# ---- T13:")
    return text[start:text.index("# ---- T14:", start)]


class TestT13AsksBeforeTheProbe(unittest.TestCase):
    def test_the_head_requests_come_after_a_yes(self):
        block = t13_block()
        ask, probe = block.find("YesNo"), block.find("Invoke-WebRequest")
        self.assertNotEqual(ask, -1, "T13 must ask before contacting outside sites")
        self.assertLess(ask, probe)
        # the three URLs stay as they were, and only inside the Yes branch
        for u in ("https://pypi.org/simple/", "https://github.com", "https://huggingface.co"):
            self.assertIn(u, block[ask:])

    def test_no_keeps_the_one_t13_line_with_net_skip(self):
        block = t13_block()
        self.assertRegex(block, r"else\s*\{\s*'SKIP'\s*\}")
        self.assertEqual(len(re.findall(r"Rec 'T13'", block)), 1, "the result sheet keeps a single T13 key")
        self.assertIn("net: {6}", block)

    def test_t13_is_still_in_the_short_managed_set(self):
        text = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "$Only -contains 'managed'" in l)
        self.assertIn("'T13'", line)


class TestScheduleInstallSaysItSends(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k not in config.SECRET_ENV}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def install(self, rc=0):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=rc)), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--out", str(Path(self.dir.name) / "o"), "--backend", "stub", "schedule", "install"])
        return code, out.getvalue()

    def test_the_output_says_the_task_posts_and_how_to_stop_it(self):
        code, text = self.install()
        self.assertEqual(code, 0)
        self.assertIn("daily --once --send", text)
        self.assertIn(cli.SCHEDULE_SENDS_NOTE, text)
        self.assertIn("schedule remove", cli.SCHEDULE_SENDS_NOTE)
        self.assertIn("daily --once を使って", cli.SCHEDULE_SENDS_NOTE)

    def test_the_registered_line_really_sends(self):
        # the note is only true while the task line carries --send
        _, text = self.install()
        runs = next(l for l in text.splitlines() if l.startswith("runs: "))
        self.assertIn(" daily --once --send ", runs)

    def test_status_and_remove_do_not_print_the_note(self):
        for action in ("status", "remove"):
            out = io.StringIO()
            with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)), redirect_stdout(out):
                cli.main(["--out", str(Path(self.dir.name) / "o"), "schedule", action])
            self.assertNotIn(cli.SCHEDULE_SENDS_NOTE, out.getvalue())


class TestTheDocsListWhatLeavesThePc(unittest.TestCase):
    def test_managed_pc_check_lists_every_sending_item(self):
        text = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
        self.assertNotIn("T5（自分とのチャットへの送信）** と **T11（Jev への送信）** だけ", text)
        start = text.index("PC の外へ出る項目は次のとおり")
        block = text[start:text.index("\n- 本文・人名・トークンは結果シートに書かない", start)]
        for item in ("T5", "T9", "T20", "T21", "T11", "T15", "T17", "T18", "T22", "T13", "T6", "T10"):
            self.assertRegex(block, rf"\b{item}\b", item)

    def test_every_external_item_in_check_ps1_is_in_the_list(self):
        # an item that calls a web request or sends to a chat / Copilot must appear in the list
        ps = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8")
        text = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
        start = text.index("PC の外へ出る項目は次のとおり")
        block = text[start:text.index("\n- 本文・人名・トークンは結果シートに書かない", start)]
        sections = re.split(r"\n# ---- (T\d+)", ps)
        for name, body in zip(sections[1::2], sections[2::2]):
            external = re.search(r"Invoke-WebRequest\s+'?https://", body) or "送信します" in body
            if external:
                self.assertRegex(block, rf"\b{name}\b", f"{name} sends outside the PC but is not listed")

    def test_security_and_readme_say_the_schedule_always_sends(self):
        sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("`schedule install` で登録する自動運転は常に `--send` で投稿する", sec)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        line = next(l for l in readme.splitlines() if l.startswith("python -m kimeru schedule install"))
        self.assertIn("daily --once --send", line)


if __name__ == "__main__":
    unittest.main()

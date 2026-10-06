"""Judged originals (inbox/done, the full text) are kept for inbox_done_keep_days; parked files stay; 0 keeps all.
A revision instruction is logged as its length only."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, config, daily, notify


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k not in config.SECRET_ENV}
        clean["KIMERU_STATE_DIR"] = str(Path(self.dir.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        config.apply([])
        self.inbox = Path(self.dir.name) / "inbox"
        self.done = self.inbox / "done"
        self.done.mkdir(parents=True)

    def tearDown(self):
        self.env.stop()
        config.apply([])
        self.dir.cleanup()

    def file(self, name, days_old):
        p = self.done / name
        p.write_text("{}", encoding="utf-8")
        t = time.time() - days_old * 86400
        os.utime(p, (t, t))
        return p


class TestInboxDone(Base):
    def test_the_default_is_seven_days_and_parked_files_stay(self):
        self.assertEqual(config.SETTINGS["inbox_done_keep_days"][1], "7")
        old, new, parked = self.file("old.json", 8), self.file("new.json", 1), self.file("bad.json.error", 30)
        self.assertEqual(daily.purge_inbox_done(self.inbox), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(parked.exists())

    def test_zero_keeps_everything(self):
        os.environ["KIMERU_INBOX_DONE_KEEP_DAYS"] = "0"
        config.apply([])
        old = self.file("old.json", 400)
        self.assertEqual(daily.purge_inbox_done(self.inbox), 0)
        self.assertTrue(old.exists())

    def test_a_setting_that_is_not_a_number_keeps_the_default(self):
        with mock.patch.object(config, "value", return_value="x"):
            self.assertEqual(daily.inbox_done_keep_days(), 7)

    def test_a_missing_done_folder_is_fine(self):
        self.assertEqual(daily.purge_inbox_done(Path(self.dir.name) / "nowhere"), 0)

    def test_the_schedule_install_says_how_long_originals_stay(self):
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)), redirect_stdout(out):
            cli.main(["--out", str(Path(self.dir.name) / "o"), "--backend", "stub", "schedule", "install"])
        self.assertIn("7 日で消します", out.getvalue())
        self.assertIn("inbox_done_keep_days", out.getvalue())


class TestInstructionIsNotLogged(Base):
    def test_only_the_length_of_a_revision_instruction_is_logged(self):
        try:
            from .test_writer import GRAPHS, MSG, PBS, FakeTeams, FakeWriter
        except ImportError:
            from test_writer import GRAPHS, MSG, PBS, FakeTeams, FakeWriter
        from kimeru.backends import StubBackend
        from kimeru.cli import process
        out, w, t = Path(self.dir.name) / "out", FakeWriter(), FakeTeams()
        process(MSG, GRAPHS, StubBackend(), out, PBS, writer=w)
        notify.notify(out, t, send=True)
        words = "佐藤さんの件はもっと短く"
        t.timeline.append("R:修正 1 " + words)
        changes = notify.collect(out, t, writer=w)
        self.assertEqual(changes[0]["instruction"], words)            # the caller still sees it
        log = (out / "approvals.log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(words, log)
        row = next(json.loads(l) for l in log.splitlines() if "instruction_len" in l)
        self.assertEqual(row["instruction_len"], len(words))
        self.assertNotIn("instruction", row)


if __name__ == "__main__":
    unittest.main()

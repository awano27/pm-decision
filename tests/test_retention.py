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


class TestInstructionFingerprint(Base):
    """A short preference-like revision instruction leaves a 12-digit fingerprint in approvals.log.jsonl, never its words, so
    digest --week can count the same wording written again on another item. The post and the returned change do not change."""
    EVENT = {"author": "佐藤", "text": "来月のリリース日をずらすか判断をお願いします"}
    GOLDEN_POST = ("[kimeru #1] 佐藤さん: 来月のリリース日をずらすか判断をお願いします\n"
                   "→ スケジュール変更の判断: 1.現行日程に依存するチーム・顧客を確認（今日） → 2.選択肢（延期・スコープ縮小・現状維持）を比較（今日） → 3.日程を決定し記録（今日） → 4.関係者へ変更を通知…\n"
                   "案: 佐藤さん、承知しました。（もっと 短く）\n"
                   "OK 1 / NG 1 / 修正 1 <点> / 詳細 1")

    def test_the_same_short_instruction_written_again_with_other_spacing_or_width_has_the_same_fingerprint(self):
        first = notify.instruction_sig("もっと短く", self.EVENT)
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        for again in ("もっと 短く", "もっと　短く", "もっと短く！", "もっと短く。", " もっと短く "):
            self.assertEqual(notify.instruction_sig(again, self.EVENT), first, again)

    def test_a_date_a_name_a_number_a_link_or_a_quote_of_the_colleague_gets_no_fingerprint(self):
        for words in ("明日と書いて", "山田さんに", "#12 を", "もっと短く 2 行で", "詳細は https://example.com を",
                      "担当は a@example.jp で", "来月の件を短く", "あ" * 41):
            self.assertIsNone(notify.instruction_sig(words, self.EVENT), words)
        self.assertIsNotNone(notify.instruction_sig("あ" * 40, self.EVENT))

    def test_the_log_keeps_the_fingerprint_and_not_the_words_and_the_post_is_the_same_as_before(self):
        try:
            from .test_writer import GRAPHS, MSG, PBS, FakeTeams, FakeWriter
        except ImportError:
            from test_writer import GRAPHS, MSG, PBS, FakeTeams, FakeWriter
        from kimeru.backends import StubBackend
        from kimeru.cli import process
        out, w, t = Path(self.dir.name) / "out", FakeWriter(), FakeTeams()
        process(MSG, GRAPHS, StubBackend(), out, PBS, writer=w)
        notify.notify(out, t, send=True)
        t.timeline.append("R:修正 1 もっと 短く")
        changes = notify.collect(out, t, writer=w)
        self.assertEqual(changes, [{"id": 1, "instruction": "もっと 短く", "status": "redrafted"}])   # no fingerprint in the change
        notify.notify(out, t, send=True)
        self.assertEqual(t.posts[-1], self.GOLDEN_POST)
        log = (out / "approvals.log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("もっと", log)
        self.assertNotIn("短く", log)
        row = next(json.loads(l) for l in log.splitlines() if "instruction_len" in l)
        self.assertEqual(row["instruction_sig"], notify.instruction_sig("もっと短く", self.EVENT))


if __name__ == "__main__":
    unittest.main()

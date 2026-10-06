"""When something is missing, kimeru says what and what to do in one line (no traceback), and `schedule remove`
says what it leaves behind."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import re
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, config

ROOT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k not in config.SECRET_ENV}
        clean["KIMERU_STATE_DIR"] = str(Path(self.dir.name) / "state")
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.out = Path(self.dir.name) / "out"

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def cli(self, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(["--out", str(self.out), *argv])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                print(e.code, file=err) if not isinstance(e.code, int) else None
        return rc, out.getvalue(), err.getvalue()


class TestOneLine(Base):
    def test_an_unreachable_kev_on_run_is_one_line(self):
        os.environ["KIMERU_KEV_URL"] = "http://127.0.0.1:1/v1"
        rc, _, err = self.cli("--backend", "kev", "run", str(ROOT / "examples" / "teams_chat.json"))
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("判断モデルに接続できません", err)
        self.assertLessEqual(len(err.strip().splitlines()), 2)

    def test_debug_shows_the_traceback(self):
        os.environ["KIMERU_KEV_URL"] = "http://127.0.0.1:1/v1"
        os.environ["KIMERU_DEBUG"] = "1"
        from kimeru.backends import BackendUnavailable
        with self.assertRaises(BackendUnavailable), redirect_stdout(io.StringIO()):
            cli.main(["--out", str(self.out), "--backend", "kev", "run", str(ROOT / "examples" / "teams_chat.json")])

    def test_a_missing_jev_key_says_where_to_get_one(self):
        rc, _, err = self.cli("--backend", "jev", "run", str(ROOT / "examples" / "teams_chat.json"))
        self.assertNotEqual(rc, 0)
        self.assertIn("TYPESAFE_API_KEY is not set", err)
        self.assertIn("docs.typesafe.ai", err)

    def test_missing_data_folders_stop_validate_and_run(self):
        nowhere = str(Path(self.dir.name) / "nowhere")
        for cmd in (["validate"], ["run", str(ROOT / "examples" / "teams_chat.json")], ["demo", "--pace", "0"]):
            rc, _, err = self.cli("--graphs", nowhere, "--playbooks", nowhere, *cmd)
            self.assertEqual(rc, 2, cmd)
            self.assertIn("フォルダが見つかりません", err)
            self.assertNotIn("Traceback", err)

    def test_validate_with_nothing_to_check_is_not_a_success(self):
        empty = Path(self.dir.name) / "empty"
        empty.mkdir()
        rc, _, err = self.cli("--graphs", str(empty), "--playbooks", str(empty), "validate")
        self.assertEqual(rc, 2)
        self.assertIn("検査するものがありません", err)

    def test_validate_in_the_repo_is_unchanged(self):
        rc, text, _ = self.cli("validate")
        self.assertEqual(rc, 0)
        self.assertIn("ok  teams-chat-triage", text)

    def test_a_missing_demo_scenario_is_one_line(self):
        rc, _, err = self.cli("--backend", "stub", "demo", "--pace", "0", "--scenario", str(Path(self.dir.name) / "x.json"))
        self.assertEqual(rc, 2)
        self.assertIn("デモの台本が見つかりません", err)

    def test_review_without_a_terminal_is_one_line(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            rc, _, err = self.cli("review")
        self.assertEqual(rc, 2)
        self.assertIn("対話用のコマンド", err)


class TestScheduleRemoveSaysWhatStays(Base):
    def test_remove_lists_the_runner_the_saved_settings_and_the_records(self):
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.cli("--backend", "kev", "schedule", "install")
            rc, text, _ = self.cli("schedule", "remove")
        self.assertEqual(rc, 0)
        self.assertIn("run-daily.vbs", text)
        self.assertIn("保存した設定", text)
        self.assertIn("backend", text)
        self.assertIn("config unset", text)
        self.assertIn(str(self.out.resolve()), text)


class TestHelpCoversEveryCommand(unittest.TestCase):
    def test_reference_lists_every_command(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        names = next(g for g in re.findall(r"\{([^}]*)\}", buf.getvalue()) if "validate" in g).split(",")
        for name in names:
            self.assertRegex(ref, rf"kimeru {name}\b|kimeru [\w|]*\b{name}\b|/ {name} ", name)


if __name__ == "__main__":
    unittest.main()

try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, daily
from kimeru.backends import BackendUnavailable


class TestSchedule(unittest.TestCase):
    def test_install_writes_hidden_vbs_runner_and_short_task_command(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data with space"
            a = SimpleNamespace(action="install", minutes=5, extra="", out=str(out), backend="kev")
            with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(cli.schedule(a), 0)
            args = run.call_args[0][0]
            self.assertEqual(args[:3], ["schtasks", "/Create", "/TN"])
            tr = args[args.index("/TR") + 1]
            self.assertTrue(tr.startswith('wscript.exe "'))
            self.assertLess(len(tr), 262)                               # schtasks /TR limit
            vbs = (out / "run-daily.vbs").read_text(encoding="utf-16")   # WSH needs UTF-16 for non-ASCII paths
            self.assertTrue(vbs.startswith('WScript.Quit CreateObject("WScript.Shell").Run("'))
            self.assertTrue(vbs.rstrip().endswith('", 0, True)'))        # hidden window, wait, pass the exit code on
            self.assertEqual(vbs.count("\n"), 1)                         # one line, no stray breaks
            inner = vbs[len('WScript.Quit CreateObject("WScript.Shell").Run("'):-len('", 0, True)\n')]
            self.assertNotIn('"', inner.replace('""', ""))               # every quote doubled for VBS
            self.assertIn("--backend kev daily --once --send", inner)

    def test_ado_names_with_spaces_are_quoted_in_the_runner(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            out = Path(d) / "o"
            a = SimpleNamespace(action="install", minutes=5, extra="", out=str(out), backend="kev",
                                ado_org="contoso", ado_project="My Project")
            self.assertEqual(cli.schedule(a), 0)
            vbs = (out / "run-daily.vbs").read_text(encoding="utf-16")
            self.assertIn('--ado-org ""contoso"" --ado-project ""My Project""', vbs)


class TestStateFolderIsolation(unittest.TestCase):
    def test_a_preset_state_dir_is_never_written_by_the_tests(self):
        import os
        import subprocess as sp
        import sys
        with tempfile.TemporaryDirectory() as a:
            env = {**os.environ, "KIMERU_STATE_DIR": a}
            r = sp.run([sys.executable, "-m", "unittest", "-q", "tests.test_setup.TestSchedule"], env=env,
                       cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(list(Path(a).iterdir()), [])


class TestKevNotUpYet(unittest.TestCase):
    def test_inbox_kept_when_backend_unreachable(self):
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            inbox.mkdir()
            (inbox / "a.json").write_text(json.dumps({"kind": "teams.chat", "id": "1", "text": "x"}), encoding="utf-8")

            def down(*a, **k):
                raise BackendUnavailable("Kev not reachable at http://127.0.0.1:8009/v1 (refused)")
            n = daily.process_inbox(inbox, out, {}, None, {}, down)
            self.assertEqual(n, 0)
            self.assertTrue((inbox / "a.json").exists())                 # waits for the next cycle
            self.assertFalse(list((inbox / "done").glob("*")))
            self.assertIn("waiting", (out / "daily.log.jsonl").read_text(encoding="utf-8"))

    def test_other_errors_still_parked(self):
        with tempfile.TemporaryDirectory() as d:
            inbox, out = Path(d) / "inbox", Path(d) / "out"
            inbox.mkdir()
            (inbox / "b.json").write_text("{}", encoding="utf-8")

            def broken(*a, **k):
                raise RuntimeError("Kev HTTP 500: boom")
            daily.process_inbox(inbox, out, {}, None, {}, broken)
            self.assertTrue((inbox / "done" / "b.json.error").exists())


if __name__ == "__main__":
    unittest.main()

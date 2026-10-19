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


class TestScheduleRemoveRestoresSettings(unittest.TestCase):
    def setUp(self):
        from kimeru import config
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        p = mock.patch.dict("os.environ", {"KIMERU_STATE_DIR": str(self.state)})
        p.start()
        self.addCleanup(p.stop)
        self.out = Path(self.tmp.name) / "out"

    def _run(self, action, **kw):
        a = SimpleNamespace(action=action, minutes=5, extra="", out=str(self.out), backend=kw.pop("backend", "kev"),
                            ado_org=kw.pop("ado_org", ""), ado_project=kw.pop("ado_project", ""), **kw)
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(cli.schedule(a), 0)

    def _file(self):
        return self.config.read_raw()

    def test_remove_puts_back_the_values_from_before_the_first_install(self):
        self.config.set_value("backend", "stub")          # before install: backend set, ado_org / ado_project absent
        self._run("install", backend="kev", ado_org="o1", ado_project="p1")
        self.assertEqual(self._file()["backend"], "kev")
        self._run("install", backend="jev", ado_org="o2", ado_project="p2")   # second install: not the original
        self.assertEqual(self._file()["ado_org"], "o2")
        self._run("remove")
        f = self._file()
        self.assertEqual(f.get("backend"), "stub")        # not "kev": the first install's value is not the original
        self.assertNotIn("ado_org", f)                    # did not exist before: removed
        self.assertNotIn("ado_project", f)
        rec = self.state / "schedule_install.json"
        self.assertTrue(not rec.exists() or rec.read_text(encoding="utf-8").strip() in ("", "{}"))

    def test_a_value_the_pm_changed_after_install_stays(self):
        self._run("install", backend="kev", ado_org="o1", ado_project="p1")
        self.config.set_value("ado_org", "mine")          # changed by hand after install
        self._run("remove")
        f = self._file()
        self.assertEqual(f.get("ado_org"), "mine")
        self.assertNotIn("ado_project", f)                # still what install wrote: removed

    def test_keep_settings_leaves_them_and_no_record_changes_nothing(self):
        self._run("remove")                               # no record: as before
        self.assertEqual(self._file(), {})
        self._run("install", backend="kev", ado_org="o1", ado_project="p1")
        self._run("remove", keep_settings=True)
        self.assertEqual(self._file()["ado_org"], "o1")
        self._run("remove")                               # the record is still there: a later plain remove restores
        self.assertNotIn("ado_org", self._file())

    def test_a_failed_delete_leaves_the_settings_and_the_record(self):
        self.config.set_value("backend", "stub")
        self._run("install", backend="kev", ado_org="o1", ado_project="p1")
        a = SimpleNamespace(action="remove", minutes=5, extra="", out=str(self.out), backend="", ado_org="", ado_project="")
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(cli.schedule(a), 1)
        f = self._file()
        self.assertEqual((f.get("backend"), f.get("ado_org"), f.get("ado_project")), ("kev", "o1", "p1"))
        rec = self.state / "schedule_install.json"
        self.assertIn("ado_org", rec.read_text(encoding="utf-8"))
        self._run("remove")                               # the delete works the next time: the settings go back
        self.assertEqual(self._file().get("backend"), "stub")
        self.assertNotIn("ado_org", self._file())


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

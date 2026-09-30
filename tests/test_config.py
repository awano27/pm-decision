try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, config, daily


class Env(unittest.TestCase):
    """Every test gets its own state folder and none of the KIMERU_* variables of the machine it runs on."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_") and k not in config.SECRET_ENV}
        clean["KIMERU_STATE_DIR"] = self.dir.name
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        self.file = Path(self.dir.name) / "config.json"

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def write(self, obj):
        self.file.write_text(json.dumps(obj), encoding="utf-8")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class TestPrecedence(Env):
    def test_default_then_file_then_env_then_arg(self):
        config.apply([])
        self.assertEqual((config.value("writer"), config.SOURCES["writer"]), ("", "default"))
        self.write({"writer": "m365"})
        config.apply([])
        self.assertEqual((config.value("writer"), config.SOURCES["writer"]), ("m365", "file"))
        os.environ["KIMERU_WRITER"] = "copilot"
        config.apply([])
        self.assertEqual((config.value("writer"), config.SOURCES["writer"]), ("copilot", "env"))
        config.apply(["--backend", "kev"])
        self.assertEqual(config.SOURCES["backend"], "arg")
        config.apply(["--backend=kev"])
        self.assertEqual(config.SOURCES["backend"], "arg")

    def test_show_names_the_source(self):
        self.assertEqual(self.run_cli("config", "set", "writer", "m365")[0], 0)
        rc, out, _ = self.run_cli("config", "show")
        line = next(l for l in out.splitlines() if l.startswith("writer"))
        self.assertIn("m365", line)
        self.assertTrue(line.rstrip().endswith("file"))
        os.environ["KIMERU_WRITER"] = "copilot"
        rc, out, _ = self.run_cli("config", "show")
        line = next(l for l in out.splitlines() if l.startswith("writer"))
        self.assertIn("copilot", line)
        self.assertTrue(line.rstrip().endswith("env"))

    def test_unset_and_path(self):
        self.run_cli("config", "set", "toast", "0")
        self.assertIn("unset", self.run_cli("config", "unset", "toast")[1])
        self.assertNotIn("toast", json.loads(self.file.read_text(encoding="utf-8")))
        self.assertEqual(self.run_cli("config", "path")[1].strip(), str(self.file))

    def test_file_values_reach_the_code_that_reads_the_environment(self):
        self.write({"kev_url": "http://127.0.0.1:9999/v1"})
        config.apply([])
        from kimeru.backends import KevBackend
        self.assertEqual(KevBackend().api, "http://127.0.0.1:9999/v1")


class TestSecrets(Env):
    def test_a_secret_in_the_file_is_not_used_and_is_reported(self):
        self.write({"TYPESAFE_API_KEY": "sk-not-real", "writer": "m365", "token": "x"})
        warnings = config.apply([])
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)
        self.assertEqual(config.value("writer"), "m365")
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("secret" in w for w in warnings))
        self.assertNotIn("sk-not-real", " ".join(warnings))

    def test_set_refuses_a_secret_and_show_only_says_set_or_not(self):
        rc, _, err = self.run_cli("config", "set", "api_key", "abc")
        self.assertEqual(rc, 2)
        self.assertIn("secret", err)
        os.environ["TYPESAFE_API_KEY"] = "sk-not-real"
        _, out, _ = self.run_cli("config", "show")
        self.assertIn("TYPESAFE_API_KEY=set", out)
        self.assertNotIn("sk-not-real", out)

    def test_unknown_setting_is_refused(self):
        self.assertEqual(self.run_cli("config", "set", "nonsense", "1")[0], 2)


class TestBrokenFile(Env):
    def test_broken_json_stops_the_run_and_says_what_is_broken(self):
        self.file.write_text('{"writer": "m365",', encoding="utf-8")
        rc, _, err = self.run_cli("validate")
        self.assertEqual(rc, 2)
        self.assertIn("config file is broken", err)
        self.assertIn("line 1", err)
        self.assertIn(str(self.file), err)

    def test_config_path_still_works_when_the_file_is_broken(self):
        self.file.write_text("not json", encoding="utf-8")
        self.assertEqual(self.run_cli("config", "path")[0], 0)


class TestDailyRecord(Env):
    def test_cycle_records_names_and_sources_but_no_secret(self):
        os.environ["TYPESAFE_API_KEY"] = "sk-not-real"
        os.environ["KIMERU_WRITER"] = "m365"
        self.write({"ado_org": "contoso-not-real", "toast": "0"})
        config.apply([])
        rep = config.report()
        self.assertEqual(rep["effective"]["writer"], {"source": "env", "value": "m365"})
        self.assertEqual(rep["effective"]["toast"], {"source": "file", "value": "0"})
        self.assertEqual(rep["effective"]["ado_org"], {"source": "file"})          # by name and source only
        self.assertNotIn("sk-not-real", json.dumps(rep))
        self.assertNotIn("contoso-not-real", json.dumps(rep))

    def test_an_unset_writer_is_recorded_as_templates(self):
        config.apply([])
        self.assertIn("定型文", config.report()["writer_note"])

    def test_status_lines_show_the_last_cycle_and_the_sources(self):
        out = Path(self.dir.name) / "out"
        daily._log(out, {"step": "config", "effective": {"writer": {"source": "file", "value": "m365"}}})
        daily._log(out, {"step": "cycle", "report": {"judge": 1, "notify": "error: X: boom", "waiting": 2}})
        text = "\n".join(daily.status_lines(out))
        self.assertIn("failed steps: notify", text)
        self.assertIn("could not take: 2", text)
        self.assertIn("writer=file", text)


class TestScheduleSavesSettings(Env):
    def install(self, name, backend):
        out = Path(self.dir.name) / name
        a = SimpleNamespace(action="install", minutes=5, extra="", out=str(out), backend=backend, ado_org="", ado_project="")
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.schedule(a), 0)
        return out

    def test_install_keeps_the_effective_settings_in_the_config_file(self):
        os.environ["KIMERU_WRITER"] = "m365"
        config.apply([])
        self.install("data", "kev")
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved["writer"], "m365")
        self.assertEqual(saved["backend"], "kev")
        self.assertFalse(any(k.lower().endswith("key") for k in saved))

    def test_the_runner_passes_the_exit_code_on(self):
        out = self.install("data2", "stub")
        vbs = (out / "run-daily.vbs").read_text(encoding="utf-16")
        self.assertTrue(vbs.startswith("WScript.Quit "))


class TestArgumentsAndFile(Env):
    def test_argument_reaches_config_show_and_the_record(self):
        self.write({"backend": "kev", "brief_hour": "7"})
        rc, out, _ = self.run_cli("--backend", "stub", "config", "show")
        row = next(l for l in out.splitlines() if l.startswith("backend"))
        self.assertIn("stub", row)
        self.assertIn("arg", row)
        config.apply(["--brief-hour", "9", "daily"])
        self.assertEqual(config.value("brief_hour"), "9")
        self.assertEqual(config.report()["effective"]["brief_hour"], {"source": "arg", "value": "9"})
        config.apply(["--brief-hour=10"])
        self.assertEqual(config.value("brief_hour"), "10")

    def test_schedule_install_with_the_default_value_replaces_the_files_old_one(self):
        self.write({"backend": "kev", "future_setting": "keep"})
        out = Path(self.dir.name) / "o"
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)), redirect_stdout(io.StringIO()):
            rc, *_ = self.run_cli("--out", str(out), "--backend", "stub", "schedule", "install")
        self.assertEqual(rc, 0)
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved["backend"], "stub")
        self.assertEqual(saved["future_setting"], "keep")

    def test_empty_environment_variable_counts_as_not_set(self):
        self.write({"writer": "m365"})
        os.environ["KIMERU_WRITER"] = ""
        config.apply([])
        self.assertEqual((config.value("writer"), config.SOURCES["writer"]), ("m365", "file"))
        self.assertEqual(os.environ["KIMERU_WRITER"], "m365")

    def test_unknown_choice_is_refused_by_set_and_by_reading(self):
        rc, _, err = self.run_cli("config", "set", "backend", "kevv")
        self.assertEqual(rc, 2)
        self.assertIn("kev", err)
        self.assertFalse(self.file.exists())
        self.write({"backend": "kevv"})
        rc, _, err = self.run_cli("validate")
        self.assertEqual(rc, 2)
        self.assertIn("backend='kevv'", err)
        self.assertEqual(self.run_cli("config", "set", "backend", "kev")[0], 0)   # the fix itself still works
        self.assertEqual(self.run_cli("validate")[0], 0)

    def test_broken_file_does_not_stop_status_remove_help_or_replay(self):
        self.file.write_text("not json", encoding="utf-8")
        out = Path(self.dir.name) / "o"
        with mock.patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(self.run_cli("--out", str(out), "schedule", "status")[0], 0)
            self.assertEqual(self.run_cli("--out", str(out), "schedule", "remove")[0], 0)
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            self.run_cli("--help")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(self.run_cli("config", "show")[0], 2)

    def test_set_and_unset_keep_unknown_keys_and_unset_removes_a_secret(self):
        self.write({"writer": "m365", "from_a_newer_version": {"a": 1}, "api_key": "x"})
        self.run_cli("config", "set", "toast", "0")
        self.run_cli("config", "unset", "writer")
        saved = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"from_a_newer_version": {"a": 1}, "api_key": "x", "toast": "0"})
        self.assertIn("unset", self.run_cli("config", "unset", "api_key")[1])
        self.assertNotIn("api_key", json.loads(self.file.read_text(encoding="utf-8")))


class TestLogSize(Env):
    def test_daily_log_stays_under_its_limit_and_status_reads_the_tail(self):
        out = Path(self.dir.name) / "out"
        for i in range(2000):
            daily._log(out, {"step": "cycle", "n": i, "pad": "x" * 200, "report": {"judge": i}})
        p = out / "daily.log.jsonl"
        self.assertLessEqual(p.stat().st_size, daily.LOG_MAX_BYTES)
        rows = daily._tail_rows(p)
        self.assertEqual(rows[-1]["n"], 1999)
        self.assertIn("last cycle:", "\n".join(daily.status_lines(out)))


if __name__ == "__main__":
    unittest.main()

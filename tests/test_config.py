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


if __name__ == "__main__":
    unittest.main()

"""Settings that used to stop or mislead an existing setup: forgiving values, empty variables, the judge address,
the scheduled run's judge, the isolation of the tests, and what the records keep when a writer is set."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kimeru import cli, config, graph, plan, stats, writer
from kimeru.backends import KevBackend, StubBackend
from kimeru.cli import process

ROOT = Path(__file__).resolve().parent.parent
PBS = plan.load_playbooks(ROOT / "playbooks")
GRAPHS = graph.load_dir(ROOT / "graphs", PBS)
SETTING_ENV = [env for env, _ in config.SETTINGS.values()]


def clean_env(**extra):
    """The environment without any kimeru setting, plus `extra`."""
    env = {k: v for k, v in os.environ.items() if k not in SETTING_ENV}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


def state(d):
    return mock.patch.dict(os.environ, {"KIMERU_STATE_DIR": d})


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    except SystemExit as e:
        rc = e.code
    return rc, out.getvalue(), err.getvalue()


class TestForgivingValues(unittest.TestCase):
    def test_the_old_ways_of_writing_a_value_are_accepted(self):
        for raw, key, want in (("Copilot", "writer", "copilot"), (" copilot ", "writer", "copilot"), ("CODEX", "writer", "codex"),
                               ("on", "toast", "1"), ("Off", "toast", "0"), ("True", "plan_lean", "1"), ("no", "read_full", "0"),
                               ("YES", "batch_questions", "1"), (" Kev ", "backend", "kev"), ("08", "brief_hour", "8"), ("Detail", "toast", "detail")):
            with self.subTest(raw=raw), clean_env(**{config.SETTINGS[key][0]: raw}), tempfile.TemporaryDirectory() as d, state(d):
                config.apply()
                self.assertEqual(config.value(key), want)
                self.assertEqual(os.environ[config.SETTINGS[key][0]], want)     # the code that reads the variable sees the same
                self.assertEqual(config.problems(), [])
                self.assertEqual(config.WARNINGS, [])

    def test_a_command_with_the_old_values_runs(self):
        with clean_env(KIMERU_WRITER="Copilot", KIMERU_TOAST="on"), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "validate"])
            self.assertEqual(rc, 0, err)

    def test_an_unknown_value_falls_back_to_the_default_and_says_why(self):
        with clean_env(KIMERU_WRITER="m365_auto", KIMERU_TOAST="sometimes", KIMERU_BRIEF_HOUR="25"), tempfile.TemporaryDirectory() as d, state(d):
            warnings = config.apply()
            self.assertEqual(len(warnings), 3)
            self.assertEqual((config.value("writer"), config.value("toast"), config.value("brief_hour")), ("", "1", "8"))
            self.assertEqual(config.problems(), [])                                   # the run goes on
            self.assertEqual(len(config.report()["warnings"]), 3)                     # and daily.log.jsonl says why

    def test_the_reason_is_shown_at_start_up(self):
        with clean_env(KIMERU_WRITER="m365_auto"), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "validate"])
            self.assertEqual(rc, 0)
            self.assertIn("m365_auto", err)

    def test_an_unknown_backend_stops(self):
        with clean_env(KIMERU_BACKEND="kevv"), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "validate"])
            self.assertEqual(rc, 2)
            self.assertIn("kevv", err)
            rc, out, err = run_main(["--out", d, "config", "path"])                   # the fix stays reachable
            self.assertEqual(rc, 0)

    def test_config_set_reads_the_same_way(self):
        with clean_env(), tempfile.TemporaryDirectory() as d, state(d):
            config.set_value("writer", " Copilot ")
            config.set_value("toast", "on")
            self.assertEqual(json.loads((Path(d) / "config.json").read_text(encoding="utf-8")), {"writer": "copilot", "toast": "1"})
            with self.assertRaises(config.ConfigError):
                config.set_value("writer", "m365_auto")


class TestEmptyMeansNotSet(unittest.TestCase):
    def test_an_empty_kev_address_is_the_local_default_not_the_cloud(self):
        with clean_env(KIMERU_KEV_URL="", KIMERU_CLM_URL="  "):
            self.assertEqual(KevBackend().api, "http://127.0.0.1:8009/v1")
            config.apply()
            self.assertEqual(config.value("kev_url"), "http://127.0.0.1:8009/v1")

    def test_config_show_and_the_real_address_agree(self):
        with clean_env(KIMERU_KEV_URL=""), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "config", "show"])
            self.assertEqual(rc, 0, err)
            shown = re.search(r"^kev_url\s+(\S+)", out, re.M).group(1)
            self.assertEqual(shown, KevBackend().api)
            self.assertNotIn("typesafe", KevBackend().api)

    def test_an_address_that_is_not_this_pc_is_refused_without_an_explicit_setting(self):
        with clean_env(KIMERU_KEV_URL="http://10.1.2.3:8009/v1", KIMERU_BACKEND="kev"):
            with self.assertRaises(SystemExit) as c:
                KevBackend()
            self.assertIn("KIMERU_KEV_URL_REMOTE_OK", str(c.exception))
            config.apply()
            self.assertTrue(config.problems())                                        # the command stops before it sends anything
        with clean_env(KIMERU_KEV_URL="http://10.1.2.3:8009/v1", KIMERU_KEV_URL_REMOTE_OK="1"):
            self.assertEqual(KevBackend().api, "http://10.1.2.3:8009/v1")

    def test_empty_variables_read_directly_do_not_break_or_blank_a_setting(self):
        with clean_env(KIMERU_TITLE_MAX="", KIMERU_WRITER_MODEL="", KIMERU_CODEX_MODEL="", KIMERU_TOAST="", KIMERU_BATCH=""):
            self.assertEqual(graph.one_line("a" * 300), "a" * 99 + "…")              # the default length, no ValueError
            self.assertEqual(writer.ClaudeWriter(exe="x").model, "sonnet")
            self.assertEqual(writer.CodexWriter(exe="x").model, "gpt-6-luna")


class TestArguments(unittest.TestCase):
    def test_the_full_flag_reaches_the_settings_and_an_abbreviation_is_refused(self):
        with clean_env(), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "--backend", "stub", "config", "show"])
            self.assertEqual(rc, 0, err)
            self.assertRegex(out, r"(?m)^backend\s+stub\s+arg")
            rc, out, err = run_main(["--out", d, "--back", "stub", "config", "show"])   # would work for argparse, not for the settings
            self.assertEqual(rc, 2)
            self.assertIn("error", err)


class TestScheduledJudge(unittest.TestCase):
    def install(self, backend, **env):
        with clean_env(**env), tempfile.TemporaryDirectory() as d, state(d), \
                mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)), redirect_stdout(io.StringIO()):
            config.apply()
            out = Path(d) / "o"
            self.assertEqual(cli.schedule(SimpleNamespace(action="install", minutes=5, extra="", out=str(out), backend=backend)), 0)
            saved = json.loads((Path(d) / "config.json").read_text(encoding="utf-8"))
            return (out / "run-daily.vbs").read_text(encoding="utf-16"), saved

    def test_stub_is_written_into_the_start_line_and_wins_over_the_environment(self):
        vbs, saved = self.install("stub", KIMERU_BACKEND="kev")
        self.assertIn("--backend stub daily --once --send", vbs)
        self.assertEqual(saved["backend"], "stub")

    def test_every_backend_is_named(self):
        vbs, _ = self.install("kev")
        self.assertIn("--backend kev daily", vbs)


class TestIsolationOfTheTests(unittest.TestCase):
    def test_discover_with_a_file_pattern_does_not_touch_the_state_folder(self):
        with tempfile.TemporaryDirectory() as a:
            env = {**os.environ, "KIMERU_STATE_DIR": a}
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_setup.py", "-k", "TestSchedule"],
                               env=env, cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(list(Path(a).iterdir()), [])                           # no config.json, nothing

    def test_every_test_module_imports_the_isolation(self):
        for f in sorted((ROOT / "tests").glob("test_*.py")):
            self.assertIn("import isolate", f.read_text(encoding="utf-8"), f.name)


LONG = "来月のリリース日をずらすか判断をお願いします。" + "".join(f"補足{i}として、先方の担当者から要望{i}が届いており、対応の工数を見積もっています。" for i in range(1, 9))
MSG = {"type": "message", "chatId": "19:c1", "id": "m-long", "createdDateTime": "2026-10-01T09:00:00Z",
       "from": {"user": {"displayName": "佐藤"}}, "body": {"contentType": "text", "content": LONG}}


class DraftingWriter:
    NAME = "fake"

    def draft(self, res, event, instruction=None):
        return {key: "佐藤さん、承知しました。" if a["type"] == "teams.reply" else f"説明: {a.get('title')}" for key, a in writer.targets(res)}


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(k)
            yield from strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v)


class TestRecordsStayShortWithAWriter(unittest.TestCase):
    def check(self, w):
        with clean_env(), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            [res] = process(MSG, GRAPHS, StubBackend(), out, PBS, writer=w)
            self.assertTrue(res["needs_human"])
            self.assertTrue(any(a.get("drafted_by") or a.get("held_for") for a in res["actions"]))     # the writer did work
            for name in ("decisions.jsonl", "queue.jsonl"):
                for rec in map(json.loads, (out / name).read_text(encoding="utf-8").splitlines()):
                    for s in strings(rec):
                        self.assertNotIn(LONG[60:140], s, name)                                         # no item holds the long text
                    for k in ("text", "description"):
                        self.assertLessEqual(len((rec.get("material_event") or {}).get(k, "")), 120)
                        self.assertLessEqual(len((rec.get("event") or {}).get(k, "")), 120)
            raw = (out / "decisions.jsonl").read_text(encoding="utf-8") + (out / "queue.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(LONG[:140], raw)
            kept = json.loads((out / "full_text.json").read_text(encoding="utf-8"))
            [entry] = kept.values()
            self.assertEqual(entry["text"], LONG)                                                       # the whole text waits here
            return res, entry

    def test_a_cli_writer(self):
        res, _ = self.check(DraftingWriter())
        self.assertLessEqual(len(res["material_event"]["text"]), 120)

    def test_a_paste_in_request_writer(self):
        res, entry = self.check(writer.M365PromptWriter())
        self.assertNotIn(LONG[125:165], res["copilot_request"])
        self.assertIn(LONG[125:165], entry["request"])                                                     # the full request waits apart

    def test_the_writer_still_gets_the_whole_text_when_the_item_is_redrafted(self):
        from kimeru import notify
        seen = {}

        class Spy(DraftingWriter):
            def draft(self, res, event, instruction=None):
                seen["text"] = event.get("text")
                return super().draft(res, event, instruction)

        class Teams:
            def post(self, text, send):
                return {"ok": True}

            def read(self):
                return {"ok": True, "timeline": []}
        with clean_env(), tempfile.TemporaryDirectory() as d:
            out = Path(d)
            process(MSG, GRAPHS, StubBackend(), out, PBS, writer=Spy())
            notify.notify(out, Teams(), send=False)
            [(num, it)] = notify.Approvals(out).data["items"].items()
            seen.clear()
            notify._redraft(it, "短く", Spy(), out)
            self.assertEqual(seen["text"], LONG)


class TestDocsCommands(unittest.TestCase):
    def test_the_documented_webhook_body_command_keeps_the_config_file_valid(self):
        doc = (ROOT / "docs" / "push-notification.md").read_text(encoding="utf-8")
        block = re.search(r"```powershell\n(@'\n.*?\n'@ \| Set-Content[^\n]*\n.*?)```", doc, re.S).group(1)
        body = re.search(r"@'\n(.*?)\n'@", block, re.S).group(1)
        fname = re.search(r"Set-Content -Encoding utf8 (\S+)", block).group(1)
        cmd = re.search(r'python -m kimeru (config set push_webhook_body "@[^"]+")', block).group(1)
        with clean_env(), tempfile.TemporaryDirectory() as d, state(d):
            (Path(d) / "config.json").write_text(json.dumps({"push": "webhook"}), encoding="utf-8")
            body_file = Path(d) / fname
            body_file.write_text(body, encoding="utf-8-sig")             # PowerShell 5.1 writes a BOM
            argv = ["--out", d] + cmd.replace("@" + fname, "@" + str(body_file)).replace('"', "").split()
            rc, out, err = run_main(argv)
            self.assertEqual(rc, 0, err)
            saved = json.loads((Path(d) / "config.json").read_text(encoding="utf-8"))   # still one JSON object
            self.assertEqual(saved["push"], "webhook")
            self.assertEqual(json.loads(saved["push_webhook_body"].replace("{text}", "x")), {"channel": "me", "message": "x"})
            rc, out, err = run_main(["--out", d, "config", "show"])
            self.assertEqual(rc, 0, err)

    def test_a_body_that_is_not_json_is_not_written(self):
        with clean_env(), tempfile.TemporaryDirectory() as d, state(d):
            rc, out, err = run_main(["--out", d, "config", "set", "push_webhook_body", "{channel: me}"])
            self.assertEqual(rc, 2)
            self.assertFalse((Path(d) / "config.json").exists())

    def test_the_docs_do_not_repeat_a_setting_row_or_name_a_removed_setting(self):
        ref = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
        rows = [l for l in ref.splitlines() if l.startswith("| `KIMERU_")]
        firsts = [l.split("|")[1].strip() for l in rows]
        self.assertEqual(len(firsts), len(set(firsts)))
        self.assertNotIn("KIMERU_PUSH_MAIL_TO", ref)


class TestSmallThings(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_display_scale_probe_runs_on_this_python(self):
        r = subprocess.run([sys.executable, "-c", stats._DPI_PROBE], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(int(r.stdout.strip()), 96)

    def test_the_eval_scripts_do_not_read_the_users_coefficients_by_default(self):
        for name in ("run_eval", "compare", "e2e", "measure_speed", "day_run", "drafts", "draft_quality", "text_length"):
            src = (ROOT / "eval" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn("ignoring_overrides", src, name)
            self.assertIn("--user-thresholds", src, name)

    def test_setup_managed_puts_back_every_setting_install_writes(self):
        src = (ROOT / "tools" / "setup-managed.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$cfgKeys = @('backend', 'ado_org', 'ado_project')", src)
        self.assertIn("Restore-ConfigValues", src)
        self.assertIn("configValues", src)


if __name__ == "__main__":
    unittest.main()

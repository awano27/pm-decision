"""build-kev-bundle: one command that builds the Kev folder on the development PC.

The script runs only on Windows. -DryRun prints the eight steps and the four places they fetch from, and touches
nothing. Without git or uv it stops in one line before any download. The static checks run on every platform."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from . import hidden_words
except ImportError:
    import hidden_words

ROOT = Path(__file__).resolve().parent.parent
PS1 = ROOT / "tools" / "build-kev-bundle.ps1"
CMD = ROOT / "build-kev-bundle.cmd"
POWERSHELL = shutil.which("powershell")
HOSTS = ("github.com", "pypi.org", "download.pytorch.org", "huggingface.co")
WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32" and POWERSHELL,
                                   "Windows PowerShell runs tools/build-kev-bundle.ps1")


def run_script(args, path=None, stdin_text=""):
    """Runs the script with Windows PowerShell. `path` replaces PATH, so that git and uv cannot be found. `stdin_text`
    holds the answers to the [y/N] questions; the empty default is no answer, so Read-Host never waits for a human."""
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS1), *args],
                          input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, env=env)


@WINDOWS_ONLY
class TestDryRun(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.out = self.base / "kev-bundle"

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_dry_run_prints_each_of_the_eight_steps_once(self):
        r = run_script(["-DryRun", "-Out", str(self.out)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for n in range(1, 9):
            self.assertEqual(r.stdout.count(f"手順 {n}  "), 1, n)

    def test_dry_run_names_the_four_places_it_fetches_from(self):
        r = run_script(["-DryRun", "-Out", str(self.out)])
        for host in HOSTS:
            self.assertIn(host, r.stdout)

    def test_dry_run_creates_nothing_and_leaves_the_out_folder_alone(self):
        run_script(["-DryRun", "-Out", str(self.out)])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.base.iterdir()), [])

    def test_dry_run_does_not_need_git_or_uv_on_the_path(self):
        r = run_script(["-DryRun", "-Out", str(self.out)], path="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("前提が見つかりません", r.stdout)

    def test_dry_run_says_the_total_download_is_about_ten_gigabytes(self):
        r = run_script(["-DryRun", "-Out", str(self.out)])
        self.assertIn("約 10GB", r.stdout)


@WINDOWS_ONLY
class TestMissingPrerequisites(unittest.TestCase):
    def test_without_git_or_uv_it_stops_in_one_line_before_any_download(self):
        base = Path(tempfile.mkdtemp())
        try:
            out = base / "kev-bundle"
            r = run_script(["-Out", str(out)], path="")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            lines = [line for line in r.stdout.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("前提が見つかりません", lines[0])
            self.assertFalse(out.exists())
        finally:
            shutil.rmtree(base, ignore_errors=True)


@WINDOWS_ONLY
class TestTheScriptParses(unittest.TestCase):
    def test_the_script_parses_without_errors(self):
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                            "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                            f"'{PS1}', [ref]$t, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestTheScriptText(unittest.TestCase):
    src = PS1.read_text(encoding="utf-8-sig")

    def test_the_script_keeps_its_bom_and_the_launcher_is_ascii(self):
        self.assertTrue(PS1.read_bytes().startswith(b"\xef\xbb\xbf"),
                        "Windows PowerShell 5.1 reads Japanese text as UTF-8 only with a BOM")
        CMD.read_bytes().decode("ascii")

    def test_the_dry_run_exit_comes_before_any_download_or_tool_call(self):
        dry = self.src.index("if ($DryRun)")
        for call in ("Invoke-WebRequest", "git clone", "uv python", "pip install", "kev-models.py') fetch", "Copy-Item"):
            self.assertLess(dry, self.src.index(call), call)

    def test_the_big_download_is_asked_for_before_anything_is_fetched(self):
        ask = self.src.index("約 10GB を取得して")
        for step in ("uv python install", "pip install", "kev-models.py') fetch", "git clone"):
            self.assertGreater(self.src.index(step), ask, step)

    def test_the_script_takes_the_pip_marker_off_the_copy_before_the_first_pip_install(self):
        lines = self.src.splitlines()
        self.assertIn(r"$marker = Join-Path $Out 'python\Lib\EXTERNALLY-MANAGED'", self.src)
        removals = [i for i, line in enumerate(lines) if "Remove-Item" in line and "$marker" in line]
        robocopy = next(i for i, line in enumerate(lines) if "robocopy" in line)
        first_pip = next(i for i, line in enumerate(lines) if "-m pip install" in line)
        self.assertTrue(removals, "the marker is never removed")
        self.assertLess(robocopy, removals[0], "the marker must go from the copy, after robocopy")
        self.assertLess(removals[0], first_pip)

    def test_a_marker_that_stays_names_the_one_file_to_delete_instead_of_looping_back(self):
        self.assertNotIn("からやり直す", self.src)
        self.assertIn("そのファイルを手で消して", self.src)

    def test_the_script_finds_only_the_python_that_uv_manages(self):
        self.assertIn("uv python find --managed-python 3.13", self.src)
        self.assertNotIn("uv python find 3.13", self.src)

    def test_the_script_asks_before_it_sends_and_offers_yes_to_skip_the_questions(self):
        for needle in ("HEAD", "[y/N]", "-Yes", "-DryRun", "-NoStartCheck"):
            self.assertIn(needle, self.src)

    def test_the_defaults_are_the_documented_folders(self):
        self.assertIn(r"[string]$Out = 'C:\develop\kev-bundle'", self.src)
        self.assertIn(r"[string]$KevRepo = 'C:\develop\kev-local'", self.src)

    def test_every_documented_destination_is_named_in_the_script(self):
        for host in HOSTS:
            self.assertIn(host, self.src)

    def test_the_script_touches_no_kimeru_record_or_setting(self):
        for word in ("KIMERU_", "LOCALAPPDATA", "approvals", "kimeru\\out"):
            self.assertNotIn(word, self.src, word)

    def test_no_workplace_word_is_in_the_script_or_the_launcher(self):
        self.assertEqual(hidden_words.found(self.src + CMD.read_text(encoding="ascii")), [])


@WINDOWS_ONLY
@unittest.skipUnless(shutil.disk_usage(tempfile.gettempdir()).free >= 15 * 1024 ** 3,
                     "the script stops at its disk check first when the temp drive has less than 15GB free")
class TestAnsweringNoStopsBeforeAnyFetch(unittest.TestCase):
    def test_answering_no_to_the_first_question_exits_one_and_calls_no_git_or_uv(self):
        base = Path(tempfile.mkdtemp())
        try:
            out = base / "kev-bundle"
            stubs = base / "stubs"
            stubs.mkdir()
            called = base / "called.txt"
            for tool in ("git", "uv"):
                (stubs / f"{tool}.cmd").write_text(f'@echo off\r\necho {tool}>>"{called}"\r\nexit /b 0\r\n', encoding="ascii")
            system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            r = run_script(["-Out", str(out)], path=os.pathsep.join([str(stubs), str(system32)]), stdin_text="n\n")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("中止しました", r.stdout)
            self.assertFalse(out.exists(), "a folder was created before the answer")
            self.assertFalse(called.exists(), "git or uv was called before the answer")
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestTheLauncher(unittest.TestCase):
    def test_the_launcher_runs_the_script_with_its_arguments_and_waits(self):
        text = CMD.read_text(encoding="ascii")
        self.assertIn(r"tools\build-kev-bundle.ps1", text)
        self.assertIn("%*", text)
        self.assertIn("pause", text)


if __name__ == "__main__":
    unittest.main()

"""run-auto-check.cmd / tools/check-auto.ps1 / check.ps1 -Auto: nobody is asked, nothing is sent, Teams and Kev are not
started, and the public documents do not name the environment the author tested on."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401
try:
    from . import hidden_words
except ImportError:
    import hidden_words

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
AUTO = (ROOT / "tools" / "check-auto.ps1").read_bytes()


def _tracked(*patterns):
    """The files git tracks that match the patterns (a folder without git, such as the update zip, is searched instead)."""
    try:
        names = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        names = [p.relative_to(ROOT).as_posix() for pat in patterns for p in ROOT.rglob(pat)]
    return [n for n in names if (ROOT / n).is_file()]


class TestAutoMode(unittest.TestCase):
    def test_every_question_is_answered_no_in_auto_mode(self):
        block = CHECK[CHECK.index("if ($Auto) {"):]
        block = block[:block.index("\n}\n") + 3]
        self.assertIn("function YesNo($q)", block)
        self.assertRegex(block, r"function YesNo\(\$q\) \{[^}]*\$false \}")
        self.assertIn("function Read-Host", block)
        self.assertRegex(block, r"function Read-Host \{.*'' \}")
        # the override comes after the original definition, so it is the one every step calls
        self.assertLess(CHECK.index("function YesNo($q) { (Read-Host"), CHECK.index("if ($Auto) {"))

    def test_teams_is_not_started_and_the_manual_learning_is_skipped(self):
        start = CHECK.index("Start-Process 'explorer.exe' 'shell:AppsFolder\\MSTeams")
        guard = CHECK.index("if ($uia -and $Auto -and -not (Self 'status').teams)")
        self.assertLess(guard, start)
        self.assertIn("if (-not $selfOk -and $Auto) { Rec 'T3-learn' 'SKIP", CHECK)

    def test_the_auto_set_has_only_steps_that_send_nothing(self):
        line = next(l for l in CHECK.splitlines() if "$Only -contains 'auto'" in l)
        items = set(re.findall(r"'(T\d+)'", line))
        self.assertEqual(items, {"T1", "T7", "T8", "T12", "T13", "T14", "T16", "T23"})
        sending = {"T5", "T9", "T10", "T11", "T15", "T17", "T18", "T19", "T20", "T21", "T22", "T24"}
        self.assertFalse(items & sending)

    def test_the_launcher_and_the_script(self):
        cmd = (ROOT / "run-auto-check.cmd").read_bytes()
        cmd.decode("ascii")
        self.assertIn(rb"tools\check-auto.ps1", cmd)
        self.assertTrue(AUTO.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the BOM")
        text = AUTO.decode("utf-8-sig")
        self.assertIn("'check.ps1') -Auto auto", text)
        self.assertIn("check-update.ps1", text)
        self.assertNotIn("Invoke-WebRequest", text)
        self.assertNotIn("Start-Process", text)

    def test_files_left_by_an_older_version_are_named(self):
        release = (ROOT / "tools" / "release.ps1").read_text(encoding="utf-8-sig")
        self.assertIn(r"ls-files | Set-Content -Path (Join-Path $pack 'kimeru\FILES')", release)
        text = AUTO.decode("utf-8-sig")
        self.assertIn("Join-Path $root 'FILES'", text)
        self.assertIn("stale  NG", text)
        self.assertNotIn("Remove-Item", text)   # named, never deleted

    def test_the_result_files_are_not_committed(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for f in ("kimeru-check-result.txt", "kimeru-update-check-result.txt", "kimeru-auto-check-result.txt"):
            self.assertIn(f, ignored)


class TestTheAutomaticCheckSaysWhatItDoes(unittest.TestCase):
    # T3 may open the self chat, so the automatic check must not say that it opens no chat (AC3)
    NOT_OPENED = ("チャットを開かない", "チャットを開かず", "チャットを開きません")
    AUTO_MARKERS = ("自動の確認", "run-auto-check", "check-auto", "何も送らず", "何も送らない", "Nothing is sent")

    def test_the_banner_the_user_sees_says_the_self_chat_may_open(self):
        banner = next(l for l in AUTO.decode("utf-8-sig").splitlines() if "kimeru 自動の確認" in l)
        self.assertIn("何も送らない・貼らない・質問しない", banner)
        self.assertIn("自分とのチャットは開くことがあります", banner)
        for phrase in self.NOT_OPENED:
            self.assertNotIn(phrase, banner)

    def test_no_tracked_script_or_doc_says_the_automatic_check_does_not_open_a_chat(self):
        # CHANGELOG.md is not scanned: it quotes the old wording when it records the fix. Other lines about another
        # subject (the daily read of the self chat, sent_watch) are not about the automatic check and are not scanned.
        files = [f for f in _tracked("*.cmd", "*.ps1", "*.md") if f != "CHANGELOG.md"]
        self.assertIn("run-auto-check.cmd", files)
        for f in files:
            for line in (ROOT / f).read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if any(m in line for m in self.AUTO_MARKERS):
                    for phrase in self.NOT_OPENED:
                        self.assertNotIn(phrase, line, f)

    def test_the_three_files_of_the_automatic_check_say_the_self_chat_may_open(self):
        for f in ("run-auto-check.cmd", "tools/check-auto.ps1", "docs/managed-pc-check.md"):
            text = (ROOT / f).read_text(encoding="utf-8-sig", errors="replace")
            for phrase in self.NOT_OPENED:
                self.assertNotIn(phrase, text, f)
        self.assertIn("The self chat may be opened", (ROOT / "run-auto-check.cmd").read_text(encoding="utf-8-sig"))
        self.assertIn("自分とのチャットは開くことがあり", (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8-sig"))


class TestT18PutsTheSettingBack(unittest.TestCase):
    def test_the_no_rest_setting_sits_inside_a_try_whose_finally_puts_it_back(self):
        lines = CHECK.splitlines()
        saved = next(i for i, l in enumerate(lines) if l.startswith("$noRestPrev = "))
        set_at = next(i for i, l in enumerate(lines) if l.strip() == "$env:KIMERU_M365_NO_REST = '1'")
        try_at = max(i for i, l in enumerate(lines[:set_at]) if l.strip() == "try {")
        finally_at = next(i for i in range(set_at, len(lines)) if lines[i].strip() == "} finally {")
        self.assertLess(saved, try_at)
        self.assertLess(try_at, set_at)
        self.assertLess(set_at, finally_at)
        self.assertEqual([l for l in lines[try_at + 1:set_at] if l == "}"], [], "the setting must be inside the try")
        restore = "\n".join(lines[finally_at:finally_at + 3])
        self.assertIn("Remove-Item Env:KIMERU_M365_NO_REST", restore)
        self.assertIn("$env:KIMERU_M365_NO_REST = $noRestPrev", restore)


class TestTeamsWithoutAWindow(unittest.TestCase):   # N2: Teams that runs with no window is not "not started"
    OLD_SKIP = "SKIP（自動: Teams が起動していない。自動では起動しない）"

    def test_the_two_states_have_their_own_sentences(self):
        self.assertIn("Teams は起動しているが、ウィンドウが表示されていない", CHECK)
        self.assertIn("Teams が起動していない", CHECK)
        self.assertIn("function Teams-Down", CHECK)

    def test_the_skip_for_a_missing_window_is_not_the_old_sentence_alone(self):
        self.assertNotIn(self.OLD_SKIP, CHECK)
        guard = next(l for l in CHECK.splitlines() if "-not (Self 'status').teams) { $uia = $false; Rec 'T1-T5'" in l)
        self.assertIn("Teams-Down", guard)

    def test_the_manual_start_says_which_state_teams_is_in_before_it_starts_it(self):
        block = CHECK[CHECK.index('Say "Teams を起動"'):CHECK.index("Start-Process 'explorer.exe' 'shell:AppsFolder\\MSTeams")]
        self.assertIn("Teams-Down", block)

    def test_the_status_reports_the_process_and_the_window_apart(self):
        text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        status = next(l for l in text.splitlines() if "$Action -eq 'status'" in l)
        self.assertIn("teams = [bool]$w", status)   # the old field keeps its meaning
        self.assertIn("teams_process = ", status)
        self.assertIn("teams_window = [bool]$w", status)


class TestNoEnvironmentDetails(unittest.TestCase):

    def test_public_documents_do_not_name_the_tested_environment(self):
        try:
            files = subprocess.run(["git", "ls-files", "*.md", "*.ps1", "*.cmd", "*.yml"], cwd=ROOT, capture_output=True,
                                   text=True, encoding="utf-8", check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        bad = []
        for f in files:
            p = ROOT / f
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            bad += [f"{f}: {d}" for d in hidden_words.found(text)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

"""run-auto-check.cmd / tools/check-auto.ps1 / check.ps1 -Auto: nobody is asked, nothing is sent, Teams and Kev are not
started, and the public documents do not name the environment the author tested on."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
AUTO = (ROOT / "tools" / "check-auto.ps1").read_bytes()


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

    def test_the_result_files_are_not_committed(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for f in ("kimeru-check-result.txt", "kimeru-update-check-result.txt", "kimeru-auto-check-result.txt"):
            self.assertIn(f, ignored)


class TestNoEnvironmentDetails(unittest.TestCase):
    WORDS = ("会社 PC", "会社PC", "開発部", "26225", "別テナントの組織", "社内ルール", "社内ポリシー", "非公開の流入データ",
             "会議チャット 2 件", "事故", "リモートデスクトップ", "管理者同意は要りません", "テナント管理者の同意", "個人 PC で確認済み",
             "条件付きアクセス", "別のテナントにある場合", "作者の 2 台", "特定の職場")

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
            for w in self.WORDS:
                if w in text:
                    bad.append(f"{f}: {w}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the identity rules in tools/teams-copilot.ps1.

The script drives the real Teams window, so it cannot run here. What can be checked is the pure part: the patterns that
decide "this is the Copilot compose box" / "this is a meeting chat". A request once went to a meeting chat because these
were too loose; the cases below are the ones that must never pass again.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kimeru import writer

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "teams-copilot.ps1"
POWERSHELL = shutil.which("powershell")


def _ps_literal(name):
    """The single-quoted value assigned to $name in the script."""
    text = SCRIPT.read_text(encoding="utf-8-sig")
    m = re.search(r"^\$" + name + r" = '([^\n]*)'", text, re.M)
    assert m, name
    return m.group(1)


def _ps_matches(pattern, names):
    """Which of `names` the .NET regex `pattern` matches, evaluated by PowerShell itself."""
    items = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    code = "\n".join([
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "$p = '" + pattern.replace("'", "''") + "'",
        "foreach ($n in @(" + items + ")) { if ($n -match $p) { '1' } else { '0' } }",
        "",
    ])
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.ps1"
        f.write_text(code, encoding="utf-8-sig")   # BOM: Windows PowerShell 5.1 reads it as UTF-8
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    return [x.strip() == "1" for x in r.stdout.strip().splitlines()]


@unittest.skipUnless(POWERSHELL, "PowerShell not available")
class TestComposeBoxIdentity(unittest.TestCase):
    def test_only_copilots_own_box_matches(self):
        pattern = _ps_literal("BOXRX")
        good = ["Copilot にメッセージを送信する", "Copilot に質問する", "Message Copilot", "Ask Copilot"]
        bad = [
            "メッセージを入力",                       # every chat's placeholder (a meeting chat got a request that way)
            "Copilot で検索または質問する (Ctrl+E)",   # the top search box
            "Type a message",
            "新しいメッセージ",
            "",
        ]
        got = _ps_matches(pattern, good + bad)
        self.assertEqual(got, [True] * len(good) + [False] * len(bad))

    def test_copilot_row_pattern(self):
        pattern = _ps_literal("COPILOT")
        good = ["Copilot", "Microsoft 365 Copilot", "Copilot (アプリ)"]
        bad = ["LINE連携プロジェクト", "【診療支援】デイリー朝会", "Copilot活用チーム", "診療支援Daily comm"]
        got = _ps_matches(pattern, good + bad)
        self.assertEqual(got, [True] * len(good) + [False] * len(bad))


class TestScriptGuardsPresent(unittest.TestCase):
    """The safety steps must stay in the script (a refactor that drops one is caught here)."""

    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8-sig")

    def test_every_write_is_preceded_by_identity_checks(self):
        for needle in ("Test-CopilotPane", "Focus-Box", "Test-FocusOn", "Enter-UiLock", "Wait-UserIdle", "-DryRun"):
            self.assertIn(needle, self.text, needle)
        # the pane check runs before the paste and again right before the send
        self.assertGreaterEqual(self.text.count("Test-CopilotPane"), 4)

    def test_no_position_guess_for_the_compose_box_when_asking(self):
        # the strict branch returns before the loose "lowest editable field" guess
        # every action except the read-only probe is strict (pastetest once fell into the loose branch and picked
        # the composer of another chat)
        self.assertIn("if ($Strict -or $Action -ne 'probe')", self.text)
        self.assertNotIn("$Action -eq 'ask')", self.text.split("function Find-Box")[1].split("function Get-Box")[0])
        strict = self.text.index("if ($Strict -or $Action -ne 'probe')")
        loose = self.text.index("Sort-Object { $_.Current.BoundingRectangle.Y } | Select-Object -Last 1")
        self.assertLess(strict, loose)

    def test_diag_file_is_not_a_stringified_array(self):
        # the first version joined "$head, $rows" and wrote "System.Object[]" instead of the layout
        self.assertNotIn("(($head, $rows) -join", self.text)
        self.assertIn("@($head) + @($script:FocusTrace", self.text)

    def test_dpi_awareness_is_set_before_any_ui_work(self):
        self.assertIn("SetProcessDpiAwarenessContext", self.text)
        self.assertLess(self.text.index("SetProcessDpiAwarenessContext"), self.text.index("function Get-TeamsWindow"))

    def test_focus_check_accepts_text_or_value_pattern_elements(self):
        i = self.text.index("function Test-FocusOn")
        body = self.text[i:i + 1400]
        self.assertIn("TextPattern", body)
        self.assertIn("ValuePattern", body)

    def test_focus_trace_is_recorded(self):
        self.assertIn("Describe-Focus 'afterClick'", self.text)

    def test_diagnostics_are_content_free(self):
        # names are only ever printed for a fixed list of UI words; everything else becomes its length
        self.assertIn('"<$($n.Length)>"', self.text)


class TestCitations(unittest.TestCase):
    def test_inline_references_are_removed(self):
        d = {"a1": "受領しました。[1] 確認します【1†source】。", "memo": {"options": ["案A^2^", "案B[[3]]"]}}
        got = writer.strip_citations(d)
        self.assertEqual(got["a1"], "受領しました。 確認します。")
        self.assertEqual(got["memo"]["options"], ["案A", "案B"])

    def test_ordinary_numbering_is_kept(self):
        self.assertEqual(writer.strip_citations("手順(1)を確認し、v2.3 を出す"), "手順(1)を確認し、v2.3 を出す")

    def test_source_given_as_object(self):
        # a source may come back as {"title": ...}; it is shown by its title, not as a dict repr
        self.assertEqual(writer._item_text({"title": "デイリー朝会", "type": "meeting"}), "デイリー朝会：meeting")


if __name__ == "__main__":
    unittest.main()

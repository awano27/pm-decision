"""N4: the managed-PC check has three reading items for the reply words that only a real Teams can show. T25: 停止 and 再開 in my own
line of the chat list. T26: a short message I sent to a 1:1 chat, seen in the list. T27: the reply words under a test post.
They run by hand only (-Auto gives SKIP), open and read no other chat, change no approval state, and keep counts, words and seconds."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import re
import unittest
from pathlib import Path

from kimeru import control, notify

ROOT = Path(__file__).resolve().parent.parent
CHECK = (ROOT / "tools" / "check.ps1").read_text(encoding="utf-8-sig")
AUTO = (ROOT / "tools" / "check-auto.ps1").read_text(encoding="utf-8-sig")
MANUAL = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8")
SELF_READER = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
ITEMS = ("T25", "T26", "T27")
# the variables a result row of these items may use: counts, states, seconds and fixed words, never a message or a row of the list
ROW_VARIABLES = {"ok", "readErr", "found", "waited", "row", "tot", "asIs", "edited", "other", "posted", "hits", "want", "missTxt", "via"}


def _block(item):
    """The check.ps1 lines of one item: from its header to the next header."""
    start = CHECK.index(f"# ---- {item} ")
    end = CHECK.find("\n# ---- ", start + 5)
    return CHECK[start:] if end < 0 else CHECK[start:end]


def _code(block):
    """The lines of a block that are code (not comments, not blank)."""
    return [line for line in block.splitlines() if line.strip() and not line.strip().startswith("#")]


class TestTheReadingItemsRunByHandOnly(unittest.TestCase):
    def test_each_item_has_its_own_branch_gated_by_its_name(self):
        for item in ITEMS:
            self.assertIn(f"if (Want '{item}')", CHECK, item)

    def test_each_item_is_skipped_in_auto_mode_before_it_does_anything(self):
        for item in ITEMS:
            block = _block(item)
            self.assertIn("if ($Auto)", block, item)
            first_action = min(block.index(call) for call in ("Self '", "Py @(", "YesNo") if call in block)
            self.assertLess(block.index("if ($Auto)"), first_action, item)
            self.assertIn(f"Rec '{item}' 'SKIP（自動:", block, item)

    def test_no_item_opens_or_reads_another_chat(self):
        for item in ITEMS:
            code = "\n".join(_code(_block(item)))
            self.assertNotIn("readchat", code, item)
            self.assertNotIn("Self 'open'", code, item)
            self.assertNotIn("-ChatId", code, item)

    def test_no_item_touches_the_approval_state_or_the_daily_cycle(self):
        for item in ITEMS:
            code = "\n".join(_code(_block(item)))
            for word in ("approvals", "daily", "notify", "--send", "KIMERU_EXECUTE", "Invoke-WebRequest", "Start-Process"):
                self.assertNotIn(word, code, f"{item} must not use {word}")

    def test_t27_posts_exactly_one_test_post_that_starts_with_the_test_mark(self):
        block = _block("T27")
        self.assertEqual(block.count("Self 'post'"), 1)
        self.assertRegex(block, r"Self 'post' @\('-Text', \"\[kimeru 試験 #\$n\]")

    def test_no_item_takes_a_chat_list_row_for_anything_but_a_count_or_a_word(self):
        # a message or a row of the list may be read into a variable, never into a result row
        for item in ITEMS:
            for line in _code(_block(item)):
                if f"Rec '{item}'" not in line:
                    continue
                arguments = line[line.index(f"Rec '{item}'"):]   # the result row only, not the `if` that guards it
                names = set(re.findall(r"\$([A-Za-z_]\w*)", arguments))
                self.assertLessEqual(names, ROW_VARIABLES, f"{item}: {line.strip()}")

    def test_the_python_snippets_of_t25_and_t26_print_counts_as_json_and_never_a_text(self):
        helpers = CHECK[CHECK.index("function Control-Word"):CHECK.index('Write-Host "kimeru 管理PCチェック')]
        calls = [line for line in helpers.splitlines() if "'-c'" in line]
        self.assertEqual(len(calls), 2)   # Control-Word (T25) and Sent-Classes (T26)
        for line in calls:
            self.assertIn("json.dumps(", line)
            self.assertNotIn('"', line, "a double quote breaks the -c argument on Windows PowerShell 5.1")
            self.assertEqual(re.findall(r"print\((\w+)", line), ["json"])

    def test_the_python_snippets_use_only_the_judgments_of_kimeru(self):
        self.assertIn("control.line_of", CHECK)
        self.assertIn("sent.classify", CHECK)
        self.assertIn("Control-Word", _block("T25"))
        self.assertIn("Sent-Classes", _block("T26"))
        self.assertNotIn("sent.observe", CHECK)   # observe writes the approval state; the check only classifies


class TestTheResultsCountWordsAndSecondsOnly(unittest.TestCase):
    def test_the_t25_row_names_the_two_words_and_the_seconds_waited(self):
        block = _block("T25")
        self.assertIn("停止", block)
        self.assertIn("再開", block)
        self.assertIn("認識", block)
        self.assertIn("待った", block)

    def test_the_t26_row_names_the_counts_of_the_three_states(self):
        block = _block("T26")
        for label in ("as_is=", "edited=", "other=", "件数="):
            self.assertIn(label, block)

    def test_the_t27_row_names_the_seven_words_by_their_word_only(self):
        block = _block("T27")
        for word in ("違う", "保留", "不要", "着手", "完了", "延期", "送った"):
            self.assertIn(word, block, word)
        self.assertIn("分類=", block)


class TestTheReplyWordsAreTheOnesKimeruAndTheReaderKnow(unittest.TestCase):
    def test_kimeru_reads_each_of_the_seven_words_under_its_number(self):
        for line in ("違う 523", "保留 523 明日", "不要 523", "着手 523", "完了 523 1", "延期 523 明日", "送った 523"):
            self.assertEqual(str(notify._reply_number(line)), "523", line)

    def test_kimeru_reads_the_word_of_t25_as_a_control_word_of_my_own_line(self):
        self.assertEqual(control.line_of([{"kind": "self", "preview": "あなた: 停止"}])[1], "停止")
        self.assertEqual(control.line_of([{"kind": "self", "preview": "あなた: 再開"}])[1], "再開")
        self.assertIsNone(control.line_of([{"kind": "self", "preview": "あなた: 停止中です"}])[1])

    def test_the_reader_hands_back_the_seven_words_in_the_form_t27_expects(self):
        for pattern in (r"違う\s*#?(\d+)", r"保留\s*#?(\d+)\s+(\S{1,10})", r"不要", "(着手|完了)", r"延期\s*#?(\d+)\s+(\S{1,10})",
                        r"送った\s*#?(\d+)"):
            self.assertIn(pattern, SELF_READER, pattern)
        self.assertIn("'R:延期 {0} {1}'", SELF_READER)
        self.assertIn("'R:送った {0}'", SELF_READER)

    def test_t27_expects_the_reader_form_of_each_word_under_the_random_number(self):
        block = _block("T27")
        for expected in ('"R:違う $n"', '"R:保留 $n 明日"', '"R:不要 $n"', '"R:着手 $n"', '"R:完了 $n 1"', '"R:延期 $n 明日"',
                         '"R:送った $n"'):
            self.assertIn(expected, block, expected)
        self.assertIn("Get-Random -Minimum 100 -Maximum 999", block)   # 100 to 998, as in T4 and T5


class TestTheDocsAndTheAutoListNameTheItems(unittest.TestCase):
    def test_the_auto_check_manual_list_names_each_item_by_its_command(self):
        for item in ITEMS:
            self.assertIn(f"run-check.cmd {item}", AUTO, item)

    def test_the_manual_has_a_section_for_each_item_and_the_run_command_has_the_prefix(self):
        for item in ITEMS:
            self.assertRegex(MANUAL, rf"(?m)^### {item} ", item)
            self.assertIn(f".\\run-check.cmd {item}", MANUAL, item)

    def test_the_result_sheet_has_a_line_for_each_item(self):
        sheet = MANUAL[MANUAL.index("## 結果シート"):]
        for item in ITEMS:
            self.assertRegex(sheet, rf"(?m)^{item} OK/NG", item)

    def test_the_list_of_items_that_leave_the_pc_names_the_reading_items(self):
        start = MANUAL.index("PC の外へ出る項目は次のとおり")
        block = MANUAL[start:MANUAL.index("\n- 本文・人名・トークンは結果シートに書かない", start)]
        for item in ITEMS:
            self.assertRegex(block, rf"\b{item}\b", item)


if __name__ == "__main__":
    unittest.main()

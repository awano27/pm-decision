"""Regression tests for the identity rules in tools/teams-copilot.ps1.

The script drives the real Teams window, so it cannot run here. What can be checked is the pure part: the patterns that
decide "this is the Copilot compose box" / "this is a meeting chat". A request once went to a meeting chat because these
were too loose; the cases below are the ones that must never pass again.
"""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

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

    def test_named_composer_is_identified_by_element_not_by_rectangle(self):
        # after a long paste the composer grows and can scroll out of view (y = -562 on the real Teams): its old
        # rectangle proves nothing, so the focus check compares the element itself
        i = self.text.index("function Test-FocusOn")
        body = self.text[i:i + 1600]
        self.assertIn("GetRuntimeId", body)
        self.assertLess(body.index("GetRuntimeId"), body.index("IntersectsWith"))

    def test_clicks_stay_inside_the_teams_window(self):
        i = self.text.index("function Click(")
        self.assertIn("Contains($cx, $cy)", self.text[i:i + 900])

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


class TestCheckRunsIgnoreTheRest(unittest.TestCase):
    def test_managed_check_resets_the_rest_before_a_deliberate_attempt(self):
        text = (SCRIPT.parent / "check.ps1").read_text(encoding="utf-8-sig")
        i = text.index("# ---- T18:")
        self.assertIn("KIMERU_M365_NO_REST", text[i:i + 3000])
        self.assertIn("m365-auto.json", text[i:i + 3000])


# ---------------------------------------------------------------------------------------------------------------------
# Synthetic UI: the production functions of the script (taken out with the PowerShell parser, see
# tests/synthetic_copilot_ui.ps1) run against in-memory UIA-shaped trees. Teams is never started or touched. This shows
# how the identity / focus / equality / send logic behaves on the layouts below; it does NOT show that a real Teams
# window looks like these trees (that is the real-environment check in docs/managed-pc-check.md, NOT_RUN here).
# ---------------------------------------------------------------------------------------------------------------------
import json  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "synthetic_copilot_ui.ps1"
PROMPT = "あなたはテスト用の依頼文です。本文は架空です。（JSON だけ）"
TARGET = "COP-box"

# (layout, mutation): what the screen looks like, and what changes while the operation runs
SCENARIOS = [
    # layouts
    ("full", "none"), ("panel", "none"), ("meeting", "none"), ("meeting-nobutton", "none"),
    ("meeting-unnamed", "none"), ("unknown-other-chat", "none"), ("ambiguous-two-named", "none"),
    ("unnamed-two-editors", "none"), ("unnamed-single", "none"), ("other-chat-navigable", "none"),
    # the screen changing mid-operation (with a second compose box on screen)
    ("meeting", "target-gone-before-paste"), ("meeting", "second-copilot-box-appears"), ("meeting", "paste-mangled"),
    ("meeting", "focus-stolen-before-paste"), ("meeting", "focus-stolen-after-paste"),
    ("meeting", "focus-stolen-after-paste-locked"), ("meeting", "target-gone-after-paste"),
    ("meeting", "late-target-gone"), ("meeting", "late-second-copilot"), ("meeting", "late-text-changed"), ("meeting-nobutton", "late-focus-stolen"),
    ("meeting-nobutton", "late-focus-stolen-locked"), ("meeting-nobutton", "late-target-gone"),
    ("meeting", "late-focus-stolen"), ("panel", "late-focus-stolen-locked"),
    # a draft of the person / a leftover of ours in the Copilot box
    ("full", "draft-in-box"), ("full", "stale-kimeru-text"),
]


def _run_synthetic(script_path):
    cases = [{"id": f"{lay}/{mut}", "layout": lay, "mutation": mut, "prompt": PROMPT} for lay, mut in SCENARIOS]
    with tempfile.TemporaryDirectory() as d:
        h, sc = Path(d) / "h.ps1", Path(d) / "sc.json"
        h.write_text(HARNESS.read_text(encoding="utf-8"), encoding="utf-8-sig")   # BOM: Windows PowerShell 5.1 reads it as UTF-8
        sc.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(h),
                            "-ScriptPath", str(script_path), "-ScenarioFile", str(sc)],
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            o = json.loads(line)
            out[o["id"]] = o
    if len(out) != len(cases):
        raise AssertionError(f"harness gave {len(out)}/{len(cases)} results: {r.stderr[:500]} {r.stdout[-500:]}")
    for o in out.values():
        if "harnessError" in o:
            raise AssertionError(f"{o['id']}: {o['harnessError']}")
    return out


@unittest.skipUnless(POWERSHELL, "PowerShell not available")
class TestSyntheticLayouts(unittest.TestCase):
    """Synthetic UI only. Rule under test: if the Copilot composer cannot be confirmed as the one and only target,
    nothing is sent; whatever is sent goes to the Copilot composer and nowhere else."""

    @classmethod
    def setUpClass(cls):
        cls.r = _run_synthetic(SCRIPT)

    def get(self, layout, mutation="none"):
        return self.r[f"{layout}/{mutation}"]

    def assertSentOnlyToCopilot(self, o):
        self.assertEqual(o["sent"], [f"{TARGET}|{PROMPT}"], o)

    def assertNothingSent(self, o):
        self.assertTrue(o["failed"], o)
        self.assertEqual(o["sent"], [], o)
        self.assertFalse([k for k in o["keys"] if k.startswith(("{ENTER}", "^{ENTER}"))], o)

    def assertOthersUntouched(self, o):
        for tag, text in o["texts"].items():
            if tag != TARGET:
                self.assertTrue(text, f"{tag} lost its text: {o}")   # the other chat's draft is still there

    def assertKeysOnlyInTarget(self, o):
        self.assertEqual([k for k in o["keys"] if not k.endswith("@" + TARGET)], [], o)

    # ---- layouts that can be confirmed: the request reaches Copilot and only Copilot
    def test_copilot_full_screen(self):
        o = self.get("full")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertEqual(o["keys"], [f"^v@{TARGET}"])      # the button sent it; no key but the paste

    def test_copilot_right_side_panel_next_to_another_chat_with_a_draft(self):
        # the other chat's Send button sits right beside the panel and comes first in tree order
        o = self.get("panel")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertOthersUntouched(o)

    def test_meeting_chat_coexisting_two_compose_boxes(self):
        o = self.get("meeting")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertOthersUntouched(o)
        self.assertKeysOnlyInTarget(o)

    def test_send_button_of_another_chat_is_never_used(self):
        # Copilot's own Send button is not exposed, the meeting chat's (with its draft) is: Enter inside the
        # focus-checked Copilot box is used instead of the foreign button (the original script invoked the foreign one)
        o = self.get("meeting-nobutton")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertOthersUntouched(o)
        self.assertEqual(o["keys"], [f"^v@{TARGET}", f"{{ENTER}}@{TARGET}"])

    def test_another_chat_on_screen_is_navigated_to_copilot_before_anything_is_pasted(self):
        # the title says Copilot but another chat (with a draft) is shown: the Copilot row is selected, then the new
        # composer is confirmed; the draft of the other chat is never touched
        o = self.get("other-chat-navigable")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertOthersUntouched(o)
        self.assertKeysOnlyInTarget(o)

    def test_unnamed_composer_is_accepted_only_when_the_pane_is_proven_and_alone(self):
        o = self.get("unnamed-single")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)

    # ---- target unknown / ambiguous: nothing is pasted, nothing is sent
    def test_unconfirmed_targets_get_nothing(self):
        for layout in ("meeting-unnamed", "unknown-other-chat", "ambiguous-two-named", "unnamed-two-editors"):
            with self.subTest(layout=layout):
                o = self.get(layout)
                self.assertNothingSent(o)
                self.assertEqual(o["keys"], [], o)          # not even a paste
                if layout != "unknown-other-chat":          # (there the Copilot box is the other chat's own box)
                    self.assertEqual(o["texts"][TARGET], "", o)
                if layout in ("meeting-unnamed", "unknown-other-chat"):
                    self.assertOthersUntouched(o)

    def test_unknown_target_reports_why(self):
        self.assertIn("compose box not found", self.get("unknown-other-chat")["error"])
        self.assertIn("not the only one", self.get("ambiguous-two-named")["error"])

    def test_person_draft_in_the_copilot_box_is_left_alone(self):
        o = self.get("full", "draft-in-box")
        self.assertNothingSent(o)
        self.assertEqual(o["keys"], [])
        self.assertEqual(o["texts"][TARGET], "Private draft that is not kimeru text")

    def test_leftover_of_our_own_request_may_be_cleared_then_sent(self):
        o = self.get("full", "stale-kimeru-text")
        self.assertFalse(o["failed"], o)
        self.assertSentOnlyToCopilot(o)
        self.assertEqual(o["keys"][:2], [f"^a@{TARGET}", f"{{DEL}}@{TARGET}"])

    # ---- the screen changes while the operation runs
    def test_target_disappears_before_the_paste(self):
        o = self.get("meeting", "target-gone-before-paste")
        self.assertNothingSent(o)
        self.assertEqual(o["keys"], [], o)
        self.assertOthersUntouched(o)

    def test_second_copilot_box_appears_before_the_paste(self):
        o = self.get("meeting", "second-copilot-box-appears")
        self.assertNothingSent(o)
        self.assertEqual(o["keys"], [], o)

    def test_focus_is_in_another_box_before_the_paste(self):
        o = self.get("meeting", "focus-stolen-before-paste")
        self.assertNothingSent(o)
        self.assertEqual(o["keys"], [], o)                  # the paste would have gone to the meeting chat
        self.assertOthersUntouched(o)

    def test_paste_that_does_not_equal_the_prompt_is_removed_not_sent(self):
        o = self.get("meeting", "paste-mangled")
        self.assertNothingSent(o)
        self.assertKeysOnlyInTarget(o)
        self.assertEqual(o["texts"][TARGET], "")
        self.assertOthersUntouched(o)

    def test_focus_moves_to_another_box_right_after_the_paste(self):
        o = self.get("meeting", "focus-stolen-after-paste")
        self.assertNothingSent(o)
        self.assertKeysOnlyInTarget(o)                      # the focus was taken back before any ^a / DEL
        self.assertOthersUntouched(o)
        locked = self.get("meeting", "focus-stolen-after-paste-locked")
        self.assertNothingSent(locked)
        self.assertEqual(locked["keys"], [f"^v@{TARGET}"], locked)   # focus cannot be restored: no further key at all
        self.assertOthersUntouched(locked)

    def test_target_disappears_after_the_paste(self):
        o = self.get("meeting", "target-gone-after-paste")
        self.assertNothingSent(o)
        self.assertEqual(o["keys"], [f"^v@{TARGET}"], o)
        self.assertOthersUntouched(o)

    def test_target_disappears_between_the_last_check_and_the_send(self):
        for layout in ("meeting", "meeting-nobutton"):
            with self.subTest(layout=layout):
                o = self.get(layout, "late-target-gone")
                self.assertNothingSent(o)
                self.assertOthersUntouched(o)

    def test_focus_moves_between_the_last_check_and_the_send(self):
        locked = self.get("meeting-nobutton", "late-focus-stolen-locked")
        self.assertNothingSent(locked)                      # Enter would have gone to the meeting chat
        self.assertEqual(locked["keys"], [f"^v@{TARGET}"], locked)
        self.assertOthersUntouched(locked)
        # a Send button that belongs to the composer does not depend on the focus
        by_button = self.get("panel", "late-focus-stolen-locked")
        self.assertFalse(by_button["failed"], by_button)
        self.assertSentOnlyToCopilot(by_button)
        # restorable focus: it is put back first, so Enter lands in the Copilot box
        back = self.get("meeting-nobutton", "late-focus-stolen")
        self.assertFalse(back["failed"], back)
        self.assertSentOnlyToCopilot(back)
        self.assertEqual(back["keys"][-1], f"{{ENTER}}@{TARGET}")
        self.assertOthersUntouched(back)
        self.assertSentOnlyToCopilot(self.get("meeting", "late-focus-stolen"))

    def test_target_and_text_are_rechecked_after_send_button_discovery(self):
        for mutation in ("late-second-copilot", "late-text-changed"):
            with self.subTest(mutation=mutation):
                o = self.get("meeting", mutation)
                self.assertNothingSent(o)
                self.assertEqual(o["keys"], [f"^v@{TARGET}"], o)
                self.assertOthersUntouched(o)
                if mutation == "late-text-changed":
                    self.assertEqual(o["texts"][TARGET], "User changed this draft")

    def test_no_key_ever_goes_to_another_box_and_nothing_else_is_sent(self):
        for key, o in self.r.items():
            with self.subTest(case=key):
                if key.split("/")[1] in ("focus-stolen-before-paste", "focus-stolen-after-paste-locked") or key.startswith("unknown-other-chat"):
                    continue   # (the focus is deliberately elsewhere; these cases are asserted above)
                self.assertKeysOnlyInTarget(o)
                for s in o["sent"]:
                    self.assertTrue(s.startswith(TARGET + "|"), o)


class TestSendButtonIsTiedToTheComposer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8-sig")

    def test_the_send_button_comes_from_the_composer_bound_helper(self):
        self.assertIn("function Get-SendButton", self.text)
        self.assertIn("$btn = Get-SendButton $w $script:BoxRect", self.text)
        # the old "first enabled Send button in the whole window" search is gone
        self.assertNotIn("Find-All $w $CT::Button | Where-Object { $_.Current.Name -match '^(送信|Send)(\\s*\\(|$)' -and $_.Current.IsEnabled } | Select-Object -First 1", self.text)

    def test_enter_is_only_pressed_after_the_focus_check(self):
        i = self.text.index("if (-not $btn) {")
        line = self.text[i:self.text.index("\n", i)]
        self.assertLess(line.index("Focus-Box"), line.index("{ENTER}"))

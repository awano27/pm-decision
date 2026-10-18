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

import os
import re
import shutil
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
        sending = {"T5", "T9", "T10", "T11", "T15", "T17", "T18", "T19", "T20", "T21", "T22", "T24", "T25", "T26", "T27"}
        self.assertFalse(items & sending)

    def test_the_reading_items_of_n4_are_not_in_the_auto_set_and_are_listed_for_a_person(self):   # N4: T25 to T27 need a person
        line = next(l for l in CHECK.splitlines() if "$Only -contains 'auto'" in l)
        for item in ("T25", "T26", "T27"):
            self.assertNotIn(f"'{item}'", line)
        for item in ("T25", "T26", "T27"):
            self.assertIn(f"run-check.cmd {item}", AUTO.decode("utf-8-sig"))

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


class TestTheAutomaticCheckOpensNoChat(unittest.TestCase):   # N5: the self chat is read only when it is already open
    MAY_OPEN = ("開くことがあり", "may be opened", "brought to the front", "前面に出す")
    AUTO_MARKERS = ("自動の確認", "run-auto-check", "check-auto", "何も送らず", "何も送らない", "Nothing is sent")

    def t3_block(self):
        start = CHECK.index('Say "T3 自分とのチャット"')
        return CHECK[start:CHECK.index("if (-not $selfOk -and $Auto) { Rec 'T3-learn'", start)].splitlines()

    def test_the_banner_the_user_sees_says_no_chat_is_opened(self):
        banner = next(l for l in AUTO.decode("utf-8-sig").splitlines() if "kimeru 自動の確認" in l)
        self.assertIn("何も送らない・チャットを開かない・質問しない", banner)
        for phrase in self.MAY_OPEN:
            self.assertNotIn(phrase, banner)

    def test_no_tracked_script_or_doc_says_the_automatic_check_may_open_a_chat(self):
        # CHANGELOG.md is not scanned: it quotes the wording of each fix. Other lines (the diagnose read, the daily read) are not about
        # the automatic check and are not scanned.
        files = [f for f in _tracked("*.cmd", "*.ps1", "*.md") if f != "CHANGELOG.md"]
        self.assertIn("run-auto-check.cmd", files)
        for f in files:
            for line in (ROOT / f).read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if any(m in line for m in self.AUTO_MARKERS):
                    for phrase in self.MAY_OPEN:
                        self.assertNotIn(phrase, line, f)

    def test_the_three_files_of_the_automatic_check_say_no_chat_is_opened(self):
        self.assertIn("no chat is opened", (ROOT / "run-auto-check.cmd").read_text(encoding="utf-8-sig"))
        self.assertIn("no chat is opened", AUTO.decode("utf-8-sig"))
        doc = (ROOT / "docs" / "managed-pc-check.md").read_text(encoding="utf-8-sig")
        self.assertIn("チャットを開かず", doc)
        self.assertIn("T3 は SKIP", doc)

    def test_the_automatic_branch_of_t3_never_calls_open_and_reads_only_an_open_self_chat(self):
        lines = self.t3_block()
        auto = lines[lines.index("  if ($Auto) {"):lines.index("  } else {")]
        self.assertFalse(any("Self 'open'" in l for l in auto))
        self.assertTrue(any("Self 'read'" in l for l in auto))
        self.assertTrue(any("self_open" in l for l in auto))
        self.assertTrue(any("Self 'open'" in l for l in lines[lines.index("  } else {"):]), "the manual check still opens it")

    def test_an_automatic_check_without_an_open_self_chat_records_a_skip_that_names_the_manual_steps(self):
        self.assertIn("Rec 'T3' 'SKIP（自動: 自分とのチャットが開いていない。手で開いてから再実行するか run-check.cmd T3）'", CHECK)

    def test_the_list_read_of_t23_runs_in_the_automatic_check_too(self):
        self.assertIn("if ($uia -and ($selfOk -or $Auto) -and (Want 'T23')) {", CHECK)

    def test_status_says_whether_the_self_chat_is_open_and_the_fake_reader_says_so_too(self):
        text = (ROOT / "tools" / "teams-self.ps1").read_text(encoding="utf-8-sig")
        status = next(l for l in text.splitlines() if "$Action -eq 'status'" in l)
        self.assertIn("self_open = $so", status)
        self.assertIn("selfChatOpen = $so", status)
        fake = (ROOT / "tests" / "fake-teams-self.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("self_open = $win", fake)

    def test_the_scripts_of_the_automatic_check_parse_without_errors(self):
        if os.name != "nt" or not shutil.which("powershell"):
            self.skipTest("needs Windows PowerShell")
        for f in ("tools/check.ps1", "tools/teams-self.ps1", "tools/check-auto.ps1"):
            cmd = ("& { param($f) $e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);"
                   "if($e){$e|%{$_.Message};exit 1} } '" + str(ROOT / f) + "'")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f + r.stdout + r.stderr)


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


class TestT5SaysWhenTheWindowIsMissing(unittest.TestCase):   # the tray-only Teams must not end as "window not found"
    SENTENCE = ("Teams のウィンドウが表示されていません（起動はしています）。"
                "ウィンドウを開いてチャット一覧を表示してから run-check.cmd T5 をやり直してください")

    def test_the_sentence_is_in_the_check_script(self):
        self.assertIn(self.SENTENCE, CHECK)
        self.assertIn("Teams window not found", CHECK)   # the raw error is mapped, not dropped

    def test_status_is_read_before_the_t5_post_and_the_send_is_skipped(self):
        block = CHECK[CHECK.index('Say "T4/T5 貼り付け・送信・iPhone 返信"'):]
        status_at = block.index("Self 'status'")
        post_at = block.index("Self 'post'")
        self.assertLess(status_at, post_at)
        self.assertIn("teams_process", block[status_at:post_at])
        self.assertIn("teams_window", block[status_at:post_at])


class TestT5WaitsForAReplyFromThePcOrThePhone(unittest.TestCase):   # a reply typed on the PC, and one that comes after the window
    BLOCK_FROM = 'Say "T4/T5 貼り付け・送信・iPhone 返信"'

    def _block(self):
        return CHECK[CHECK.index(self.BLOCK_FROM):]

    def test_the_prompt_and_the_question_name_the_pc_as_well(self):
        block = self._block()
        self.assertIn("PC か iPhone の Teams で、自分とのチャットに「OK $n」と返信してください（全角でも可。最大 $t5Wait 秒待ちます）", block)
        self.assertIn("返信しましたか（PC でも iPhone でも）？ [y/n]", block)
        self.assertNotIn("iPhone から返信しましたか", CHECK)   # the old question made a PC user answer n
        self.assertIn("iPhone に通知は届きましたか？ [y/n]", block)   # the notification question stays

    def test_the_wait_is_300_seconds_unless_the_variable_says_otherwise(self):
        block = self._block()
        self.assertIn("$t5Wait = 300", block)
        self.assertIn("KIMERU_T5_WAIT_SEC", block)
        env_at = block.index("KIMERU_T5_WAIT_SEC")
        self.assertIn("-ge 30", block[env_at:env_at + 400])
        self.assertIn("-le 900", block[env_at:env_at + 400])
        self.assertIn("WaitReply $n 'OK' $t5Wait", block)
        self.assertIn("[int]$WaitSec = 180", CHECK)   # the other steps keep -WaitSec

    def test_the_wait_says_it_is_waiting_and_says_when_the_reply_is_read(self):
        wait = CHECK[CHECK.index("function WaitReply"):]
        wait = wait[:wait.index("\n}\n")]
        self.assertIn("返信を待っています… 残り", wait)
        self.assertIn("AddSeconds(30)", wait)
        self.assertIn("返信を読み取りました", wait)
        self.assertLess(wait.index("ReplySeen $id $word"), wait.index("返信を読み取りました"))

    def test_a_reply_that_comes_after_the_window_is_read_once_more_and_recorded(self):
        block = self._block()
        wait_at = block.index("WaitReply $n 'OK' $t5Wait")
        late_at = block.index("ReplySeen $n 'OK'")
        self.assertLess(wait_at, late_at)
        self.assertIn("OK(遅れて届いた)", block)
        self.assertIn("NG(タイムアウト)", block)   # still the record when nothing is there
        rec_at = block.index("Rec 'T5' (\"送信=OK")   # the record of a sent post, not the window-missing one
        self.assertLess(late_at, rec_at)
        self.assertIn("返信読取={3}", block)   # the other fields of the record keep their form
        self.assertIn("送信=OK 投稿を画面で確認={0} iPhone通知={1} 返信した={2} 返信読取={3}", block)


class TestSelfReplyIsOptInAndNeverAuto(unittest.TestCase):   # run-check.cmd -SelfReply: the script types the short replies itself
    def test_the_switch_and_the_environment_variable_exist_and_auto_wins(self):
        self.assertIn("[switch]$SelfReply", CHECK)
        self.assertIn("$env:KIMERU_CHECK_SELF_REPLY -eq '1'", CHECK)
        on_at = CHECK.index("$env:KIMERU_CHECK_SELF_REPLY -eq '1'")
        auto_at = CHECK.index("if ($Auto -and $SelfReply) { $SelfReply = [switch]$false }")
        self.assertLess(on_at, auto_at)   # -Auto switches it off after the variable switched it on
        self.assertNotIn("SelfReply", AUTO.decode("utf-8-sig"))   # run-auto-check never asks for it

    def test_the_consents_of_the_five_steps_are_answered_y(self):
        self.assertRegex(CHECK, r"function Consent\(\$q\) \{\s*if \(\$SelfReply\) \{[^}]*自動: y[^}]*return \$true \}")
        for step, prompt in (("T5", "テストメッセージを1通送信します"), ("T9", "確認待ち2件を"), ("T20", "架空の確認待ち2件を"),
                             ("T25", "「停止」と「再開」を、あなたが 1 行ずつ打ちます"), ("T27", "試験の投稿を 1 通送信します")):
            line = next(l for l in CHECK.splitlines() if prompt in l)
            self.assertIn("Consent ", line, step)
            self.assertNotIn("YesNo ", line, step)

    def test_the_replies_are_typed_with_testreply_and_the_questions_about_what_a_person_saw_are_answered_n(self):
        self.assertIn("Self 'testreply' @('-Text', $text, '-Numbers', $nums)", CHECK)
        self.assertEqual(CHECK.count("function AutoNo"), 1)
        self.assertIn('AutoNo "   iPhone に通知は届きましたか？ [y/n]"', CHECK)
        self.assertIn("'自動（確認せず）'", CHECK)
        self.assertIn("返信=自動", CHECK)
        for call in ('Auto-Reply "OK $n" "$n"', 'Auto-Reply "OK $base" "$base,$($base + 1)"', 'Auto-Reply "NG $($base + 1)" "$base,$($base + 1)"',
                     'Auto-Reply "聞き返し $base" "$base,$($base + 1)"', 'Auto-Reply $word "$n25"', 'Auto-Reply $line "$n"'):
            self.assertIn(call, CHECK, call)   # every reply carries the number this run just posted

    def test_t26_t22_t21_and_the_outside_steps_are_skipped_under_it(self):
        line = next(l for l in CHECK.splitlines() if l.startswith("$SelfReplySkip"))
        skipped = set(re.findall(r"'(T\d+)'", line))
        self.assertEqual(skipped, {"T10", "T11", "T15", "T16", "T17", "T18", "T19", "T21", "T22", "T24", "T26"})
        self.assertFalse(skipped & {"T5", "T9", "T20", "T25", "T27"})
        self.assertIn("'SKIP（自動の返信では行わない: 相手・外部が要る）'", CHECK)

    def test_the_default_run_is_unchanged(self):
        # without the switch Consent is YesNo and Want is the plain "was it asked for" test
        self.assertRegex(CHECK, r"function Consent\(\$q\) \{[^}]*\}\s*YesNo \$q\s*\}")
        want = CHECK[CHECK.index("function Want($t) {"):]
        want = want[:want.index("\n}\n")]
        self.assertIn("if ($w -and $SelfReply -and", want)

    def test_t25_posts_only_a_test_post_and_always_ends_with_the_resume_word(self):
        block = CHECK[CHECK.index("# ---- T25 "):CHECK.index("# ---- T26 ")]
        self.assertIn("[kimeru 試験 #$n25] 停止・再開の試験です", block)
        self.assertIn("foreach ($word in @('停止', '再開'))", block)   # 再開 is typed even when 停止 was not read back


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

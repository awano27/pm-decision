"""The unattended approval read (tools/teams-self.ps1 Start-SafeRead / Restore-SafeRead and the end of the read action) run on a
synthetic Teams: the chat list, the selection, the window title, the foreground window and the last-input time are all simulated.
Never touches the real Teams, UI Automation tree, keyboard or mouse."""
try:   # isolation from the real state folder, whichever way the tests are started
    from . import isolate  # noqa: F401
except ImportError:
    import isolate  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "teams-self.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _between(source, start, end, include_end=True):
    i = source.index(start)
    j = source.index(end, i)
    return source[i:j + (len(end) if include_end else 0)]


def production_code():
    source = SCRIPT.read_text(encoding="utf-8")
    functions = _between(source, "$script:ReadCtx = $null", "\nif ($SafeRead) {", include_end=False)
    after_walk = _between(source, "  $screen = $null", "screen = $screen }; exit 0")
    return functions, after_walk


MOCKS = r'''
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type -Namespace K -Name W -MemberDefinition @'
public static System.IntPtr Fg = new System.IntPtr(7);
public static bool AllowSet = true;
public static int SetCalls = 0;
public static System.IntPtr GetForegroundWindow() { return Fg; }
public static bool SetForegroundWindow(System.IntPtr h) { SetCalls++; if (AllowSet) { Fg = h; } return AllowSet; }
'@
$S = ConvertFrom-Json -InputObject $env:Scenario
if ($S.foreground_restore_fails) { [K.W]::AllowSet = $false }
$script:Ops = New-Object System.Collections.Generic.List[string]
$script:Tick = 1000
$script:OpCount = 0
$script:Pane = [string]$S.screen            # the chat in the message pane: '48:notes', a chat id, or '' (none)
$script:Selected = [string]$S.selected        # the chat the list reports selected ('' = the list reports none)
$TEAMS = [IntPtr]100
function Start-Sleep { }
function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 5 }
function Fail($msg) { throw [System.Exception]::new('KFAIL:' + $msg) }
function After-Op {
  $script:OpCount++
  if ($S.input_after_op -and $script:OpCount -ge [int]$S.input_after_op) { $script:Tick++ }
  if ($S.front_on_op) { [K.W]::Fg = $TEAMS }
}
function New-Item-Fake([string]$id, [string]$title) {
  $it = [pscustomobject]@{ id = $id; title = $title }
  $it | Add-Member ScriptMethod GetCurrentPattern {
    param($p)
    $target = $this
    $pat = [pscustomobject]@{ how = $(if ($p -eq [System.Windows.Automation.SelectionItemPattern]::Pattern) { 'select' } else { 'invoke' }) }
    $pat | Add-Member NoteProperty target $target
    $pat | Add-Member ScriptMethod Select { Do-Pattern $this.target 'select' }
    $pat | Add-Member ScriptMethod Invoke { Do-Pattern $this.target 'invoke' }
    $pat
  }
  $it
}
function Do-Pattern($item, [string]$how) {
  $script:Ops.Add("${how}:" + $item.id)
  $works = if ($item.id -eq '48:notes') { @($S.notes_opens_by) } else { @($S.orig_opens_by) }
  if ($works -contains $how) { $script:Pane = $item.id; $script:Selected = $(if ($S.list_reports_selection) { $item.id } else { '' }) }
  elseif ($S.select_without_navigation -and $how -eq 'select') { $script:Selected = $item.id }
  After-Op
}
$script:Items = @()
if ($S.chat_list) {
  foreach ($c in $S.chats) { $script:Items += (New-Item-Fake ([string]$c.id) ([string]$c.title)) }
}
$WIN = [pscustomobject]@{ Current = [pscustomobject]@{ NativeWindowHandle = 100; Name = 'synthetic' } }
function Get-TeamsWindow {
  if ($S.throw_after_start -and $script:AfterStart) { $script:AfterStart = $false; throw 'synthetic post-start error' }
  if ($S.throw_after_op -and $script:OpCount -ge 1) { throw [System.InvalidOperationException]::new('synthetic element gone') }
  if ($S.teams) { $WIN } else { $null }
}
function Get-ChatItems($w) { if ($S.input_during_enumeration) { $script:Tick++ }; $script:Items }
function Get-NotesItems($w) { @($script:Items | Where-Object { $_.id -eq '48:notes' }) }
function Test-Selected($item) { $item.id -eq $script:Selected }
function Get-ChatId($item) { $item.id }
function Get-ChatTitle($item) { $item.title }
function Test-SelfTitle($w) { -not $S.header_names_chat -and $script:Pane -eq '48:notes' }
function Get-SelectedChatId($w) { if ($script:Selected) { $script:Selected } else { $null } }
function Get-WinChatTitle($w) { $t = @($S.chats | Where-Object { $_.id -eq $script:Pane } | ForEach-Object { $_.title }); if ($t) { $t[0] } else { '' } }
function Test-OrigOpen($w, $id, $title) { [bool]$id -and $script:Pane -eq $id }
function Get-LastInputTick { if ($S.no_input_time) { [uint32]0 } else { [uint32]$script:Tick } }
function Get-IdleSeconds { if ($S.idle) { 999 } else { 2 } }
if ($null -ne $S.idle_sec) { $env:KIMERU_IDLE_SEC = [string]$S.idle_sec }
function Test-TeamsInUse($w) { [bool]$S.in_use }
function Ensure-Learned($w) { $script:AfterStart = $true }
'''


def run_scenario(**kw):
    """Run Start-SafeRead, a simulated walk and the production end of the read action; returns (bridge output, ops log)."""
    scenario = {"teams": True, "chat_list": True, "idle": True, "in_use": False, "lock_busy": False,
                "chats": [{"id": "48:notes", "title": "me"}, {"id": "19:abc@thread.v2", "title": "Project room"}],
                "screen": "19:abc@thread.v2", "selected": "19:abc@thread.v2", "list_reports_selection": True,
                "notes_opens_by": ["select"], "orig_opens_by": ["select"], "input_after_op": 0, "front_on_op": False,
                "during_walk": "", "walk_fails": False, "timeline": ["P:1", "R:OK 1"]}
    scenario.update(kw)
    functions, after_walk = production_code()
    entry = _between(SCRIPT.read_text(encoding="utf-8"), "if ($SafeRead) {\n  $script:ReadCtx", "\nif ($Action -eq 'open')", include_end=False)
    code = MOCKS + "\n" + functions + r'''
function Enter-ReadLock { -not [bool]$S.lock_busy }   # the named mutex itself is not taken in a test
function Test-SelfHeader($w) { [bool]$S.header_names_chat -and $script:Pane -eq '48:notes' }   # the open chat's header (newer Teams)
$script:InRead = $false
try {
  $SafeRead = $true
  $w = Get-TeamsWindow
''' + entry + r'''
  if ($S.during_walk -eq 'user_input') { $script:Tick++ }
  if ($S.during_walk -eq 'switch_chat') { $script:Tick++; $script:Pane = '19:other@thread.v2'; $script:Selected = '19:other@thread.v2' }
  $walkFailed = [bool]$S.walk_fails
  $posts = @(); $replies = @('OK 1'); $timeline = @($S.timeline)
''' + after_walk + r'''
} finally {
  [Console]::Error.WriteLine('OPS ' + (ConvertTo-Json -Compress -InputObject @{ ops = @($script:Ops); fg = [int64][K.W]::Fg; set = [K.W]::SetCalls; screen = $script:Pane }))
}
'''
    with tempfile.TemporaryDirectory(prefix="kimeru-saferead-") as d:
        env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
               "WINDIR": os.environ.get("WINDIR", ""), "TEMP": d, "TMP": d,
               "Scenario": json.dumps(scenario, ensure_ascii=False)}
        p = Path(d) / "case.ps1"
        p.write_text(code, encoding="utf-8-sig")
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(p)],
                              input="", capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    out_lines = [l for l in proc.stdout.splitlines() if l.startswith("{")]
    ops_lines = [l for l in proc.stderr.splitlines() if l.startswith("OPS ")]
    if not out_lines or not ops_lines:
        raise AssertionError(f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return json.loads(out_lines[-1]), json.loads(ops_lines[-1][4:]), proc.returncode


@unittest.skipUnless(POWERSHELL, "PowerShell not available")
class TestSafeApprovalRead(unittest.TestCase):
    def test_self_chat_already_open_is_read_without_any_operation(self):
        out, log, rc = run_scenario(screen="48:notes", selected="48:notes", idle=False, in_use=True)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["replies"], ["OK 1"])
        self.assertEqual(out["screen"]["restore"], "none")
        self.assertTrue(out["screen"]["returned"])
        self.assertEqual(log["ops"], [])

    def test_opens_by_pattern_reads_and_puts_the_original_chat_back(self):
        out, log, rc = run_scenario()
        self.assertTrue(out["ok"])
        self.assertEqual(out["screen"], {"restore": "restored", "returned": True, "foreground": "unchanged"})
        self.assertEqual(log["ops"], ["select:48:notes", "select:19:abc@thread.v2"])
        self.assertEqual(log["screen"], "19:abc@thread.v2")

    def test_a_title_without_the_chat_name_is_decided_by_the_chat_header(self):
        out, log, rc = run_scenario(header_names_chat=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out["screen"]["restore"], "restored")
        self.assertEqual(log["ops"], ["select:48:notes", "select:19:abc@thread.v2"])
        out, log, rc = run_scenario(header_names_chat=True, screen="48:notes", selected="48:notes", idle=False)
        self.assertEqual((rc, log["ops"]), (0, []))

    def test_the_front_window_is_given_back_when_teams_took_it(self):
        out, log, _ = run_scenario(front_on_op=True)
        self.assertEqual(out["screen"]["foreground"], "restored")
        self.assertEqual(log["fg"], 7)

    def test_person_active_postpones_without_touching_anything(self):
        for kw, reason in (({"idle": False}, "user_active"), ({"in_use": True}, "teams_in_use"),
                           ({"lock_busy": True}, "ui_lock_busy"), ({"teams": False}, "teams_not_found"),
                           ({"chat_list": False}, "chat_list_not_shown"),
                           ({"chats": [{"id": "19:abc@thread.v2", "title": "Project room"}]}, "self_chat_not_in_list")):
            with self.subTest(reason=reason):
                out, log, rc = run_scenario(**kw)
                self.assertEqual(rc, 2)
                self.assertEqual((out["ok"], out["deferred"], out["reason"], out["restore"]), (False, True, reason, "none"))
                self.assertTrue(out["returned"])
                self.assertEqual(log["ops"], [])
                self.assertNotIn("replies", out)

    def test_input_while_enumerating_chats_defers_before_any_operation(self):
        out, log, rc = run_scenario(input_during_enumeration=True)
        self.assertEqual((rc, out.get("reason"), log["ops"]), (2, "user_input_during_read", []))
        self.assertNotIn("replies", out)

    def test_person_input_after_opening_postpones_and_does_not_undo_their_screen(self):
        out, log, rc = run_scenario(input_after_op=1)
        self.assertEqual(rc, 2)
        self.assertEqual((out["deferred"], out["reason"], out["restore"]), (True, "user_input_during_read", "skipped_user_input"))
        self.assertFalse(out["returned"])
        self.assertEqual(log["ops"], ["select:48:notes"])   # no put-back over the person's use

    def test_person_input_during_the_read_discards_the_read_and_does_not_put_back(self):
        out, log, rc = run_scenario(during_walk="user_input")
        self.assertEqual(rc, 2)
        self.assertNotIn("replies", out)
        self.assertEqual(out["restore"], "skipped_user_input")
        self.assertFalse(out["returned"])
        self.assertEqual(log["ops"], ["select:48:notes"])

    def test_chat_switched_during_the_read_discards_it_and_leaves_the_persons_choice(self):
        out, log, rc = run_scenario(during_walk="switch_chat")
        self.assertEqual(rc, 2)
        self.assertEqual((out["reason"], out["restore"]), ("screen_changed_during_read", "skipped_user_input"))
        self.assertNotIn("replies", out)
        self.assertEqual(log["screen"], "19:other@thread.v2")
        self.assertEqual(log["ops"], ["select:48:notes"])

    def test_a_selection_that_does_not_open_the_chat_is_not_read(self):
        out, log, rc = run_scenario(notes_opens_by=[], select_without_navigation=True, list_reports_selection=True)
        self.assertEqual(rc, 2)
        self.assertEqual(out["reason"], "self_chat_not_opened")
        self.assertNotIn("replies", out)
        self.assertEqual(log["ops"], ["select:48:notes", "invoke:48:notes", "select:19:abc@thread.v2"])
        self.assertEqual(out["restore"], "restored")

    def test_put_back_failure_is_reported_not_success(self):
        out, log, rc = run_scenario(orig_opens_by=[])
        self.assertEqual(rc, 2)
        self.assertEqual(out["restore"], "left_self")
        self.assertFalse(out["returned"])
        self.assertNotIn("replies", out)

    def test_unknown_original_chat_defers_without_navigation(self):
        out, log, rc = run_scenario(screen="", selected="")
        self.assertEqual((rc, out["reason"], log["ops"]), (2, "original_chat_unknown", []))

    def test_foreground_restore_failure_discards_replies(self):
        out, _, rc = run_scenario(front_on_op=True, foreground_restore_fails=True)
        self.assertEqual((rc, out["reason"], out["foreground"]), (2, "restore_failed", "not_restored"))
        self.assertFalse(out["returned"])
        self.assertNotIn("replies", out)

    def test_input_while_self_chat_was_already_open_discards_replies(self):
        out, log, rc = run_scenario(screen="48:notes", selected="48:notes", during_walk="user_input")
        self.assertEqual((rc, out["reason"], log["ops"]), (2, "user_input_during_read", []))
        self.assertNotIn("replies", out)

    def test_exception_during_the_read_puts_back_and_postpones(self):
        out, log, rc = run_scenario(walk_fails=True)
        self.assertEqual(rc, 2)
        self.assertEqual((out["reason"], out["restore"]), ("unexpected_error", "restored"))
        self.assertNotIn("replies", out)

    def test_exception_between_open_and_walk_restores_and_defers(self):
        out, log, rc = run_scenario(throw_after_start=True)
        self.assertEqual((rc, out["reason"], out["restore"]), (2, "unexpected_error", "restored"))
        self.assertEqual(log["screen"], "19:abc@thread.v2")
        self.assertNotIn("replies", out)

    def test_idle_sec_zero_does_not_switch_off_the_away_check(self):
        out, log, rc = run_scenario(idle=False, idle_sec=0)
        self.assertEqual((rc, out["reason"], log["ops"]), (2, "user_active", []))

    def test_unknown_input_time_postpones_before_any_operation(self):
        out, log, rc = run_scenario(no_input_time=True)
        self.assertEqual((rc, out["reason"], out["restore"], log["ops"]), (2, "input_state_unknown", "none", []))

    def test_an_exception_after_operating_still_ends_with_a_fixed_reason(self):
        out, log, rc = run_scenario(throw_after_op=True)
        self.assertEqual(rc, 2)
        self.assertEqual((out["deferred"], out["reason"], out["restore"], out["returned"]), (True, "unexpected_error", "failed", False))
        self.assertEqual(log["ops"], ["select:48:notes"])

    def test_a_pane_without_any_kimeru_post_is_not_taken_for_the_self_chat(self):
        out, log, rc = run_scenario(timeline=["R:OK 1"])
        self.assertEqual(rc, 2)
        self.assertEqual((out["reason"], out["restore"]), ("self_chat_not_confirmed", "restored"))
        self.assertNotIn("replies", out)

    def test_no_keys_clicks_or_links_in_the_unattended_read(self):
        functions, after_walk = production_code()
        functions += after_walk
        for banned in ("SendKeys", "keybd_event", "mouse_event", "SetCursorPos", "Start-Process", "Assert-Foreground",
                       "Open-SelfChat", "Show-ChatApp", "Find-SelfItems", "Clear-OurBox"):
            self.assertNotIn(banned, functions, banned)


if __name__ == "__main__":
    unittest.main()

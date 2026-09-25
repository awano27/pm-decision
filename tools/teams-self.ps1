<#
.SYNOPSIS
  Teams "chat with yourself" bridge via built-in Windows UI Automation (no downloads, no admin).

  status               -> JSON {teams, selfChatOpen}
  open                 -> select the self chat in the chat list, verify by window title
  post -Text <s>       -> open self chat, paste text into the compose box (NOT sent)
  post -Text <s> -Send -> same, then press Enter after re-verifying the window title
  send                 -> press Enter only if the compose box starts with "[kimeru"
  read                 -> JSON {posts:[...], replies:[...]} extracted from the self chat only:
                          posts   = ids N of "[kimeru #N]" posts (no other text)
                          replies = "OK 3" / "NG 3" / "保留 3" style lines
  Nothing else from the chat is output.

  Safety: every write re-checks that the window title is the self chat
  ("... (あなた) | Microsoft Teams" or "... (You) | Microsoft Teams"); otherwise it aborts.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('status', 'open', 'post', 'send', 'read')][string]$Action,
  [string]$Text = '',
  [switch]$Send
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$SELF = '\((あなた|You)\)'

Add-Type -Namespace K -Name W -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);
'@

function Assert-Foreground($w) {
  # SendKeys goes to whatever window is in front; refuse unless it is Teams
  $h = [IntPtr]$w.Current.NativeWindowHandle
  if ([K.W]::GetForegroundWindow() -ne $h) {
    [void][K.W]::ShowWindow($h, 9); [void][K.W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 400
  }
  if ([K.W]::GetForegroundWindow() -ne $h) { Fail 'Teams is not the foreground window; no keys sent' }
}

function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 5 }
function Fail($msg) { Out-Json @{ ok = $false; error = $msg }; exit 2 }

function Get-TeamsWindow {
  $pids = @(Get-Process -Name ms-teams -ErrorAction SilentlyContinue | ForEach-Object Id)
  if (-not $pids) { return $null }
  $A::RootElement.FindAll('Children', [System.Windows.Automation.Condition]::TrueCondition) |
    Where-Object { $pids -contains $_.Current.ProcessId -and $_.Current.Name } | Select-Object -First 1
}

function Test-SelfTitle($w) { $w -and ($w.Current.Name -match "\| [^|]+ $SELF \| Microsoft Teams$") }

function Find-All($root, $type) {
  $root.FindAll('Descendants', (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $type)))
}

function Open-SelfChat($w) {
  if (Test-SelfTitle $w) { return $w }
  # the self chat's list item is the short one whose whole name ends with "(あなた)"/"(You)"
  $item = Find-All $w $CT::TreeItem | Where-Object { $_.Current.Name -match "^[^:：]{1,60} $SELF$" } | Select-Object -First 1
  if (-not $item) { Fail 'self chat not found in chat list (open the Chat tab)' }
  try { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
  catch { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 250
    $w = Get-TeamsWindow
    if (Test-SelfTitle $w) { return $w }
  }
  Fail 'self chat did not open'
}

$w = Get-TeamsWindow
if ($Action -eq 'status') { Out-Json @{ ok = $true; teams = [bool]$w; selfChatOpen = [bool](Test-SelfTitle $w) }; exit 0 }
if (-not $w) { Fail 'Teams window not found' }
$w = Open-SelfChat $w
if ($Action -eq 'open') { Out-Json @{ ok = $true; selfChatOpen = $true }; exit 0 }

function Get-Box($w) {
  $b = Find-All $w $CT::Edit | Where-Object { $_.Current.AutomationId -like 'new-message-*' } | Select-Object -First 1
  if (-not $b) { Fail 'compose box not found' }
  $b
}
function Get-BoxText($b) {
  try { return $b.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(4000) }
  catch { try { return $b.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { return '' } }
}
function Send-Box($w) {
  # send only what kimeru wrote: self chat + compose box starts with [kimeru
  if (-not (Test-SelfTitle (Get-TeamsWindow))) { Fail 'window changed before send; aborted' }
  $t = (Get-BoxText (Get-Box $w)).Trim()
  if (-not $t.StartsWith('[kimeru')) { Fail 'compose box does not start with [kimeru; not sending' }
  Assert-Foreground $w
  (Get-Box $w).SetFocus(); Start-Sleep -Milliseconds 150
  Assert-Foreground $w
  [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
}

if ($Action -eq 'send') { Send-Box $w; Out-Json @{ ok = $true; sent = $true }; exit 0 }

if ($Action -eq 'post') {
  if (-not $Text.StartsWith('[kimeru')) { Fail 'refusing to post text that does not start with [kimeru' }
  $Text = $Text -replace '\\n', "`r`n"   # callers pass line breaks as literal \n
  $box = Get-Box $w
  $saved = $null
  try { $saved = [System.Windows.Forms.Clipboard]::GetText() } catch {}
  [System.Windows.Forms.Clipboard]::SetText($Text)
  Assert-Foreground $w
  $box.SetFocus(); Start-Sleep -Milliseconds 200
  Assert-Foreground $w
  [System.Windows.Forms.SendKeys]::SendWait('^v'); Start-Sleep -Milliseconds 300
  if ($saved) { [System.Windows.Forms.Clipboard]::SetText($saved) } else { [System.Windows.Forms.Clipboard]::Clear() }
  $typed = (Get-BoxText $box).Trim().StartsWith('[kimeru')
  $sent = $false
  if ($Send) { Send-Box $w; $sent = $true }
  Out-Json @{ ok = $true; typed = $typed; sent = $sent }; exit 0
}

if ($Action -eq 'read') {
  $posts = New-Object System.Collections.Generic.List[string]
  $replies = New-Object System.Collections.Generic.List[string]
  $seen = @{}
  # timeline: posts ("P:N") and replies ("R:OK N") in screen order (oldest first). One message is
  # exposed by several UIA nodes, so consecutive duplicates are collapsed.
  $timeline = New-Object System.Collections.Generic.List[string]
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  function Walk($el, $d) {
    if ($d -gt 40) { return }
    foreach ($line in ([string]$el.Current.Name) -split "`n") {
      $l = $line.Trim()
      $entry = $null
      if ($l -match '^\[kimeru #(\d+)\]') {
        $entry = "P:" + $Matches[1]
        if (-not $posts.Contains($Matches[1])) { $posts.Add($Matches[1]) }
      }
      elseif ($l -match '^(OK|NG|保留)\s*#?\d+$') {
        $entry = "R:" + $l
        if (-not $seen.ContainsKey($l)) { $seen[$l] = 1; $replies.Add($l) }
      }
      if ($entry -and ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $entry)) { $timeline.Add($entry) }
    }
    $c = $walker.GetFirstChild($el)
    while ($c) { Walk $c ($d + 1); $c = $walker.GetNextSibling($c) }
  }
  Walk $w 0
  Out-Json @{ ok = $true; posts = @($posts); replies = @($replies); timeline = @($timeline) }; exit 0
}

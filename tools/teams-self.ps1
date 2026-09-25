<#
.SYNOPSIS
  Teams "chat with yourself" bridge via built-in Windows UI Automation (no downloads, no admin).

  status               -> JSON {teams, selfChatOpen}
  open                 -> select the self chat in the chat list, verify by window title
  post -Text <s>       -> open self chat, paste text into the compose box (NOT sent)
  post -Text <s> -Send -> same, then press Enter after re-verifying the window title
  send                 -> press Enter only if the compose box starts with "[kimeru"
  diag                 -> JSON with only structure hints for troubleshooting "self chat not found":
                          window title shape and chat-list item shapes (letters masked), markers
  learn                -> while the self chat is open, remember your display name (read from the
                          window title) in %LOCALAPPDATA%\kimeru\self-name.txt so `open` can find
                          the self chat in lists whose items carry no "(自分)" marker. Local only.
  read                 -> JSON {posts:[...], replies:[...]} extracted from the self chat only:
                          posts   = ids N of "[kimeru #N]" posts (no other text)
                          replies = "OK 3" / "NG 3" / "保留 3" style lines
  Nothing else from the chat is output.

  Safety: every write re-checks that the window title is the self chat
  ("| <name> (あなた|自分|You|Me) |" in the title); otherwise it aborts.
  Personal Teams shows "(あなた)"; work accounts may show "(自分)". Override with -SelfMarker.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('status', 'open', 'post', 'send', 'read', 'diag', 'learn')][string]$Action,
  [string]$Text = '',
  [switch]$Send,
  [string]$SelfMarker = $env:KIMERU_SELF_MARKER   # e.g. "自分" if your Teams shows another word
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$markers = @('あなた', '自分', 'You', 'Me')
if ($SelfMarker) { $markers = @([regex]::Escape($SelfMarker)) + $markers }
$SELF = '\((' + ($markers -join '|') + ')\)'

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

function Test-SelfTitle($w) { $w -and ($w.Current.Name -match "\| [^|]+ $SELF \|") }

function Find-All($root, $type) {
  $root.FindAll('Descendants', (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $type)))
}

$NameFile = Join-Path $env:LOCALAPPDATA 'kimeru\self-name.txt'
function Get-SelfName { if (Test-Path $NameFile) { (Get-Content $NameFile -Encoding UTF8 -TotalCount 1).Trim() } }

function Get-ChatItems($w) {
  # the chat list is TreeItems in some Teams layouts and ListItems in others
  @(Find-All $w $CT::TreeItem) + @(Find-All $w $CT::ListItem)
}

function Find-SelfItems($w) {
  $items = Get-ChatItems $w
  # 1) "<name> (あなた|自分|...)" at the start; work accounts append the latest message and time
  $hit = $items | Where-Object { $_.Current.Name -match "^[^:：]{1,60}? $SELF" }
  # 2) learned display name as a whole word near the start (items may carry a type prefix such as
  #    "チャット "), not a group chat ("<name>, other" / "<name>、")
  $name = Get-SelfName
  if (-not $hit -and $name) {
    $rx = '^[^:：,、]{0,20}?(?<!\S)' + [regex]::Escape($name) + '(?=$|[\s(（:：])'
    $hit = $items | Where-Object { $_.Current.Name -match $rx }
  }
  @($hit | Sort-Object { $_.Current.Name.Length } | Select-Object -First 3)
}
function Find-SelfItem($w) { Find-SelfItems $w | Select-Object -First 1 }

function Mask($s) {
  # keep short parentheticals like "(自分)" and punctuation; letters/digits become x
  $e = [System.Text.RegularExpressions.MatchEvaluator] { param($m) if ($m.Value.StartsWith('(')) { $m.Value } else { 'x' } }
  $o = [regex]::Replace([string]$s, '\([^)]{1,8}\)|[\p{L}\p{N}]+', $e)
  if ($o.Length -gt 60) { $o.Substring(0, 60) } else { $o }
}

function Open-SelfChat($w) {
  if (Test-SelfTitle $w) { return $w }
  $cands = @(Find-SelfItems $w)
  if (-not $cands) { Fail 'self chat not found in chat list (open it once by hand and run -Action learn)' }
  foreach ($item in $cands) {   # a candidate may be a message rather than the chat entry: verify by title
    try { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
    catch { try { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch { continue } }
    for ($i = 0; $i -lt 12; $i++) {
      Start-Sleep -Milliseconds 250
      $w = Get-TeamsWindow
      if (Test-SelfTitle $w) { return $w }
    }
  }
  Fail 'self chat did not open'
}

$w = Get-TeamsWindow
if ($Action -eq 'diag') {
  if (-not $w) { Fail 'Teams window not found' }
  $title = $w.Current.Name
  $shape = (($title -split ' \| ') | ForEach-Object { if ($_ -match '\(([^)]{1,8})\)\s*$') { "<name> ($($Matches[1]))" } elseif ($_ -in 'Microsoft Teams', 'チャット', 'Chat') { $_ } else { '<text>' } }) -join ' | '
  $tree = @(Find-All $w $CT::TreeItem); $list = @(Find-All $w $CT::ListItem)
  $items = $tree + $list
  $found = @{}
  foreach ($i in $items) { foreach ($m in [regex]::Matches($i.Current.Name, '\(([^)]{1,8})\)')) { $found[$m.Groups[1].Value] = 1 + [int]$found[$m.Groups[1].Value] } }
  $tabs = @(Find-All $w $CT::TabItem | Where-Object { try { $_.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected } catch { $false } } | ForEach-Object { $_.Current.Name } | Where-Object { $_.Length -le 12 })
  # most useful fields first: the result sheet truncates long lines
  Out-Json ([ordered]@{ ok = $true; selfItemFound = [bool](Find-SelfItem $w); learnedName = [bool](Get-SelfName)
              parenMarkers = $found; titleShape = $shape; treeItems = $tree.Count; listItems = $list.Count; selectedTabs = $tabs
              itemShapes = @($items | Select-Object -First 8 | ForEach-Object { Mask $_.Current.Name }) })
  exit 0
}
if ($Action -eq 'learn') {
  if (-not $w) { Fail 'Teams window not found' }
  if ($w.Current.Name -notmatch "\| ([^|]+?) $SELF \|") { Fail 'open your self chat in Teams first, then run learn' }
  $name = $Matches[1].Trim()
  New-Item -ItemType Directory -Force (Split-Path $NameFile) | Out-Null
  [IO.File]::WriteAllText($NameFile, $name, (New-Object Text.UTF8Encoding $false))
  Out-Json ([ordered]@{ ok = $true; learned = $true; itemFound = [bool](Find-SelfItem $w) })   # the name itself is not printed
  exit 0
}
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
function Test-BoxHasKimeru($w) { (Get-BoxText (Get-Box $w)).Trim().StartsWith('[kimeru') }

function Wait-Sent($w) {
  for ($i = 0; $i -lt 12; $i++) { Start-Sleep -Milliseconds 250; if (-not (Test-BoxHasKimeru $w)) { return $true } }
  $false
}

function Send-Box($w) {
  # send only what kimeru wrote: self chat + compose box starts with [kimeru
  if (-not (Test-SelfTitle (Get-TeamsWindow))) { Fail 'window changed before send; aborted' }
  if (-not (Test-BoxHasKimeru $w)) { Fail 'compose box does not start with [kimeru; not sending' }
  # 1) the Send button: works whether Enter or Ctrl+Enter sends in this user's Teams settings
  $btn = Find-All $w $CT::Button | Where-Object { $_.Current.Name -match '^(送信|Send)(\s*\(|$)' -and $_.Current.IsEnabled } | Select-Object -First 1
  if ($btn) {
    try { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch {}
    if (Wait-Sent $w) { return 'button' }
  }
  # 2) keys, re-checking the target each time (Enter may only insert a line break)
  foreach ($k in @('^{ENTER}', '{ENTER}')) {
    if (-not (Test-SelfTitle (Get-TeamsWindow))) { Fail 'window changed before send; aborted' }
    if (-not (Test-BoxHasKimeru $w)) { return 'keys' }
    Assert-Foreground $w
    (Get-Box $w).SetFocus(); Start-Sleep -Milliseconds 150
    Assert-Foreground $w
    [System.Windows.Forms.SendKeys]::SendWait($k)
    if (Wait-Sent $w) { return "keys:$k" }
  }
  Fail 'message was not sent (compose box still holds the text); press Send in Teams by hand'
}

if ($Action -eq 'send') { $how = Send-Box $w; Out-Json @{ ok = $true; sent = $true; via = $how }; exit 0 }

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
  if ($Send) { [void](Send-Box $w); $sent = $true }
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
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^(?i)(OK|NG|保留)\s*#?(\d+)$') {
        # phones often send full-width or re-cased text ("ＯＫ　６７５", "Ok 675"): canonicalize
        $c = '{0} {1}' -f $Matches[1].ToUpper(), $Matches[2]
        $entry = "R:" + $c
        if (-not $seen.ContainsKey($c)) { $seen[$c] = 1; $replies.Add($c) }
      }
      if ($entry -and ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $entry)) { $timeline.Add($entry) }
    }
    $c = $walker.GetFirstChild($el)
    while ($c) { Walk $c ($d + 1); $c = $walker.GetNextSibling($c) }
  }
  Walk $w 0
  Out-Json @{ ok = $true; posts = @($posts); replies = @($replies); timeline = @($timeline) }; exit 0
}

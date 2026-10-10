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
  chats                -> JSON list of chat-list entries for `kimeru pull teams` (local use only):
                          {id, kind: self|oneOnOne|group|meeting|other, title, preview, time, unread, mention}
                          kind comes from the chat id in the UIA AutomationIds (48:notes = self chat,
                          *@unq.gbl.spaces = 1:1, 19:meeting_* = meeting, *@thread.* = group)
  learn                -> while the self chat is open, remember your display name (read from the
                          window title) in %LOCALAPPDATA%\kimeru\self-name.txt so `open` can find
                          the self chat in lists whose items carry no "(自分)" marker. Local only.
  probe                -> read only, for `kimeru diagnose`: the self-chat messages as the send readback reads them
                          (Get-ChatMessages), with fragment counts and on-screen sizes. Python keeps numbers only.
  read                 -> JSON {posts:[...], replies:[...]} extracted from the self chat only:
                          posts   = ids N of "[kimeru #N]" posts (no other text)
                          replies = "OK 3" / "NG 3" / "保留 3" style lines
  Nothing else from the chat is output.

  Safety: every write re-checks that the window title is the self chat
  ("| <name> (あなた|自分|You|Me) |" in the title); otherwise it aborts.
  Personal Teams shows "(あなた)"; work accounts may show "(自分)". Override with -SelfMarker.
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('status', 'open', 'post', 'send', 'read', 'diag', 'learn', 'chats', 'readchat', 'test-messages', 'test-compare', 'test-readback', 'test-container', 'test-failure', 'test-send-once', 'probe')][string]$Action,
  [string]$Text = '',
  [string]$ChatId = '',      # readchat: the chat to open (its id from `chats`)
  [int]$Count = 5,           # readchat: how many of the last messages to read
  [string]$ReadbackText = '',
  [string]$TestRows = '',
  [string]$PriorIds = '',
  [string]$PriorAllIds = '',
  [ValidateSet('pre-send', 'attempted', 'sent')][string]$TestDeliveryState = 'pre-send',
  [int]$PriorCount = 0,
  [string]$Preview = '',     # readchat: the start of the chat's list preview (a screen that reports no selection is checked against it)
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
[DllImport("user32.dll")] public static extern System.IntPtr GetAncestor(System.IntPtr h, uint f);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr h, out uint pid);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, int flags, int extra);
'@

$script:PrevFg = [IntPtr]::Zero
function Assert-Foreground($w) {
  # SendKeys and clicks go to whatever window is in front; refuse unless it is Teams
  $h = [IntPtr]$w.Current.NativeWindowHandle
  $cur = [K.W]::GetForegroundWindow()
  if ($cur -ne $h) {
    if ($script:PrevFg -eq [IntPtr]::Zero) { $script:PrevFg = $cur }   # the window the person was in: put back afterwards (Restore-Foreground)
    [void][K.W]::ShowWindow($h, 9); [void][K.W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 400
    if ([K.W]::GetForegroundWindow() -ne $h) {
      # Windows refuses to let a process that is not in front take the front (foreground lock). A bare Alt press (no other key) lifts
      # the lock for the next call. Nothing is typed into any window; the check below still refuses unless Teams really is in front.
      [K.W]::keybd_event(0x12, 0, 0, 0); [K.W]::keybd_event(0x12, 0, 2, 0)
      [void][K.W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 400
    }
  }
  if ([K.W]::GetForegroundWindow() -ne $h) { Fail 'Teams is not the foreground window; no keys sent (click the Teams window once, then retry)' }
}
function Save-Foreground($w) {
  # a fixed link may bring Teams to the front by itself: remember the window the person was in, so Restore-Foreground can go back
  if ($script:PrevFg -ne [IntPtr]::Zero) { return }
  try { $cur = [K.W]::GetForegroundWindow(); if ($w -and $cur -ne [IntPtr]$w.Current.NativeWindowHandle) { $script:PrevFg = $cur } } catch {}
}
function Restore-Foreground {
  # Teams was brought to the front: go back to the window the person was in, unless they have moved on by themselves
  if ($script:PrevFg -eq [IntPtr]::Zero) { return }
  try {
    $w = Get-TeamsWindow
    if ($w -and [K.W]::GetForegroundWindow() -eq [IntPtr]$w.Current.NativeWindowHandle) { [void][K.W]::SetForegroundWindow($script:PrevFg) }
  } catch {}
  $script:PrevFg = [IntPtr]::Zero
}

function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 5 }
$script:InRead = $false   # readchat: a failure must not end the script before the chat is put back
$script:DeliveryTyped = $false
$script:DeliveryAttempted = $false
$script:DeliverySent = $false
function Get-SentEvidence {
  if ($script:DeliverySent) { return $true }
  if ($script:DeliveryAttempted) { return $null }
  $false
}
function Get-DeliveryFailureEvidence($msg) {
  # A send is definitely absent only when the bridge never entered a send action.
  # Once Invoke/SendKeys is attempted, failures are ambiguous until readback proves delivery.
  $sent = Get-SentEvidence
  return @{ ok = $false; error = $msg; typed = [bool]$script:DeliveryTyped; sent = $sent
            readback = @{ matched = $false; message_id = '' } }
}
function Get-SendGuardFailure([bool]$SelfChat, [bool]$ComposeOwned) {
  if (-not $SelfChat) { return 'window changed before send (title is not the self chat); aborted' }
  if (-not $ComposeOwned) { return 'compose box does not start with [kimeru; not sending' }
  return ''
}
function Get-KeySendGuardFailure([bool]$Foreground, [bool]$ComposeFocused) {
  if (-not $Foreground) { return 'Teams is not the foreground window; no keys sent (click the Teams window once, then retry)' }
  if (-not $ComposeFocused) { return 'keyboard focus is not the compose box; nothing sent' }
  return ''
}
function Invoke-SendOnce([bool]$ButtonAvailable, [scriptblock]$InvokeButton, [scriptblock]$SendKey,
                         [scriptblock]$WaitForSent, $Button = $null) {
  # Once an operation is attempted, never send again in this bridge call. UI response/readback may be delayed.
  $script:DeliveryAttempted = $true
  if ($ButtonAvailable) {
    try { [void](& $InvokeButton $Button) } catch {}
  } else {
    try { [void](& $SendKey '^{ENTER}') } catch {}
  }
  $cleared = $false
  try { $cleared = [bool](& $WaitForSent) } catch {}
  if ($ButtonAvailable) { return $(if ($cleared) { 'button' } else { 'button-unknown' }) }
  return $(if ($cleared) { 'keys:^{ENTER}' } else { 'keys-unknown' })
}
function Fail($msg) {
  if ($script:InRead) { throw [System.Exception]::new('KFAIL:' + $msg) }
  Out-Json (Get-DeliveryFailureEvidence $msg); exit 2
}

function Normalize-MeaningfulText([string]$s) {
  # Teams may render line breaks and non-breaking spaces differently. Collapse whitespace
  # runs, but keep the boundary between words (unlike the old remove-all-whitespace check).
  (($s -replace '[\u200b\ufeff]', '') -replace '[\s\u00a0]+', ' ').Trim()
}
function Runtime-Identity($el) {
  try { return (@($el.GetRuntimeId()) -join '.') } catch { return '' }
}
function Select-MessageContainerId($ancestors, [string]$fallback) {
  foreach ($candidate in $ancestors) {
    $aid = [string]$candidate.automation_id
    $kind = [string]$candidate.kind
    $individualMessage = $aid -match '(?i)^(?:message|chat-message|conversation-message)(?:[-_](?:item|bubble|body|content|container))(?:[-_][a-z0-9]+)?$'
    if ($kind -in @('Group', 'ListItem') -and $individualMessage -and
        $aid -notmatch '(?i)(list|pane|root|collection|thread)') {
      if ($candidate.runtime_id) { return [string]$candidate.runtime_id }
    }
  }
  $fallback
}
function Get-MessageContainerIdentity($el) {
  $fallback = Runtime-Identity $el
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $p = $walker.GetParent($el)
  $ancestors = @()
  for ($i = 0; $p -and $i -lt 6; $i++) {
    $kind = ([string]$p.Current.ControlType.ProgrammaticName -split '\.')[-1]
    $ancestors += [pscustomobject]@{ automation_id = [string]$p.Current.AutomationId; kind = $kind; runtime_id = (Runtime-Identity $p) }
    $p = $walker.GetParent($p)
  }
  Select-MessageContainerId $ancestors $fallback
}
function Collapse-MessageRows([object[]]$rows, [int]$count = 100) {
  $seen = @{}
  $messages = @()
  $flat = New-Object 'System.Collections.Generic.List[object]'
  foreach ($value in $rows) {
    if ($value -is [array]) { foreach ($inner in $value) { $flat.Add($inner) } }
    else { $flat.Add($value) }
  }
  foreach ($row in @($flat | Sort-Object { [double]$_.y })) {
    $id = [string]$row.container_id
    if (-not $id) { $id = [string]$row.runtime_id }
    if (-not $id -or $seen.ContainsKey($id)) { continue }
    $seen[$id] = $true
    $messages += [ordered]@{ message_id = $id; text = Normalize-MeaningfulText ([string]$row.text); sender = ''; time = '' }
  }
  @($messages | Select-Object -Last $count)
}
function Test-MeaningfulTextMatch([string]$left, [string]$right) {
  (Normalize-MeaningfulText $left) -ceq (Normalize-MeaningfulText $right)
}
function Test-PostedTextMatch([string]$sent, [string]$visible) {
  # a posted message as the message list reads it back. Newer Teams builds wrap the text for screen readers:
  # "<your name> 送信済み <the text> 今日の 10:33." (or "Sent ... Today at 10:33."). The text we sent must be there whole,
  # starting with its "[kimeru" marker, with at most a short label before it and a short time after it
  $a = Normalize-MeaningfulText $sent
  $b = Normalize-MeaningfulText $visible
  if ($a -ceq $b) { return $true }
  if (-not $a.StartsWith('[kimeru') -or $a.Length -lt 8) { return $false }
  $i = $b.IndexOf($a, [StringComparison]::Ordinal)
  if ($i -lt 0 -or $i -gt 80) { return $false }
  $before = $b.Substring(0, $i)
  if ($before.Contains('[kimeru')) { return $false }
  ($b.Length - $i - $a.Length) -le 60
}
function Get-AnchorIds($messages, [int]$keep = 3) {
  # the newest messages on screen before a send: they must still be there afterwards (same chat, still at the bottom).
  # Not every message: newer Teams builds drop the oldest rendered messages when a long post arrives, which made every
  # readback of a long post fail although it was delivered
  $ids = New-Object 'System.Collections.Generic.HashSet[string]'
  foreach ($m in @(@($messages) | Select-Object -Last $keep)) { if ([string]$m.message_id) { [void]$ids.Add([string]$m.message_id) } }
  ,$ids
}
function Find-NewMatchingMessage($messages, [string]$text, $priorIds, [int]$priorCount = 0, $priorAllIds = $null) {
  if ($priorAllIds) {
    $currentIds = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($message in $messages) { [void]$currentIds.Add([string]$message.message_id) }
    foreach ($id in $priorAllIds) { if (-not $currentIds.Contains([string]$id)) { return @{ matched = $false; message_id = '' } } }
  }
  $matches = @($messages | Where-Object {
    (Test-PostedTextMatch $text ([string]$_.text)) -and -not $priorIds.Contains([string]$_.message_id)
  })
  $matchingCount = @($messages | Where-Object { Test-PostedTextMatch $text ([string]$_.text) }).Count
  if ($matchingCount -gt $priorCount -and $matches.Count -eq 1 -and $matches[0].message_id) {
    return @{ matched = $true; message_id = [string]$matches[0].message_id }
  }
  @{ matched = $false; message_id = '' }
}

# ---- one Teams operation at a time, and never while the person is typing / moving the mouse ----
Add-Type -Namespace KI -Name L -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO p);
'@
function Get-IdleSeconds {
  $i = New-Object 'KI.L+LASTINPUTINFO'
  $i.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($i)
  if (-not [KI.L]::GetLastInputInfo([ref]$i)) { return 999 }
  $now = [int64][Environment]::TickCount -band 4294967295
  $d = ($now - [int64]$i.dwTime) % 4294967296
  if ($d -lt 0) { $d += 4294967296 }
  $d / 1000
}
function Get-LastInputTick {
  $i = New-Object 'KI.L+LASTINPUTINFO'
  $i.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($i)
  if (-not [KI.L]::GetLastInputInfo([ref]$i)) { return [uint32]0 }
  [uint32]$i.dwTime
}
$script:OwnTick = $null
function Mark-OwnInput { $script:OwnTick = Get-LastInputTick }   # our own click counts as input: do not mistake it for the person
$script:Restoring = $false   # readchat: putting the chat back (a shorter threshold than the one before reading)
function Get-ReadIdleNeed {
  # reading another chat is the heaviest operation: KIMERU_READ_IDLE_SEC (read_idle_sec), 30 s when it is not set (or not a number).
  # KIMERU_IDLE_SEC (idle_sec) has no say here: 0 there must not switch off the check before a colleague's chat is opened
  $n = 0
  if ("$env:KIMERU_READ_IDLE_SEC" -ne '' -and [int]::TryParse("$env:KIMERU_READ_IDLE_SEC", [ref]$n) -and $n -ge 0) { return $n }
  30
}
function Get-IdleNeed([int]$fallback) {
  # seconds without keyboard / mouse use that the person must have had
  if ($Action -ne 'readchat') {
    if ("$env:KIMERU_IDLE_SEC" -ne '') { return [int]$env:KIMERU_IDLE_SEC }
    return $fallback
  }
  $read = Get-ReadIdleNeed
  # putting the chat back is safer than leaving a colleague's chat open: the short threshold of the caller (3 s), never above the read one
  if ($script:Restoring) { return [math]::Min($fallback, $read) }
  $read
}
function Test-UserIdle([int]$need = 3) {
  # a check made right before each screen operation (no waiting): $false = the person is using the keyboard / mouse
  $need = Get-IdleNeed $need
  if ($need -le 0) { return $true }
  if ($null -ne $script:OwnTick -and (Get-LastInputTick) -eq $script:OwnTick) { return $true }   # nothing since our own input
  (Get-IdleSeconds) -ge $need
}
function Assert-Idle {
  if (-not (Test-UserIdle)) {
    $off = if ($Action -eq 'readchat') { 'KIMERU_READ_IDLE_SEC=0' } else { 'KIMERU_IDLE_SEC=0' }
    Fail "the keyboard / mouse has been in use; stopped (set $off to switch this off)"
  }
}
function Wait-IdleSoft([int]$need = 3, [int]$max = 20) {
  $t0 = Get-Date
  while (-not (Test-UserIdle $need)) {
    if (((Get-Date) - $t0).TotalSeconds -gt $max) { return $false }
    Start-Sleep -Milliseconds 500
  }
  $true
}
function Wait-UserIdle([int]$need = 4, [int]$max = 90) {
  if ($env:KIMERU_IDLE_SEC -ne $null -and $env:KIMERU_IDLE_SEC -ne '') { $need = [int]$env:KIMERU_IDLE_SEC }
  if ($need -le 0) { return }
  $t0 = Get-Date
  while ((Get-IdleSeconds) -lt $need) {
    if (((Get-Date) - $t0).TotalSeconds -gt $max) { Fail "the keyboard / mouse has been in use for $max s; Teams was not touched (set KIMERU_IDLE_SEC=0 to switch this off)" }
    Start-Sleep -Milliseconds 500
  }
}
function Enter-UiLock([int]$ms = 180000) {
  $script:UiMutex = New-Object Threading.Mutex($false, 'Local\kimeru-ui')
  $got = $false
  try { $got = $script:UiMutex.WaitOne($ms) } catch [Threading.AbandonedMutexException] { $got = $true }
  if (-not $got) { Fail 'another kimeru Teams operation is running; Teams was not touched' }
}


function Get-TeamsWindow {
  $pids = @(Get-Process -Name ms-teams -ErrorAction SilentlyContinue | ForEach-Object Id)
  if (-not $pids) { return $null }
  $A::RootElement.FindAll('Children', [System.Windows.Automation.Condition]::TrueCondition) |
    Where-Object { $pids -contains $_.Current.ProcessId -and $_.Current.Name } | Select-Object -First 1
}

function Get-TeamsPids { @(Get-Process -Name ms-teams -ErrorAction SilentlyContinue | ForEach-Object Id) }
function Get-WindowPid($h) {
  # the process that owns a top-level window (0 = none / unknown)
  if (-not $h -or [IntPtr]$h -eq [IntPtr]::Zero) { return 0 }
  [uint32]$p = 0
  [void][K.W]::GetWindowThreadProcessId([IntPtr]$h, [ref]$p)
  [int]$p
}
function Get-ForegroundPid { Get-WindowPid ([K.W]::GetForegroundWindow()) }
function Get-FocusInfo {
  # read only: where the keyboard focus is. $null when there is none / it cannot be read. The page of Teams is a WebView2, so the
  # ProcessId of the focused element may name msedgewebview2: the window that holds it (its top-level ancestor) says whose it is.
  $f = $A::FocusedElement
  if (-not $f) { return $null }
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $e = $f; $h = 0
  for ($i = 0; $e -and $i -lt 60; $i++) {
    $n = [int]$e.Current.NativeWindowHandle
    if ($n -ne 0) { $h = $n; break }
    $e = $walker.GetParent($e)
  }
  $top = [IntPtr]::Zero
  if ($h -ne 0) { $top = [K.W]::GetAncestor([IntPtr]$h, 2) }   # GA_ROOT
  [pscustomobject]@{ Type = [string]$f.Current.ControlType.ProgrammaticName; ElemPid = [int]$f.Current.ProcessId
                     Hwnd = $h; Top = [int64]$top; TopPid = (Get-WindowPid $top) }
}
function Test-TeamsInUse($w) {
  # read only: a window of Teams (the main one or a popped-out chat) is in front, or the keyboard focus is in an input box of one
  # (the person may be writing). Nothing is opened then. A failure to tell counts as in use.
  try {
    $pids = @(Get-TeamsPids)
    if ($pids -contains (Get-ForegroundPid)) { return $true }
    $fi = Get-FocusInfo
    if (-not $fi) { return $true }                # no focused element / not readable: cannot tell
    if ([int]$fi.TopPid -eq 0) { return $true }   # the window that holds it is unknown: cannot tell
    if (($pids -contains [int]$fi.TopPid) -and ($fi.Type -in @('ControlType.Edit', 'ControlType.Document'))) { return $true }
  } catch { return $true }
  $false
}
function Test-ReadClickAllowed { "$env:KIMERU_READ_CLICK" -eq '1' }   # read_click=1: readchat may bring Teams to the front and click (off by default)

function Test-SelfTitle($w) { $w -and ($w.Current.Name -match "\| [^|]+ $SELF \|") }
function Test-Selected($item) {
  try { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Current.IsSelected } catch { $false }
}

function Find-All($root, $type) {
  $root.FindAll('Descendants', (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $type)))
}

$StateDir = if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }
$NameFile = Join-Path $StateDir 'self-name.txt'
function Get-SelfName { if (Test-Path $NameFile) { (Get-Content $NameFile -Encoding UTF8 -TotalCount 1).Trim() } }

function Get-ChatItems($w) {
  # the chat list is TreeItems in some Teams layouts and ListItems in others
  @(Find-All $w $CT::TreeItem) + @(Find-All $w $CT::ListItem)
}

function Get-Raw($el, $max = 5) {
  # raw view: the unnamed Groups that carry the chat-list AutomationIds are not in FindAll's view
  $out = New-Object System.Collections.Generic.List[object]
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  $stack = New-Object System.Collections.Stack
  $stack.Push(@($el, 0))
  while ($stack.Count) {
    $e, $d = $stack.Pop()
    if ($d -ge $max) { continue }
    $k = $walker.GetFirstChild($e)
    while ($k) { $out.Add($k); $stack.Push(@($k, ($d + 1))); $k = $walker.GetNextSibling($k) }
  }
  $out
}

function Get-ChatId($item) {
  # chat-list entries contain children with AutomationIds like "title-chat-list-item_<chat id>"
  foreach ($d in (Get-Raw $item 4)) {
    if ($d.Current.AutomationId -match '^title-chat-list-item_(.+)$') { return $Matches[1] }
  }
  $null
}

function Get-ChatKind($id) {
  if ($id -eq '48:notes') { 'self' }
  elseif ($id -match '@unq\.gbl\.spaces$') { 'oneOnOne' }
  elseif ($id -match '^19:meeting_') { 'meeting' }
  elseif ($id -match '@thread\.') { 'group' }
  else { 'other' }
}

function Find-SelfItems($w) {
  $items = Get-ChatItems $w
  # 0) the self chat's id is "48:notes" in current Teams: language independent
  $byId = @($items | Where-Object { (Get-ChatId $_) -eq '48:notes' })
  if ($byId) { return @($byId | Select-Object -First 3) }
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
function Get-NotesItems($w) { @(Get-ChatItems $w | Where-Object { (Get-ChatId $_) -eq '48:notes' }) }
function Get-NotesItem($w) { Get-NotesItems $w | Select-Object -First 1 }
function Test-SelectedUp($el) {
  # the element itself or one of its 3 nearest parents reports IsSelected
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  for ($i = 0; $el -and $i -lt 4; $i++) {
    if (Test-Selected $el) { return $true }
    $el = $walker.GetParent($el)
  }
  $false
}
function Test-SelfTitleStrict($w) {
  # writes only (paste, delete, send): the window title names the learned display name exactly (case-sensitive)
  # followed by the self marker. Any "(You)" / "(自分)" chat of a colleague with a similar name does not pass.
  if (-not $w) { return $false }
  if (Test-SelfHeader $w) { return $true }
  $n = Get-SelfName
  if (-not $n) { return $false }
  [bool]($w.Current.Name -cmatch ("\| " + [regex]::Escape($n) + " $SELF \|"))
}
function Test-SelfHeader($w) {
  # newer Teams builds keep "チャット | Microsoft Teams" as the window title whatever chat is open; the header of the open
  # chat carries the chat id instead: "chat-header-48:notes" is the self chat's fixed id, which no colleague's chat has
  if (-not $w) { return $false }
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'chat-header-48:notes')
  $h = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
  [bool]($h -and $h.Current.BoundingRectangle.Height -gt 0)
}
function Ensure-Learned($w) {
  # the self chat was just opened through its fixed id (48:notes) or its title says so: remember the display
  # name once, so every later write can be checked against it exactly
  if (Get-SelfName) { return }
  if ($w -and $w.Current.Name -match "\| ([^|]+?) $SELF \|") {
    New-Item -ItemType Directory -Force (Split-Path $NameFile) | Out-Null
    [IO.File]::WriteAllText($NameFile, $Matches[1].Trim(), (New-Object Text.UTF8Encoding $false))
  }
}
function Test-SelfOpen($w) {
  # either signal is enough: the window title "チャット | <name> (あなた) | Microsoft Teams", or the
  # 48:notes entry reporting IsSelected (some list layouts report no selection at all)
  if (-not $w) { return $false }
  if (Test-SelfTitle $w) { return $true }
  [bool](@(Get-NotesItems $w | Where-Object { Test-SelectedUp $_ }).Count)
}
function Get-SelectedKinds($w) {
  # which kinds of chat are selected right now (no names): tells "opened another chat" from "nothing selected"
  @(Get-ChatItems $w | Where-Object { Test-Selected $_ } | ForEach-Object { Get-ChatKind (Get-ChatId $_) }) -join ','
}

function Get-ChatTitle($item) {
  # the chat's title as the chat list shows it (the same text `chats` reports)
  foreach ($d in (Get-Raw $item 4)) {
    if ($d.Current.AutomationId -like 'title-chat-list-item_*') {
      $t = (@(Get-Raw $d 3 | ForEach-Object { $_.Current.Name } | Where-Object { $_ }) -join ' ').Trim()
      if (-not $t) { $t = [string]$d.Current.Name }
      return $t
    }
  }
  ''
}
function Get-SelectedChatId($w) {
  foreach ($it in (Get-ChatItems $w)) { if (Test-Selected $it) { return (Get-ChatId $it) } }
  $null
}
function Get-WinChatTitle($w) {
  # the chat's name as the window title shows it: "<page> | <name> | Microsoft Teams" ('' when it does not have that shape)
  if ($w -and ([string]$w.Current.Name) -match '^[^|]+ \| (.+) \| [^|]+$') { return $Matches[1].Trim() }
  ''
}
function Test-ChatOpen($w, $id, $title) {
  # 'confirmed' = the window title names the chat AND the list selection agrees; 'title' = the list reports no selection at all,
  # so only the title agrees (the caller must check the text that was read); '' = not open.
  # Either one alone has been wrong before (a title that lags, a selection that does not move the pane).
  if (-not $w -or -not $title) { return '' }
  if (-not ([string]$w.Current.Name).Contains("| $title |")) { return '' }
  $sel = @(Get-ChatItems $w | Where-Object { Test-Selected $_ })
  if ($sel.Count) { if (@($sel | Where-Object { (Get-ChatId $_) -eq $id }).Count) { return 'confirmed' } else { return '' } }
  'title'
}
function Test-Deadline { if ($script:Deadline -and (Get-Date) -gt $script:Deadline) { Fail 'the time for reading the chat ran out; stopped' } }
function Select-Chat($w, $id, $title) {
  # select the chat's list entry (pattern, then invoke, then a click) and wait until Test-ChatOpen agrees: 'confirmed' / 'title' / '' (not confirmed).
  # The person's keyboard / mouse is checked right before each operation (Assert-Idle stops everything if it is in use).
  $item = Get-ChatItems $w | Where-Object { (Get-ChatId $_) -eq $id } | Select-Object -First 1
  if (-not $item) { return '' }
  Assert-Idle
  try { $item.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView(); Start-Sleep -Milliseconds 300 } catch {}
  $hows = @('select', 'invoke'); if (Test-ReadClickAllowed) { $hows += 'click' }   # the click needs Teams in front: not by default
  foreach ($how in $hows) {
    Assert-Idle; Test-Deadline
    try {
      switch ($how) {
        'select' { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
        'invoke' { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        'click' {
          Assert-Foreground $w
          $r = $item.Current.BoundingRectangle; $wr = $w.Current.BoundingRectangle
          if ($r.Width -le 0 -or -not $wr.Contains([int]($r.X + 40), [int]($r.Y + $r.Height / 2))) { throw 'not clickable' }
          [void][K.W]::SetCursorPos([int]($r.X + [math]::Min(60, $r.Width / 2)), [int]($r.Y + $r.Height / 2))
          [K.W]::mouse_event(2, 0, 0, 0, 0); [K.W]::mouse_event(4, 0, 0, 0, 0)
          Mark-OwnInput
        }
      }
      $script:Acted = $true   # the screen was operated (when select and invoke both throw, nothing changed: nothing to put back)
    } catch { continue }
    for ($i = 0; $i -lt 20; $i++) {
      Start-Sleep -Milliseconds 300
      $st = Test-ChatOpen (Get-TeamsWindow) $id $title
      if ($st) { return $st }
    }
  }
  ''
}
function Get-ChatMessages($w, $count) {
  # read only: the texts of the last messages in the pane to the right of the chat list (ids first, then names by position)
  $got = Get-ChatMessageRows $w
  [pscustomobject]@{ how = $got.how; messages = @(Collapse-MessageRows $got.rows $Count) }
}
function Get-ChatMessageRows($w) {
  # read only: every UIA node of the message pane that Get-ChatMessages turns into messages (one message may have several)
  $wr = $w.Current.BoundingRectangle
  $listRight = 0
  foreach ($it in (Get-ChatItems $w)) { $r = $it.Current.BoundingRectangle; if ($r.Width -gt 0 -and $r.Right -gt $listRight -and $r.Right -lt ($wr.X + $wr.Width * 0.5)) { $listRight = $r.Right } }
  $all = @($w.FindAll('Descendants', [System.Windows.Automation.Condition]::TrueCondition))
  $byId = @(); $byName = @()
  foreach ($e in $all) {
    $r = $e.Current.BoundingRectangle
    if ($r.Width -le 0 -or $r.Height -le 0 -or $r.X -lt ($listRight - 2)) { continue }
    $n = ([string]$e.Current.Name).Trim()
    if ($n.Length -lt 1) { continue }
    if ([string]$e.Current.AutomationId -match '^(message-body|content-|body-)') { $byId += [pscustomobject]@{ y = $r.Y; h = $r.Height; text = $n; container_id = (Get-MessageContainerIdentity $e) } }
    elseif ($n.Length -ge 4 -and $r.Y -gt ($wr.Y + $wr.Height * 0.12) -and $r.Y -lt ($wr.Y + $wr.Height * 0.88) -and
            $e.Current.ControlType -in @($CT::ListItem, $CT::Group, $CT::Text)) { $byName += [pscustomobject]@{ y = $r.Y; h = $r.Height; text = $n; container_id = (Get-MessageContainerIdentity $e) } }
  }
  $how = if ($byId.Count) { 'ids' } else { 'names' }
  [pscustomobject]@{ how = $how; rows = @(if ($byId.Count) { $byId } else { $byName }) }
}

function Find-PostedReadback([string]$text, $priorIds, [int]$priorCount, $priorAllIds) {
  for ($i = 0; $i -lt 20; $i++) {
    $current = Get-TeamsWindow
    if (-not (Test-SelfTitleStrict $current)) { return @{ matched = $false; message_id = '' } }
    $read = Get-ChatMessages $current 100
    $found = Find-NewMatchingMessage $read.messages $text $priorIds $priorCount $priorAllIds
    if ($found.matched) { return $found }
    Start-Sleep -Milliseconds 250
  }
  @{ matched = $false; message_id = '' }
}

function Mask($s) {
  # keep short parentheticals like "(自分)" and punctuation; letters/digits become x
  $e = [System.Text.RegularExpressions.MatchEvaluator] { param($m) if ($m.Value.StartsWith('(')) { $m.Value } else { 'x' } }
  $o = [regex]::Replace([string]$s, '\([^)]{1,8}\)|[\p{L}\p{N}]+', $e)
  if ($o.Length -gt 60) { $o.Substring(0, 60) } else { $o }
}

function Show-ChatApp($w) {
  # Teams may be on Activity / Calendar / Teams: then there is no chat list to search
  $btn = @(Find-All $w $CT::Button) + @(Find-All $w $CT::TabItem) + @(Find-All $w $CT::ListItem) |
    Where-Object { $_.Current.Name -match '^(チャット|Chat)(\s|$|\(|（|,)' } | Select-Object -First 1
  if (-not $btn) { return $false }
  # the page was switched (Activity / Calendar -> Chat): that is an operation, so readchat puts things back afterwards
  try { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); $script:Acted = $true; $script:PageSwitched = $true; return $true } catch {}
  try { $btn.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); $script:Acted = $true; $script:PageSwitched = $true; return $true } catch {}
  $false
}

function Test-ListItemFocused {
  try {
    $f = $A::FocusedElement
    for ($i = 0; $f -and $i -lt 4; $i++) {
      if ([string]$f.Current.AutomationId -like 'new-message-*' -or $f.Current.ControlType -eq $CT::Edit) { return $false }
      if ($f.Current.ControlType -in @($CT::ListItem, $CT::TreeItem)) { return $true }
      $f = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($f)
    }
  } catch {}
  $false
}

function Open-SelfChat($w) {
  if (Test-SelfOpen $w) { return $w }
  $cands = @(Find-SelfItems $w)
  if (-not $cands -and (Show-ChatApp $w)) { Start-Sleep -Seconds 2; $w = Get-TeamsWindow; $cands = @(Find-SelfItems $w) }
  if (-not $cands) {
    # Teams is on another app page (Activity, Calendar, Copilot...) and its chat button was not found:
    # the self chat has a fixed link (48:notes), which opens it without any list
    try { Start-Process 'msteams:/l/chat/48:notes/conversations' } catch {}
    for ($i = 0; $i -lt 16; $i++) {
      Start-Sleep -Milliseconds 500
      $w = Get-TeamsWindow
      if (Test-SelfOpen $w) { return $w }
      $cands = @(Find-SelfItems $w)
      if ($cands) { break }
    }
  }
  for ($i = 0; -not $cands -and $i -lt 10; $i++) {   # Teams just started: the chat list fills in a few seconds
    Start-Sleep -Seconds 1
    $w = Get-TeamsWindow
    if ($w) { $cands = @(Find-SelfItems $w) }
  }
  if (-not $cands) { Fail 'self chat not found in chat list (open it once by hand and run -Action learn)' }
  $tried = New-Object System.Collections.Generic.List[string]
  $rectNote = 'none'
  foreach ($item in $cands) {   # a candidate may be a message rather than the chat entry: verify after each try
    # a long list is virtualized: bring the entry into view first, and note whether it is inside the window
    try { $item.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView(); Start-Sleep -Milliseconds 400 } catch {}
    $wr = $w.Current.BoundingRectangle; $ir = $item.Current.BoundingRectangle
    $rectNote = if ($ir.Width -le 0) { 'empty' } elseif ($ir.X -ge $wr.X -and $ir.Y -ge $wr.Y -and ($ir.X + $ir.Width) -le ($wr.X + $wr.Width) -and ($ir.Y + $ir.Height) -le ($wr.Y + $wr.Height)) { 'inside' } else { 'outside' }
    foreach ($how in 'select', 'link', 'invoke', 'click', 'enter') {
      try {
        switch ($how) {
          'select' { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
          'link' {
            # the self chat's id is 48:notes: a Teams deep link opens it directly (language independent)
            Start-Process 'msteams:/l/chat/48:notes/conversations'
          }
          'invoke' { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
          'click' {
            # new Teams may mark the entry selected via UIA without navigating: click it like a person would
            Assert-Foreground $w
            $r = $item.Current.BoundingRectangle
            if ($r.Width -le 0 -or $rectNote -ne 'inside') { throw 'not clickable' }
            [void][K.W]::SetCursorPos([int]($r.X + [math]::Min(60, $r.Width / 2)), [int]($r.Y + $r.Height / 2))
            [K.W]::mouse_event(2, 0, 0, 0, 0); [K.W]::mouse_event(4, 0, 0, 0, 0)
          }
          'enter' {
            # keyboard: focus the entry and press Enter, only while Teams is in front
            Assert-Foreground $w
            $item.SetFocus(); Start-Sleep -Milliseconds 200
            Assert-Foreground $w
            # Enter goes to whatever has the focus: only an item of the chat list may have it (a compose box would send its draft)
            if (-not (Test-ListItemFocused)) { throw 'focus is not on a chat list item' }
            [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
          }
        }
        $tried.Add("${how}:ok")
      } catch { $tried.Add("${how}:err"); continue }
      for ($i = 0; $i -lt 24; $i++) {   # up to 6 s: opening a chat can take a while on a busy Teams
        Start-Sleep -Milliseconds 250
        $w = Get-TeamsWindow
        if (Test-SelfOpen $w) { return $w }
      }
    }
  }
  $notes = if ($w) { @(Get-NotesItems $w) } else { @() }
  $sel = if ($w) { Get-SelectedKinds $w } else { '' }
  Fail ("self chat did not open (tried " + ($tried -join ',') + "; entry " + $rectNote + "; notes entries " + $notes.Count +
        ", selected chats: " + $(if ($sel) { $sel } else { 'none' }) + ")")
}

if ($Action -eq 'test-compare') {
  Out-Json @{ matches = (Test-MeaningfulTextMatch $Text $ReadbackText) }; exit 0
}
if ($Action -eq 'test-failure') {
  switch ($TestDeliveryState) {
    'pre-send' { $script:DeliveryTyped = $false; $script:DeliveryAttempted = $false; $script:DeliverySent = $false }
    'attempted' { $script:DeliveryTyped = $false; $script:DeliveryAttempted = $true; $script:DeliverySent = $false }
    'sent' { $script:DeliveryTyped = $true; $script:DeliveryAttempted = $true; $script:DeliverySent = $true }
  }
  Out-Json (Get-DeliveryFailureEvidence 'test failure'); exit 0
}
if ($Action -eq 'test-send-once') {
  if (-not $TestRows -or -not (Test-Path -LiteralPath $TestRows)) { Out-Json @{ error = 'test scenario file not found' }; exit 2 }
  $script:SendScenario = Get-Content -Raw -Encoding UTF8 -LiteralPath $TestRows | ConvertFrom-Json
  $script:SendCounts = @{ invokes = 0; keys = 0; key_values = @() }
  $script:DeliveryTyped = $true
  $guard = Get-SendGuardFailure ([bool]$script:SendScenario.self_ok) ([bool]$script:SendScenario.compose_ok)
  if (-not $guard -and -not [bool]$script:SendScenario.button_present) {
    $foregroundOk = if ($null -eq $script:SendScenario.foreground_ok) { $true } else { [bool]$script:SendScenario.foreground_ok }
    $guard = Get-KeySendGuardFailure $foregroundOk ([bool]$script:SendScenario.focused)
  }
  $via = 'guarded'
  if (-not $guard) {
    $invoke = { param($button) $script:SendCounts.invokes++; if ($script:SendScenario.invoke_throws) { throw 'mock invoke failure' } }
    $key = { param($value) $script:SendCounts.keys++; $script:SendCounts.key_values += $value; if ($script:SendScenario.key_throws) { throw 'mock key failure' } }
    $wait = { [bool]$script:SendScenario.wait_sent }
    $via = Invoke-SendOnce ([bool]$script:SendScenario.button_present) $invoke $key $wait ([pscustomobject]@{ mock = $true })
  }
  Out-Json @{ via = $via; guard = $guard; invokes = $script:SendCounts.invokes; keys = $script:SendCounts.keys;
              key_values = @($script:SendCounts.key_values); attempted = $script:DeliveryAttempted;
              sent = (Get-SentEvidence) }; exit 0
}
if ($Action -eq 'test-messages') {
  if (-not $TestRows -or -not (Test-Path -LiteralPath $TestRows)) { Fail 'test rows file not found' }
  $rows = @(Get-Content -Raw -Encoding UTF8 -LiteralPath $TestRows | ConvertFrom-Json)
  Out-Json @{ messages = @(Collapse-MessageRows $rows 100) }; exit 0
}
if ($Action -eq 'test-readback') {
  if (-not $TestRows -or -not (Test-Path -LiteralPath $TestRows)) { Fail 'test rows file not found' }
  $rows = @(Get-Content -Raw -Encoding UTF8 -LiteralPath $TestRows | ConvertFrom-Json)
  $prior = New-Object 'System.Collections.Generic.HashSet[string]'
  foreach ($id in ($PriorIds -split ',')) { if ($id) { [void]$prior.Add($id) } }
  $priorAll = New-Object 'System.Collections.Generic.HashSet[string]'
  foreach ($id in ($PriorAllIds -split ',')) { if ($id) { [void]$priorAll.Add($id) } }
  $messages = @(Collapse-MessageRows $rows 100)
  Out-Json @{ readback = (Find-NewMatchingMessage $messages $Text $prior $PriorCount $priorAll) }; exit 0
}
if ($Action -eq 'test-container') {
  if (-not $TestRows -or -not (Test-Path -LiteralPath $TestRows)) { Fail 'test rows file not found' }
  $snapshot = Get-Content -Raw -Encoding UTF8 -LiteralPath $TestRows | ConvertFrom-Json
  Out-Json @{ identity = (Select-MessageContainerId $snapshot.ancestors ([string]$snapshot.fallback)) }; exit 0
}
if ($Action -in 'open', 'post', 'send', 'read', 'readchat') { Enter-UiLock $(if ($Action -eq 'readchat') { 60000 } else { 180000 }) }
if ($Action -eq 'probe') { Enter-UiLock 180000 }   # the read the approvals step makes: one Teams operation at a time
if ($Action -in 'post', 'send') { Wait-UserIdle 3 60 }   # writing moves the screen: not while the person works. readchat does not wait: it checks and postpones
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
  # where the keyboard focus is, as numbers and type names only (never text): what Test-TeamsInUse decides from. T24 puts it on the sheet
  $focus = try { $fi = Get-FocusInfo; if ($fi) { "{0} pid={1} hwnd={2} top={3} topPid={4} fgPid={5} teamsPids={6}" -f $fi.Type, $fi.ElemPid, $fi.Hwnd, $fi.Top, $fi.TopPid, (Get-ForegroundPid), (@(Get-TeamsPids) -join '/') } else { 'none' } } catch { 'error' }
  $inUse = try { [bool](Test-TeamsInUse $w) } catch { 'error' }
  # why the chat list may read as empty: how many items carry a chat id, and which part names (AutomationIds before the
  # first '_', letters, digits and '-' only; never names or text) the items hold. A Teams update that renames them shows here
  $withId = @($items | Where-Object { Get-ChatId $_ }).Count
  $parts = @{}
  foreach ($i in ($items | Select-Object -First 40)) {
    foreach ($d in (Get-Raw $i 4)) {
      $p = ([string]$d.Current.AutomationId -replace '_.*$', '')
      if ($p -match '^[A-Za-z][A-Za-z0-9-]{1,40}$') { $parts[$p] = 1 + [int]$parts[$p] }
    }
  }
  $partList = @($parts.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 12 | ForEach-Object { "$($_.Key)=$($_.Value)" })
  # most useful fields first: the result sheet truncates long lines
  Out-Json ([ordered]@{ ok = $true; focus = $focus; teamsInUse = $inUse; selfItemFound = [bool](Find-SelfItem $w); learnedName = [bool](Get-SelfName)
              parenMarkers = $found; titleShape = $shape; treeItems = $tree.Count; listItems = $list.Count; selectedTabs = $tabs
              itemsWithChatId = $withId; itemParts = $partList
              itemShapes = @($items | Select-Object -First 8 | ForEach-Object { Mask $_.Current.Name }) })
  exit 0
}
function Get-PreviewHead([string]$p) {
  # the part of a list preview that names the chat's content: no ellipsis, at most 40 characters
  $t = (($p -replace '\s+', ' ').Trim()).TrimEnd(' ', '.', [char]0x2026)
  if ($t.Length -gt 40) { $t.Substring(0, 40) } else { $t }
}
function Test-PreviewMatch($messages, [string]$head) {
  # the last message read holds the preview (with or without a leading "name: "); a head shorter than 12 characters matches too much
  if ($head.Length -lt 12 -or -not $messages -or -not @($messages).Count) { return $false }
  $last = ((@($messages)[-1].text -replace '\s+', ' ')).Trim()
  if ($last.Contains($head)) { return $true }
  if ($head -match '^[^:：]{1,30}[:：]\s*(.{12,})$') { return $last.Contains($Matches[1]) }
  $false
}
function Test-OrigOpen($w, $origId, $origTitle) {
  if ($origId -eq '48:notes') { return [bool](Test-SelfOpen $w) }
  if ($origTitle -and ([string]$w.Current.Name).Contains("| $origTitle |")) {
    $sel = @(Get-ChatItems $w | Where-Object { Test-Selected $_ })
    if ($origId -and $sel.Count) { return [bool](@($sel | Where-Object { (Get-ChatId $_) -eq $origId }).Count) }
    return $true
  }
  $false
}
function Select-Notes($w) {
  # the self chat, by the ways that touch no input box: the list entry (pattern, invoke), then the fixed deep link
  if (-not $w) { return $false }
  if (Test-SelfOpen $w) { return $true }
  $item = Get-NotesItem $w
  foreach ($how in 'select', 'invoke', 'link') {
    if (-not (Test-UserIdle)) { return $false }
    if (-not $item -and $how -ne 'link') { continue }
    try {
      switch ($how) {
        'select' { $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
        'invoke' { $item.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        'link' { Save-Foreground $w; Start-Process 'msteams:/l/chat/48:notes/conversations' }
      }
    } catch { continue }
    for ($i = 0; $i -lt 16; $i++) { Start-Sleep -Milliseconds 300; if (Test-SelfOpen (Get-TeamsWindow)) { return $true } }
  }
  $false
}
function Restore-Original($origId, $origTitle) {
  # put the chat that was open back; otherwise the self chat. 'restored' | 'self' | 'failed'. Never operates while the person is
  # using the keyboard / mouse (it waits a little, then gives up: the caller reports it). A time-out or an interruption while putting
  # the original back does not skip the self chat.
  $script:Deadline = (Get-Date).AddSeconds(40)
  $script:Restoring = $true   # Get-IdleNeed: the short threshold (3 s), not the 30 s that guards opening a colleague's chat
  $hasOrig = [bool]($origId -or $origTitle)
  try {   # read only first: when the chat that was open is still open, there is nothing to put back and nothing to wait for
    $w0 = Get-TeamsWindow
    if ($hasOrig -and $w0 -and (Test-OrigOpen $w0 $origId $origTitle)) { return 'restored' }
  } catch {}
  try {
    if (-not (Wait-IdleSoft 3 20)) { return 'failed' }
    if (-not (Get-TeamsWindow)) { return 'failed' }
  } catch { return 'failed' }
  if ($hasOrig) {
    try {
      $w = Get-TeamsWindow
      if (Test-OrigOpen $w $origId $origTitle) { return 'restored' }
      $target = $null
      if ($origId) { $target = Get-ChatItems $w | Where-Object { (Get-ChatId $_) -eq $origId } | Select-Object -First 1 }
      if (-not $target -and $origTitle) {
        $m = @(Get-ChatItems $w | Where-Object { (Get-ChatTitle $_) -eq $origTitle })
        if ($m.Count -eq 1) { $target = $m[0] }
      }
      if ($target) {
        $tid = Get-ChatId $target; $ttl = Get-ChatTitle $target
        if ($tid -and $ttl -and (Select-Chat $w $tid $ttl)) { return 'restored' }
      }
    } catch {}
  }
  try { if (Select-Notes (Get-TeamsWindow)) { return 'self' } } catch {}
  'failed'
}

if ($Action -eq 'readchat') {
  # Opens one chat, reads its last messages, and puts the chat that was open back. Read only: the input box is never touched.
  # Every way out (a failure, the person using the PC, the time limit) goes through the put-back below.
  $script:InRead = $true
  $script:Deadline = (Get-Date).AddSeconds(90)
  $errMsg = ''; $origId = $null; $origTitle = ''; $state = ''; $matched = $false; $read = $null
  $script:Acted = $false   # set by the first screen operation (Show-ChatApp, Select-Chat): nothing changed before it, so nothing needs putting back
  $script:PageSwitched = $false
  try {
    if (-not $w) { Fail 'Teams window not found' }
    if (-not $ChatId -or $ChatId -eq '48:notes') { Fail 'readchat needs the id of another chat (not the self chat)' }
    # read only checks before anything is opened: the person is not using the PC, Teams is not in front, and no input box has the focus
    Assert-Idle
    if (Test-TeamsInUse $w) { Fail 'Teams is in use (it is in front, or an input box has the focus); nothing was opened' }
    if (-not (Get-ChatItems $w).Count) { Assert-Idle; if (Show-ChatApp $w) { Start-Sleep -Seconds 2; $w = Get-TeamsWindow } }
    $item = Get-ChatItems $w | Where-Object { (Get-ChatId $_) -eq $ChatId } | Select-Object -First 1
    if (-not $item) { Fail 'the chat is not in the list on screen; nothing was opened' }
    $title = Get-ChatTitle $item
    if (-not $title) { Fail 'the chat has no title to check the screen against; nothing was opened' }
    $origId = Get-SelectedChatId $w
    $origTitle = Get-WinChatTitle $w
    $head = Get-PreviewHead $Preview
    # the text read is checked against the preview on every screen: it needs a preview long enough to mean something
    if ($head.Length -lt 12) { Fail 'the preview is too short to tell the chat apart on this screen; nothing was opened' }
    # a screen whose list reports no selection cannot tell two chats of the same title apart (a recurring meeting, two people with the
    # same name), and when the chat that is open is the one to read there is nothing to open: the screen is not touched (nothing is
    # operated, so nothing needs putting back), nothing is read, and the preview decides. The same test as Test-ChatOpen
    if (-not $origId -and (Test-ChatOpen $w $ChatId $title)) { Fail 'the chat that is open has the title of the chat to read, so they cannot be told apart on this screen; nothing was opened, nothing was read' }
    $state = Select-Chat $w $ChatId $title
    if (-not $state) { Fail 'could not confirm that the chat is open (the title and the selection did not agree); nothing was read' }
    $w = Get-TeamsWindow
    $read = Get-ChatMessages $w $Count
    $matched = Test-PreviewMatch $read.messages $head
    if ($state -eq 'title' -and -not $matched) { Fail 'the chat that opened does not match the preview; nothing was used' }
  } catch {
    $m = [string]$_.Exception.Message
    $errMsg = if ($m.StartsWith('KFAIL:')) { $m.Substring(6) } else { 'unexpected error while reading the chat (' + $_.Exception.GetType().Name + ')' }
  }
  $restore = 'none'
  if ($script:Acted) { $restore = if ($origId -and $origId -eq $ChatId -and -not $script:PageSwitched) { 'unchanged' } else { Restore-Original $origId $origTitle } }
  # the page (Activity / Calendar) that Show-ChatApp left cannot be gone back to: the person is told the original was not put back
  if ($script:PageSwitched -and $restore -in 'restored', 'unchanged') { $restore = 'failed' }
  Restore-Foreground
  $had = [bool]($origId -or $origTitle -or $script:PageSwitched)
  $returned = [bool]($restore -in 'restored', 'unchanged', 'none')
  if ($errMsg) { Out-Json ([ordered]@{ ok = $false; error = $errMsg; restore = $restore; returned = $returned; hadOriginal = $had }); exit 2 }
  Out-Json ([ordered]@{ ok = $true; opened = $true; how = $read.how; messages = @($read.messages); returned = $returned; restore = $restore
                        hadOriginal = $had; verified = $state; previewMatched = $matched })
  exit 0
}

if ($Action -eq 'chats') {
  if (-not $w) { Fail 'Teams window not found' }
  $name = Get-SelfName
  $rows = foreach ($it in (Get-ChatItems $w)) {
    $id = Get-ChatId $it
    if (-not $id) { continue }
    $f = @{}
    foreach ($d in (Get-Raw $it 4)) {
      $aid = $d.Current.AutomationId
      foreach ($k in 'title', 'time', 'message-preview') {
        if ($aid -like "$k-chat-list-item_*") {
          $txt = (@(Get-Raw $d 3 | ForEach-Object { $_.Current.Name } | Where-Object { $_ }) -join ' ').Trim()
          if (-not $txt) { $txt = $d.Current.Name }
          $f[$k] = $txt
        }
      }
    }
    $full = $it.Current.Name
    $preview = [string]$f['message-preview']
    [ordered]@{
      id = $id; kind = (Get-ChatKind $id); title = [string]$f['title']; preview = $preview; time = [string]$f['time']
      unread = [bool]($full -match '未読|Unread|新しいメッセージ|New message')
      mention = [bool]($full -match 'メンション|mentioned' -or ($name -and $preview -match ('@\s*' + [regex]::Escape($name))))
    }
  }
  Out-Json ([ordered]@{ ok = $true; chats = @($rows) })
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
if ($Action -eq 'status') { Out-Json @{ ok = $true; teams = [bool]$w; selfChatOpen = [bool](Test-SelfOpen $w) }; exit 0 }
if (-not $w) { Fail 'Teams window not found' }
$w = Open-SelfChat $w
Ensure-Learned $w
if ($Action -eq 'open') { Out-Json @{ ok = $true; selfChatOpen = $true }; exit 0 }
if ($Action -eq 'probe') {
  # read only (nothing is typed, pasted or sent): the messages exactly as the send readback compares them (the first node of each
  # message, normalized), plus how many nodes each message has, all of them joined, and its place on the screen. The texts go to
  # `kimeru diagnose` through this pipe only; it keeps lengths and yes/no answers and prints no text.
  $got = Get-ChatMessageRows $w
  $wr = $w.Current.BoundingRectangle
  $groups = [ordered]@{}
  foreach ($row in @($got.rows | Sort-Object { [double]$_.y })) {
    $id = [string]$row.container_id
    if (-not $id) { continue }
    if (-not $groups.Contains($id)) { $groups[$id] = New-Object System.Collections.Generic.List[object] }
    $groups[$id].Add($row)
  }
  $msgs = @(foreach ($id in $groups.Keys) {
    $g = $groups[$id]
    $top = [double]$g[0].y; $bottom = [double]$g[0].y + [double]$g[0].h
    foreach ($row in $g) { $bottom = [math]::Max($bottom, [double]$row.y + [double]$row.h) }
    [ordered]@{ text = (Normalize-MeaningfulText ([string]$g[0].text)); raw_lines = @(([string]$g[0].text) -split "`n").Count
                fragments = $g.Count; joined = (Normalize-MeaningfulText ((@($g | ForEach-Object { [string]$_.text })) -join ' '))
                top = [math]::Round(($top - $wr.Y) / [math]::Max(1, $wr.Height), 2); height = [math]::Round(($bottom - $top) / [math]::Max(1, $wr.Height), 2) }
  })
  Out-Json ([ordered]@{ ok = $true; how = $got.how; rows = @($got.rows).Count; selfTitle = [bool](Test-SelfTitleStrict $w)
                        messages = @($msgs | Select-Object -Last 100) })
  exit 0
}

function Get-Box($w) {
  $b = Find-All $w $CT::Edit | Where-Object { $_.Current.AutomationId -like 'new-message-*' } | Select-Object -First 1
  if (-not $b) { Fail 'compose box not found' }
  $b
}
function Get-BoxText($b) {
  try { return $b.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(4000) }
  catch { try { return $b.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { return '' } }
}
function Squash($s) { ([string]$s) -replace '[\s\u00a0\u200b\ufeff]', '' }   # compare text ignoring line-break/space rendering
function Test-BoxFocused() {
  # Ctrl+A / Delete / Enter go to whatever has the keyboard focus: it must be the compose box
  try { $f = $A::FocusedElement; return [bool]($f -and $f.Current.AutomationId -like 'new-message-*') } catch { return $false }
}
function Clear-OurBox($w, $box) {
  # only called when the box starts with "[kimeru": select all and delete, then check it is empty
  if (-not (Test-SelfTitleStrict (Get-TeamsWindow))) { return $false }
  Assert-Foreground $w
  $box.SetFocus(); Start-Sleep -Milliseconds 150
  Assert-Foreground $w
  if (-not (Test-BoxFocused)) { return $false }
  [System.Windows.Forms.SendKeys]::SendWait('^a'); Start-Sleep -Milliseconds 100
  [System.Windows.Forms.SendKeys]::SendWait('{DEL}'); Start-Sleep -Milliseconds 300
  -not (Get-BoxText $box).Trim().StartsWith('[kimeru')
}
function Test-BoxHasKimeru($w) { (Get-BoxText (Get-Box $w)).Trim().StartsWith('[kimeru') }

function Wait-Sent($w) {
  for ($i = 0; $i -lt 12; $i++) { Start-Sleep -Milliseconds 250; if (-not (Test-BoxHasKimeru $w)) { return $true } }
  $false
}
function Send-KeyOnce($key) { [System.Windows.Forms.SendKeys]::SendWait($key) }

function Send-Box($w) {
  # send only what kimeru wrote: the self chat (title with the learned name) + compose box starts with [kimeru
  $guard = Get-SendGuardFailure (Test-SelfTitleStrict (Get-TeamsWindow)) (Test-BoxHasKimeru $w)
  if ($guard) { Fail $guard }
  # 1) the Send button: works whether Enter or Ctrl+Enter sends in this user's Teams settings
  $btn = Find-All $w $CT::Button | Where-Object { $_.Current.Name -match '^(送信|Send)(\s*\(|$)' -and $_.Current.IsEnabled } | Select-Object -First 1
  if ($btn) {
    $invoke = { param($target) $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    $wait = { Wait-Sent $w }
    # A button invocation is the only send action in this call, even if its response is delayed or throws.
    return Invoke-SendOnce $true $invoke {} $wait $btn
  }
  # With no button, use the configured legacy key fallback once, after checking its focus target.
  $guard = Get-SendGuardFailure (Test-SelfTitleStrict (Get-TeamsWindow)) (Test-BoxHasKimeru $w)
  if ($guard) { Fail $guard }
  Assert-Foreground $w
  (Get-Box $w).SetFocus(); Start-Sleep -Milliseconds 150
  Assert-Foreground $w
  $guard = Get-KeySendGuardFailure $true (Test-BoxFocused)
  if ($guard) { Fail $guard }
  $sendKey = { param($key) Send-KeyOnce $key }
  $wait = { Wait-Sent $w }
  return Invoke-SendOnce $false {} $sendKey $wait
}

if ($Action -eq 'send') {
  $box = Get-Box $w
  $Text = Get-BoxText $box
  if (-not $Text.Trim().StartsWith('[kimeru')) { Fail 'compose box does not start with [kimeru; not sending' }
  $script:DeliveryTyped = $true
  $beforeWindow = Get-TeamsWindow
  if (-not (Test-SelfTitleStrict $beforeWindow)) { Fail 'the window title changed before the self-chat readback baseline; nothing sent' }
  # Leave one slot for the message we expect to add; otherwise a 100-item window
  # would always evict its oldest prior ID on a successful send.
  $baseMessages = @(Get-ChatMessages $beforeWindow 99).messages
  $baseIds = New-Object 'System.Collections.Generic.HashSet[string]'
  $baseAllIds = New-Object 'System.Collections.Generic.HashSet[string]'
  $baseCount = 0
  foreach ($old in $baseMessages) {
    [void]$baseAllIds.Add([string]$old.message_id)
    if (Test-PostedTextMatch $Text ([string]$old.text)) { $baseCount++; [void]$baseIds.Add([string]$old.message_id) }
  }
  $baseAllIds = Get-AnchorIds $baseMessages
  $how = Send-Box $w
  $readback = Find-PostedReadback $Text $baseIds $baseCount $baseAllIds
  if ($readback.matched) { $script:DeliverySent = $true }
  Out-Json @{ ok = $true; typed = $script:DeliveryTyped; sent = (Get-SentEvidence); via = $how; readback = $readback }; exit 0
}

if ($Action -eq 'post') {
  if (-not $Text.StartsWith('[kimeru')) { Fail 'refusing to post text that does not start with [kimeru' }
  $Text = $Text -replace '\\n', "`r`n"   # callers pass line breaks as literal \n
  $box = Get-Box $w
  $prev = $null
  for ($i = 0; $i -lt 10; $i++) {   # the previous send may still be clearing the box: wait until it stops changing
    $cur = Get-BoxText $box
    if ($cur -eq $prev -and -not $cur.Trim().StartsWith('[kimeru')) { break }
    $prev = $cur; Start-Sleep -Milliseconds 300
  }
  if (-not (Test-SelfTitleStrict (Get-TeamsWindow))) { Fail 'the window title is not the self chat (learned name); nothing pasted' }
  $before = Squash (Get-BoxText $box)
  # a kimeru post left in the box by an earlier failed run is ours to remove; anything else is the
  # person's own draft: nothing is clicked or pasted (an empty box reads as its placeholder text)
  if ((Get-BoxText $box).Trim().StartsWith('[kimeru')) {
    if (-not (Clear-OurBox $w $box)) { Fail 'an earlier kimeru post is left in the compose box and could not be cleared; clear it in Teams and retry' }
    $before = Squash (Get-BoxText $box)
  } elseif ($before.Length -gt 2 -and $before -ne (Squash ([string]$box.Current.Name))) {
    Fail ("the compose box holds a draft of yours ($($before.Length) characters); nothing pasted, will retry later")
  }
  $saved = $null
  try { $saved = [System.Windows.Forms.Clipboard]::GetText() } catch {}
  try {
    [System.Windows.Forms.Clipboard]::SetText($Text)
    # Teams rebuilds the compose box after a send: take a fresh element each try and click into it,
    # then check the paste landed (the empty box reads as its placeholder, so compare with before)
    for ($try = 0; $try -lt 2; $try++) {
      $box = Get-Box $w
      Assert-Foreground $w
      $box.SetFocus(); Start-Sleep -Milliseconds 200
      $r = $box.Current.BoundingRectangle
      if ($r.Width -gt 0) {
        [void][K.W]::SetCursorPos([int]($r.X + [math]::Min(80, $r.Width / 2)), [int]($r.Y + $r.Height / 2))
        [K.W]::mouse_event(2, 0, 0, 0, 0); [K.W]::mouse_event(4, 0, 0, 0, 0); Start-Sleep -Milliseconds 200
      }
      Assert-Foreground $w
      [System.Windows.Forms.SendKeys]::SendWait('^v'); Start-Sleep -Milliseconds 400
      if ((Squash (Get-BoxText (Get-Box $w))) -ne $before) { break }
    }
  } finally {
    if ($saved) { [System.Windows.Forms.Clipboard]::SetText($saved) } else { [System.Windows.Forms.Clipboard]::Clear() }
  }
  $box = Get-Box $w
  # the box must hold exactly the planned text: an old draft left in the box would otherwise go out with it
  $typed = Test-MeaningfulTextMatch (Get-BoxText $box) $Text
  $script:DeliveryTyped = $typed
  $sent = $false
  $readback = @{ matched = $false; message_id = '' }
  if ($Send) {
    if (-not $typed) {
      # take our paste back out when the box holds only kimeru text; a person's draft stays as it was
      $cleared = (Get-BoxText $box).Trim().StartsWith('[kimeru') -and (Clear-OurBox $w $box)
      $got = Squash (Get-BoxText $box); $want = Squash $Text
      $shape = "before=$($before.Length) box=$($got.Length) planned=$($want.Length) endsWith=$($got.EndsWith($want)) startsWith=$($got.StartsWith($want))"
      Fail ("compose box does not hold exactly the planned text ($shape); not sent" +
            $(if ($cleared) { '; the pasted text was removed again' } else { '; clear the box in Teams and retry' }))
    }
    $baseIds = New-Object 'System.Collections.Generic.HashSet[string]'
    $baseAllIds = New-Object 'System.Collections.Generic.HashSet[string]'
    $beforeWindow = Get-TeamsWindow
    if (-not (Test-SelfTitleStrict $beforeWindow)) { Fail 'the window title changed before the self-chat readback baseline; nothing sent' }
    $baseMessages = @(Get-ChatMessages $beforeWindow 99).messages
    $baseCount = 0
    foreach ($old in $baseMessages) {
      [void]$baseAllIds.Add([string]$old.message_id)
      if (Test-PostedTextMatch $Text ([string]$old.text)) {
        $baseCount++
        [void]$baseIds.Add([string]$old.message_id)
      }
    }
    $baseAllIds = Get-AnchorIds $baseMessages
    [void](Send-Box $w); $sent = Get-SentEvidence
    $readback = Find-PostedReadback $Text $baseIds $baseCount $baseAllIds
    if ($readback.matched) { $script:DeliverySent = $true; $sent = $true }
  }
  Out-Json @{ ok = $true; typed = $typed; sent = $sent; readback = $readback }; exit 0
}

if ($Action -eq 'read') {
  $posts = New-Object System.Collections.Generic.List[string]
  $replies = New-Object System.Collections.Generic.List[string]
  $seen = @{}
  # timeline: posts ("P:N"), result posts ("X:N:k") and replies ("R:OK N") in screen order (oldest first). One message is
  # exposed by several UIA nodes, so consecutive duplicates are collapsed.
  $timeline = New-Object System.Collections.Generic.List[string]
  $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
  function Walk($el, $d) {
    if ($d -gt 40) { return }
    $isPost = ([string]$el.Current.Name).TrimStart().StartsWith('[kimeru')   # our post: only its first line counts
    $li = -1
    foreach ($line in ([string]$el.Current.Name) -split "`n") {
      $li++
      if ($isPost -and $li -gt 0) { continue }
      $l = $line.Trim()
      $entry = $null
      if ($l -match '^\[kimeru #(\d+)\]') {
        $entry = "P:" + $Matches[1]
        if (-not $posts.Contains($Matches[1])) { $posts.Add($Matches[1]) }
      }
      elseif ($l -match '^\[kimeru 試験 #(\d+)\]') {
        # a post of tools/check.ps1 (a test): it is not an approval post and not a boundary for the real reader (notify.scoped_timeline)
        $entry = "T:" + $Matches[1]
      }
      elseif ($l -match '^\[kimeru 試験 実行 #(\d+)(?:\s+(\d+))?\]') {
        $entry = 'TX:{0}:{1}' -f $Matches[1], $(if ($Matches[2]) { [int]$Matches[2] } else { 0 })
      }
      elseif ($l -match '^\[kimeru 実行 #(\d+)(?:\s+(\d+))?\]') {
        # a result post about #N, the k-th one ("[kimeru 実行 #N k]"; 0 for a post of an earlier version that has no count):
        # replies to #N before it were answered (notify.fresh_entries). The count keeps two result posts from being folded into one line.
        $entry = 'X:{0}:{1}' -f $Matches[1], $(if ($Matches[2]) { [int]$Matches[2] } else { 0 })
      }
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^(?i)(OK|NG|保留|聞き返し|再実行|済)\s*#?(\d+)\s*[.。!！]*$') {
        # phones often send full-width or re-cased text ("ＯＫ　６７５", "Ok 675"): canonicalize
        $c = '{0} {1}' -f $Matches[1].ToUpper(), $Matches[2]
        $entry = "R:" + $c
        if (-not $seen.ContainsKey($c)) { $seen[$c] = 1; $replies.Add($c) }
      }
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^再実行\s*#?(\d+)\s*[-ー−‐‑‒–—―─ｰ]\s*(\d+)\s*[.。!！]*$') {
        # `再実行 N-k`: run again, under the k-th result post of #N (notify.parse_redo_k). Every dash a phone or an IME may type is read as "-"
        $c = '再実行 {0}-{1}' -f $Matches[1], $Matches[2]
        $entry = "R:" + $c
        if (-not $seen.ContainsKey($c)) { $seen[$c] = 1; $replies.Add($c) }
      }
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^再実行\s*#?(\d+)(\s*[^\d\s.。!！]{1,6}\s*|\s+)(\d*)\s*[.。!！]*$') {
        # begins like `再実行 N-k` but with something between N and k that is not a dash (a slash, a wave, a space, a letter...): handed back as
        # "R:再実行形式 N <rest>" so that kimeru decides and records it (notify.redo_bad_kind); only a hash of its shape is ever kept
        $c = '再実行形式 {0} {1}' -f $Matches[1], (($Matches[2] + $Matches[3]).Trim())
        $entry = "R:" + $c
        if (-not $seen.ContainsKey($c)) { $seen[$c] = 1; $replies.Add($c) }
      }
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^修正\s*#?(\d+)\s*[:：]?\s*(\S.*)$') {
        # redraft request for a writer draft (notify.parse_redraft): keep the instruction text
        $entry = 'R:修正 {0} {1}' -f $Matches[1], $Matches[2].Trim()
      }
      elseif ($l.Normalize([Text.NormalizationForm]::FormKC) -match '^下書き\s*#?(\d+)\s*[:：]?\s*(\S.*)$') {
        # the PM pastes back what Microsoft 365 Copilot wrote, as one line (notify.parse_paste)
        $entry = 'R:下書き {0} {1}' -f $Matches[1], $Matches[2].Trim()
      }
      if ($entry -and ($timeline.Count -eq 0 -or $timeline[$timeline.Count - 1] -ne $entry)) { $timeline.Add($entry) }
    }
    $c = $walker.GetFirstChild($el)
    while ($c) { Walk $c ($d + 1); $c = $walker.GetNextSibling($c) }
  }
  Walk $w 0
  Out-Json @{ ok = $true; posts = @($posts); replies = @($replies); timeline = @($timeline) }; exit 0
}

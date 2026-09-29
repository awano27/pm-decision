<#
.SYNOPSIS
  Ask Microsoft 365 Copilot inside Teams (the "Copilot" chat) and read its answer, via built-in
  Windows UI Automation. No downloads, no admin rights; the prompt stays in your M365 tenant.

  probe                  -> JSON with structure hints only (no chat text): is there a Copilot entry
                            in the chat list, what the window title looks like once it is open,
                            whether a compose box and a Send/Stop button are found
  ask -PromptFile <f>    -> open the Copilot chat, paste the prompt (UTF-8 file), send it, wait until
                            the answer stops growing, print JSON {ok, text}

  Safety: text is pasted and sent only while the window title says Copilot; the compose box must hold
  exactly the prompt before sending (the same exact-text check as the self chat).
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('probe', 'ask')][string]$Action,
  [string]$PromptFile = '',
  [int]$TimeoutSec = 150
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
Add-Type -Namespace KC -Name W -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll")] public static extern void mouse_event(int f, int x, int y, int d, int e);
'@
$COPILOT = '^(Microsoft 365 )?Copilot(\s|$|,|（|\()'

function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 5 }
function Fail($msg) { Out-Json @{ ok = $false; error = $msg }; exit 2 }
function Get-TeamsWindow {
  $pids = @(Get-Process -Name ms-teams -ErrorAction SilentlyContinue | ForEach-Object Id)
  if (-not $pids) { return $null }
  $A::RootElement.FindAll('Children', [System.Windows.Automation.Condition]::TrueCondition) |
    Where-Object { $pids -contains $_.Current.ProcessId -and $_.Current.Name } | Select-Object -First 1
}
function Find-All($root, $type) {
  $root.FindAll('Descendants', (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $type)))
}
function Assert-Foreground($w) {
  $h = [IntPtr]$w.Current.NativeWindowHandle
  if ([KC.W]::GetForegroundWindow() -ne $h) { [void][KC.W]::ShowWindow($h, 9); [void][KC.W]::SetForegroundWindow($h); Start-Sleep -Milliseconds 400 }
  if ([KC.W]::GetForegroundWindow() -ne $h) { Fail 'Teams is not the foreground window; nothing sent' }
}
function Click($el) {
  $r = $el.Current.BoundingRectangle
  if ($r.Width -le 0) { return $false }
  [void][KC.W]::SetCursorPos([int]($r.X + [math]::Min(60, $r.Width / 2)), [int]($r.Y + $r.Height / 2))
  [KC.W]::mouse_event(2, 0, 0, 0, 0); [KC.W]::mouse_event(4, 0, 0, 0, 0); $true
}
function Test-CopilotOpen($w) { $w -and ($w.Current.Name -match '(^|\| )(Microsoft 365 )?Copilot( \||$)') }
function Get-Entries($w) {
  # chat-list entries (tree or list layouts) and app-bar buttons whose name starts with Copilot
  @(Find-All $w $CT::TreeItem) + @(Find-All $w $CT::ListItem) + @(Find-All $w $CT::Button) + @(Find-All $w $CT::TabItem) |
    Where-Object { $_.Current.Name -match $COPILOT }
}
function Find-Box($w) {
  # 1) the usual compose box id or a name that says so; 2) else the lowest editable field in the window
  #    (the compose box sits at the bottom of the pane); 3) else the lowest focusable document
  $edits = @(Find-All $w $CT::Edit)
  if ($Action -eq 'ask') {
    # writes: exactly ONE compose-like box may exist in the window. Two (a side chat panel, a meeting chat next to
    # Copilot) or a guess by position could put the request into somebody else's chat, so no guessing here
    $c = @($edits | Where-Object { $_.Current.BoundingRectangle.Width -gt 0 -and ($_.Current.AutomationId -like 'new-message-*' -or $_.Current.AutomationId -like '*-chat-editor-target-element' -or $_.Current.Name -match 'Copilot|メッセージ|message|質問|Ask') })
    $script:BoxCandidates = $c.Count
    $mk = { param($x) ([string]$x) -replace '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){0,4}', '<id>' -replace '\d+', 'N' }
    $script:EditShapes = (@($edits | Where-Object { $_.Current.BoundingRectangle.Width -gt 0 } | Select-Object -First 6 | ForEach-Object { "$(& $mk $_.Current.AutomationId):len$($_.Current.Name.Length)" }) -join ';')
    if ($c.Count -ne 1) { return $null }
    return $c[0]
  }
  $b = $edits | Where-Object { $_.Current.AutomationId -like 'new-message-*' -or $_.Current.Name -match 'Copilot|メッセージ|message|質問|Ask' } | Select-Object -First 1
  if (-not $b -and $edits.Count) { $b = $edits | Where-Object { $_.Current.BoundingRectangle.Width -gt 0 } | Sort-Object { $_.Current.BoundingRectangle.Y } | Select-Object -Last 1 }
  if (-not $b) { $b = @(Find-All $w $CT::Document) | Where-Object { $_.Current.IsKeyboardFocusable -and $_.Current.BoundingRectangle.Width -gt 0 } | Sort-Object { $_.Current.BoundingRectangle.Y } | Select-Object -Last 1 }
  $b
}
function Get-Box($w) {
  $b = Find-Box $w
  if (-not $b) { Fail "compose box not found, or not the only one (candidates: $script:BoxCandidates; visible edits: $script:EditShapes); nothing pasted. Close side chat panels so only the Copilot chat is open" }
  $b
}
function Shape($s) {
  # structure only: known UI words stay, every other name becomes its length
  $s = [string]$s
  if ($s -match '^(送信|Send|停止|Stop|生成を停止|添付|Attach|新しいチャット|New chat|質問|Ask|メッセージ|Message)') { $s.Substring(0, [math]::Min(14, $s.Length)) } else { "<$($s.Length)>" }
}
function Get-BoxText($b) {
  try { return $b.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(8000) }
  catch { try { return $b.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { return '' } }
}
function Squash($s) {
  # blanks and invisible format characters, incl. U+FFFC (object marker) that an empty web editor can hold
  ([string]$s) -replace '[\s ­​-‏⁠﻿\uFFFC]', ''
}
function Get-PageText($w) {
  # the whole web page as one text (the chat is a web view): the longest Document text in the window
  $best = ''
  foreach ($d in (Find-All $w $CT::Document)) {
    try { $t = [string]$d.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(300000) } catch { continue }
    if ($t.Length -gt $best.Length) { $best = $t }
  }
  $best
}
function Test-Busy($w) {
  # while Copilot is writing there is a Stop button
  [bool](Find-All $w $CT::Button | Where-Object { $_.Current.Name -match '^(停止|Stop|生成を停止|Stop generating|応答を停止)' -and $_.Current.IsEnabled } | Select-Object -First 1)
}
function Get-Texts($w) {
  # every named Text / Group / Document node under the window, in screen order, de-duplicated
  $seen = @{}; $out = New-Object System.Collections.Generic.List[string]
  foreach ($t in @($CT::Text, $CT::Group, $CT::Document, $CT::ListItem)) {
    foreach ($e in (Find-All $w $t)) {
      $n = [string]$e.Current.Name
      if ($n.Length -ge 2 -and -not $seen.ContainsKey($n)) { $seen[$n] = 1; $out.Add($n) }
    }
  }
  $out
}

function Open-Copilot($w) {
  if (Test-CopilotOpen $w) { return $w }
  foreach ($e in (Get-Entries $w)) {
    foreach ($how in 'select', 'invoke', 'click') {
      try {
        switch ($how) {
          'select' { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
          'invoke' { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
          'click' { Assert-Foreground $w; if (-not (Click $e)) { throw 'no rectangle' } }
        }
      } catch { continue }
      for ($i = 0; $i -lt 12; $i++) { Start-Sleep -Milliseconds 250; $w = Get-TeamsWindow; if (Test-CopilotOpen $w) { return $w } }
    }
  }
  $null
}

$w = Get-TeamsWindow
if (-not $w) { Fail 'Teams window not found' }

if ($Action -eq 'probe') {
  $entries = @(Get-Entries $w)
  $kinds = @($entries | ForEach-Object { $_.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '' }) -join ','
  $opened = Open-Copilot $w
  $shape = if ($opened) { (($opened.Current.Name -split ' \| ') | ForEach-Object { if ($_ -match '^(Microsoft 365 )?Copilot$|^Microsoft Teams$|^チャット$|^Chat$') { $_ } else { '<text>' } }) -join ' | ' } else { '' }
  $box = $null
  for ($i = 0; $opened -and -not $box -and $i -lt 8; $i++) { $box = Find-Box $opened; if (-not $box) { Start-Sleep -Seconds 1; $opened = Get-TeamsWindow } }
  $send = if ($opened) { [bool](Find-All $opened $CT::Button | Where-Object { $_.Current.Name -match '^(送信|Send)' } | Select-Object -First 1) } else { $false }
  $mask = { param($s) ([string]$s) -replace '[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){0,4}', '<id>' -replace '\d+', 'N' }
  $edits = if ($opened) { @(Find-All $opened $CT::Edit | Select-Object -First 8 | ForEach-Object { "Edit:$(& $mask $_.Current.AutomationId):len$($_.Current.Name.Length):focus=$($_.Current.IsKeyboardFocusable)" }) } else { @() }
  $docs = if ($opened) { @(Find-All $opened $CT::Document | Select-Object -First 5 | ForEach-Object { "Doc:$(& $mask $_.Current.AutomationId):focus=$($_.Current.IsKeyboardFocusable)" }) } else { @() }
  $btns = if ($opened) { @(Find-All $opened $CT::Button | Select-Object -First 30 | ForEach-Object { Shape $_.Current.Name }) } else { @() }
  Out-Json ([ordered]@{ ok = $true; entries = $entries.Count; entryTypes = $kinds; opened = [bool]$opened; title = $shape
                        composeBox = [bool]$box; boxId = $(if ($box) { (& $mask $box.Current.AutomationId) } else { '' })
                        sendButton = $send; edits = $edits; docs = $docs; buttons = $btns
                        boxTextLen = $(if ($box) { (Get-BoxText $box).Trim().Length } else { -1 }); boxNameLen = $(if ($box) { ([string]$box.Current.Name).Length } else { -1 }) })
  exit 0
}

# ---- ask ----
if (-not (Test-Path $PromptFile)) { Fail 'prompt file not found' }
$prompt = (Get-Content -Raw -Encoding UTF8 $PromptFile).Trim()
$w = Open-Copilot $w
if (-not $w) { Fail 'Copilot chat not found in Teams (no chat-list entry or app button named Copilot)' }
$box = Get-Box $w
# an empty web editor still reads as one invisible character (zero-width space, line break): Squash drops them
$cur = Get-BoxText $box
$sq = Squash $cur
$phq = Squash ([string]$box.Current.Name)
if ($sq.Length -gt 2 -and $sq -ne $phq) {
  # (two characters or fewer is an editor artifact, never a draft)
  # text left by our own earlier run may be cleared; anything else could be the person's draft: leave it
  if ($sq.StartsWith('あなたはプロジェクトマネージャーの下書き係')) {   # Squash: an invisible first character (U+FFFC, zero-width) defeats a plain Trim()
    Assert-Foreground $w; $box.SetFocus(); Start-Sleep -Milliseconds 150; Assert-Foreground $w
    [System.Windows.Forms.SendKeys]::SendWait('^a'); Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait('{DEL}'); Start-Sleep -Milliseconds 300
    $sq = Squash (Get-BoxText (Get-Box $w))
  }
  if ($sq.Length -gt 2 -and $sq -ne $phq) {
    # lengths only, never the text
    Fail ("the Copilot compose box holds $($sq.Length) visible characters (placeholder $($phq.Length)) that are not kimeru's; nothing sent")
  }
}
$before = @(Get-Texts $w)
$saved = $null
try { $saved = [System.Windows.Forms.Clipboard]::GetText() } catch {}
try {
  [System.Windows.Forms.Clipboard]::SetText($prompt)
  Assert-Foreground $w
  $box = Get-Box $w; $box.SetFocus(); Start-Sleep -Milliseconds 200; [void](Click $box); Start-Sleep -Milliseconds 200
  Assert-Foreground $w
  [System.Windows.Forms.SendKeys]::SendWait('^v'); Start-Sleep -Milliseconds 500
} finally {
  if ($saved) { [System.Windows.Forms.Clipboard]::SetText($saved) } else { [System.Windows.Forms.Clipboard]::Clear() }
}
$box = Get-Box $w
if ((Squash (Get-BoxText $box)) -ne (Squash $prompt)) {
  Assert-Foreground $w; $box.SetFocus(); [System.Windows.Forms.SendKeys]::SendWait('^a'); [System.Windows.Forms.SendKeys]::SendWait('{DEL}')
  Fail 'the Copilot compose box did not hold exactly the prompt; removed it, nothing sent'
}
if (-not (Test-CopilotOpen (Get-TeamsWindow))) { Fail 'window changed before send; aborted' }
$btn = Find-All $w $CT::Button | Where-Object { $_.Current.Name -match '^(送信|Send)(\s*\(|$)' -and $_.Current.IsEnabled } | Select-Object -First 1
if ($btn) { try { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() } catch { $btn = $null } }
if (-not $btn) { Assert-Foreground $w; $box.SetFocus(); [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
# the request must actually leave the box; if it is still there after a moment, press Enter once more, and
# if it still is, remove our own text (a stuck prompt would block every later run) and stop
$sent = $false
foreach ($try in 1..3) {
  Start-Sleep -Milliseconds 1500
  $left = Squash (Get-BoxText (Get-Box $w))
  if ($left.Length -le 2 -or $left -eq $phq -or -not $prompt.StartsWith($left.Substring(0, [Math]::Min(20, $left.Length)))) { $sent = $true; break }
  if ($try -lt 3) { Assert-Foreground $w; (Get-Box $w).SetFocus(); [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
}
if (-not $sent) {
  Assert-Foreground $w; $bx = Get-Box $w; $bx.SetFocus(); [System.Windows.Forms.SendKeys]::SendWait('^a'); [System.Windows.Forms.SendKeys]::SendWait('{DEL}')
  Fail 'the request stayed in the Copilot compose box (send button / Enter did not send it); removed it'
}

# wait for the answer. It is JSON carrying our keys ("a1"... / "memo"). Candidates: every new text on the
# page (nodes) and the page text, each cut after the last line of our own request. Only a text that carries
# our keys counts, so UI text (composer placeholder, profile card, object markers) is never taken for it.
$deadline = (Get-Date).AddSeconds($TimeoutSec)
$last = ''; $stable = 0; $from = ''
$known = @{}; foreach ($t in $before) { $known[$t] = 1 }
$p = Squash $prompt
$marker = '（JSON だけ）'
$keyRx = '"(a\d+|memo)"\s*:'
$dbg = ($env:KIMERU_DEBUG_WRITER -eq '1')
function After-Marker($s) { $k = ([string]$s).LastIndexOf($marker); if ($k -ge 0) { ([string]$s).Substring($k + $marker.Length).Trim() } else { [string]$s } }
$page = ''
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
  $w = Get-TeamsWindow
  $nodes = @(Get-Texts $w | Where-Object { -not $known.ContainsKey($_) -and (Squash $_) -ne $p -and -not $p.Contains((Squash $_)) })
  $page = Get-PageText $w
  $pool = @($nodes | ForEach-Object { [pscustomobject]@{ from = 'node'; text = (After-Marker $_) } }) + @([pscustomobject]@{ from = 'page'; text = (After-Marker $page) })
  $hit = $pool | Where-Object { $_.text -match $keyRx } | Sort-Object { $_.text.Length } -Descending | Select-Object -First 1
  $cand = if ($hit) { [string]$hit.text } else { '' }
  if ($cand -and $cand -eq $last -and -not (Test-Busy $w)) { $stable++ } else { $stable = 0 }
  $last = $cand
  if ($hit) { $from = $hit.from }
  if ($stable -ge 2) { Out-Json @{ ok = $true; text = $last; from = $from; pageLen = $page.Length; nodeLen = $last.Length }; exit 0 }
}
$dump = @()
if ($dbg) {
  # only for a fictional sample (company-check T17 sets the flag): what is on the page, longest first
  $all = $w.FindAll('Descendants', [System.Windows.Automation.Condition]::TrueCondition)
  $rows = foreach ($e in $all) {
    $n = [string]$e.Current.Name
    if ($n.Length -ge 25 -and -not $known.ContainsKey($n)) { [pscustomobject]@{ t = ($e.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''); n = $n } }
  }
  $dump = @($rows | Sort-Object { $_.n.Length } -Descending | Select-Object -First 12 | ForEach-Object { "$($_.t):len$($_.n.Length):" + (($_.n.Substring(0, [math]::Min(110, $_.n.Length))) -replace '\s+', ' ') })
  $tail = ((After-Marker $page) -replace '[\uFFFC\s]+', ' ').Trim()
  $dump += "pageTailVisible:len$($tail.Length):" + $tail.Substring(0, [math]::Min(150, $tail.Length))
}
# lengths and flags only, never text: how much new text appeared, whether Copilot still looked busy, whether a JSON-like brace was seen
$tl = ((After-Marker $page) -replace '[\uFFFC\s]+', ' ').Trim()
$shape = "newNodes=$($nodes.Count) tailLen=$($tl.Length) busy=$(Test-Busy $w) brace=$($tl.Contains('{')) keys=$([bool]($tl -match $keyRx))"
Out-Json @{ ok = $false; error = "no answer carrying our keys within $TimeoutSec s ($shape)"; debug = $dump }
exit 2

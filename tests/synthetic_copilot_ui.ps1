# Synthetic-UI harness for tools/teams-copilot.ps1 (driven by tests/test_copilot_script.py).
#
# It never touches Teams, UI Automation on the real desktop, the clipboard or the keyboard. The production functions are
# taken out of the script with the PowerShell parser (not re-implemented) and run against an in-memory tree of
# UIA-shaped objects. Only the OS edge is replaced by stubs: SendKeys, Clipboard, the mouse, the foreground window, the
# UIA FocusedElement, Start-Sleep, Fail (throws instead of exit) and Get-TeamsWindow.
param(
  [Parameter(Mandatory = $true)][string]$ScriptPath,
  [Parameter(Mandatory = $true)][string]$ScenarioFile
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$Action = 'ask'

# ---------------------------------------------------------------- production code, taken out by the parser
$tokens = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$perr)
if ($perr.Count) { throw "production script does not parse: $($perr[0].Message)" }
$defs = @{}
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) { $defs[$f.Name] = $f.Extent.Text }
foreach ($need in 'Find-All', 'Click', 'Test-CopilotOpen', 'Test-CopilotPane', 'Get-Entries', 'Test-FocusOn', 'Focus-Box', 'Read-Box',
                  'Find-Box', 'Get-Box', 'Get-BoxText', 'Squash', 'Get-Texts', 'Open-Copilot', 'Show-CopilotPane', 'Send-ToCopilot') {
  if (-not $defs.ContainsKey($need)) { throw "production function $need not found; update the synthetic adapter" }
}
foreach ($st in $ast.EndBlock.Statements) {
  if ($st -is [System.Management.Automation.Language.AssignmentStatementAst] -and $st.Left.Extent.Text -in @('$COPILOT', '$BOXRX')) { Invoke-Expression $st.Extent.Text }
}
if (-not $COPILOT -or -not $BOXRX) { throw 'identity patterns not found' }
# the ask-path statements that run before Send-ToCopilot (box detection, open/show the Copilot pane, stale-text check)
$stm = @($ast.EndBlock.Statements)
$i0 = -1; $i1 = -1
for ($i = 0; $i -lt $stm.Count; $i++) {
  if ($i0 -lt 0 -and $stm[$i].Extent.Text -eq '[void](Find-Box $w -Strict)') { $i0 = $i }
  if ($stm[$i].Extent.Text -like '$before = @(Get-Texts $w)*') { $i1 = $i }
}
if ($i0 -lt 0 -or $i1 -le $i0) { throw 'ask pre-flight statements not found; update the synthetic adapter' }
$PreFlight = ($stm[$i0..$i1] | ForEach-Object { $_.Extent.Text }) -join "`n"

# the OS edge: each fragment must still exist in the production text, else the adapter is out of date
$edge = @(
  @('[System.Windows.Forms.SendKeys]::SendWait(', 'Sim-SendWait('),
  @('[System.Windows.Forms.Clipboard]::GetText()', '(Sim-ClipGet)'),
  @('[System.Windows.Forms.Clipboard]::SetText(', 'Sim-ClipSet('),
  @('[System.Windows.Forms.Clipboard]::Clear()', 'Sim-ClipClear'),
  @('$A::FocusedElement', '(Sim-Focused)'),
  @('[void][KC.W]::SetCursorPos($cx, $cy)', '[void](Sim-Cursor $cx $cy)'),
  @('[KC.W]::mouse_event(2, 0, 0, 0, 0); [KC.W]::mouse_event(4, 0, 0, 0, 0)', 'Sim-MouseClick')
)
$seen = @{}
foreach ($name in @($defs.Keys)) {
  $t = $defs[$name]
  foreach ($e in $edge) { if ($t.Contains($e[0])) { $seen[$e[0]] = 1; $t = $t.Replace($e[0], $e[1]) } }
  $defs[$name] = $t
}
foreach ($e in $edge) { if ($PreFlight.Contains($e[0])) { $PreFlight = $PreFlight.Replace($e[0], $e[1]) } }
foreach ($e in $edge) { if (-not $seen.ContainsKey($e[0])) { throw "edge fragment not found in production: $($e[0])" } }
foreach ($name in @($defs.Keys)) { Invoke-Expression $defs[$name] }
$UsesSendButtonHelper = $defs.ContainsKey('Get-SendButton')

# ---------------------------------------------------------------- stubs (defined after the production code: they win)
function Start-Sleep { }
function Fail($msg) { throw "FAIL: $msg" }
function Write-DiagFile($why) { }
function Out-Json($o) { }
function Get-TeamsWindow { $script:Win }
function Assert-Foreground($w) { if ($script:ForegroundLost) { Fail 'Teams is not the foreground window; nothing sent' } }
function Sim-Focused { if (-not $script:Focus -or $script:Focus.Gone) { throw 'no focused element' }; $script:Focus }
function Sim-ClipGet { $script:Clip }
function Sim-ClipSet($t) { $script:Clip = [string]$t }
function Sim-ClipClear { $script:Clip = '' }
function Sim-Cursor($x, $y) { $script:Cursor = @($x, $y) }
function Sim-MouseClick {
  # a click focuses the smallest editable element under the pointer, as a real click on a composer does
  if ($script:FocusLocked) { return }
  $best = $null
  foreach ($e in (Get-Desc $script:Win)) {
    if (-not $e.Editable) { continue }
    if ($e.CurData.BoundingRectangle.Contains([double]$script:Cursor[0], [double]$script:Cursor[1])) {
      if (-not $best -or ($e.CurData.BoundingRectangle.Width * $e.CurData.BoundingRectangle.Height) -lt ($best.CurData.BoundingRectangle.Width * $best.CurData.BoundingRectangle.Height)) { $best = $e }
    }
  }
  if ($best) { $script:Focus = $best }
}
function Record-Sent($el) {
  if ($el.Gone -or -not $el.Text.Length) { return }
  [void]$script:Sent.Add("$($el.Tag)|$($el.Text)")
  $el.Text = ''
}
function Sim-SendWait($k) {
  $f = $script:Focus
  $tag = if ($f -and -not $f.Gone) { $f.Tag } else { '(none)' }
  [void]$script:Keys.Add("$k@$tag")
  if (-not $f -or $f.Gone) { return }
  switch ($k) {
    '^v' { if ($f.Editable) { $f.Text = $script:Clip; if ($script:PasteMangle) { $f.Text += 'X' }; if ($script:OnPaste) { & $script:OnPaste } } }
    '^a' { $f.AllSelected = $true }
    '{DEL}' { if ($f.AllSelected) { $f.Text = ''; $f.AllSelected = $false } }
    '{ENTER}' { if ($f.Editable) { Record-Sent $f } }
    '^{ENTER}' { if ($f.Editable) { Record-Sent $f } }
  }
}

# ---------------------------------------------------------------- synthetic UIA-shaped elements
function Get-Desc($el) { foreach ($k in $el.Kids) { if (-not $k.Gone) { $k; Get-Desc $k } } }
function New-El($tag, $type, $name, $x, $y, $w, $h) {
  $cur = [pscustomobject]@{ Name = [string]$name; ControlType = $type; AutomationId = ''; IsKeyboardFocusable = $true; IsEnabled = $true; ProcessId = 1
                            BoundingRectangle = (New-Object System.Windows.Rect([double]$x, [double]$y, [double]$w, [double]$h)) }
  $o = [pscustomobject]@{ Tag = $tag; CurData = $cur; Kids = (New-Object System.Collections.ArrayList); Parent = $null; Text = ''; Gone = $false
                          Rid = @(42, (++$script:NextRid)); Editable = $false; Selected = $false; Selectable = $false; Invokable = $false
                          Owner = $null; CanFocus = $true; AllSelected = $false }
  $o | Add-Member ScriptProperty Current { if ($this.Gone) { throw 'ElementNotAvailable' }; $this.CurData }
  $o | Add-Member ScriptMethod GetRuntimeId { if ($this.Gone) { throw 'ElementNotAvailable' }; $this.Rid }
  $o | Add-Member ScriptMethod SetFocus {
    if ($this.Gone -or -not $this.CanFocus) { throw 'cannot take focus' }
    if (-not $script:FocusLocked) { $script:Focus = $this }
  }
  $o | Add-Member ScriptMethod FindAll {
    param($scope, $cond)
    if ($this.Gone) { throw 'ElementNotAvailable' }
    if ($cond -is [System.Windows.Automation.PropertyCondition] -and $cond.Value -eq $script:ButtonType.Id -and $script:OnButtonSearch) {
      $h = $script:OnButtonSearch; $script:OnButtonSearch = $null; & $h
    }
    $all = @(Get-Desc $this)
    if ($cond -is [System.Windows.Automation.PropertyCondition]) { $all = @($all | Where-Object { $_.CurData.ControlType.Id -eq $cond.Value }) }
    $all
  }
  $o | Add-Member ScriptMethod GetCurrentPattern {
    param($p)
    if ($this.Gone) { throw 'ElementNotAvailable' }
    $n = $p.ProgrammaticName
    if ($n -like 'TextPattern*' -and $this.Editable) {
      $dr = [pscustomobject]@{ T = $this.Text }
      $dr | Add-Member ScriptMethod GetText { param($m) $this.T }
      return [pscustomobject]@{ DocumentRange = $dr }
    }
    if ($n -like 'ValuePattern*' -and $this.Editable) { return [pscustomobject]@{ Current = [pscustomobject]@{ Value = $this.Text } } }
    if ($n -like 'SelectionItemPattern*' -and $this.Selectable) {
      $sp = [pscustomobject]@{ Owner = $this; Current = [pscustomobject]@{ IsSelected = $this.Selected } }
      $sp | Add-Member ScriptMethod Select {
        if ($script:SelectBlocked) { return }   # the click on the row does not navigate
        foreach ($s in $this.Owner.Parent.Kids) { if ($s.Selectable) { $s.Selected = $false } }
        $this.Owner.Selected = $true
        if ($script:OnSelect) { & $script:OnSelect $this.Owner }
      }
      return $sp
    }
    if ($n -like 'InvokePattern*' -and $this.Invokable) {
      $ip = [pscustomobject]@{ Owner = $this }
      $ip | Add-Member ScriptMethod Invoke { $b = $this.Owner; if ($b.BoxOwner) { Record-Sent $b.BoxOwner } }
      return $ip
    }
    throw 'pattern not supported'
  }
  $o
}
$script:ButtonType = $CT::Button
function Add-Kid($p, $c) { $c.Parent = $p; [void]$p.Kids.Add($c); $c }
function Remove-El($el) {
  if ($el.Parent) { $el.Parent.Kids.Remove($el) }
  $el.Gone = $true
  if ($script:Focus -and [object]::ReferenceEquals($script:Focus, $el)) { $script:Focus = $null }
}
function Add-Box($parent, $tag, $name, $x, $y, $w, $h) { $b = New-El $tag $CT::Edit $name $x $y $w $h; $b.Editable = $true; $b.CurData.AutomationId = 'new-message-' + $tag; Add-Kid $parent $b }
function Add-Send($parent, $tag, $owner, $x, $y) {
  $s = New-El $tag $CT::Button '送信' $x $y 40 30
  $s.Invokable = $true; $s | Add-Member NoteProperty BoxOwner $owner
  Add-Kid $parent $s
}
function Add-ChatList($win, $selectedName) {
  foreach ($row in @(@('Project chat', 100), @('Copilot', 140), @('診療支援Daily comm', 180))) {
    $r = Add-Kid $win (New-El ('row:' + $row[0]) $CT::TreeItem $row[0] 10 $row[1] 300 36)
    $r.Selectable = $true; $r.Selected = ($row[0] -eq $selectedName)
  }
}
function Add-MeetingTabs($win) { foreach ($n in 'ファイル', 'まとめ', '出席') { [void](Add-Kid $win (New-El ('tab:' + $n) $CT::TabItem $n 400 60 80 30)) } }

# Layouts. Returns the elements the checks need. Box tags: COP-box (the target), MAIN-box / MEET-box (somebody else's chat).
function Build-Layout([string]$layout) {
  $L = @{ Boxes = [ordered]@{} }
  switch ($layout) {
    'full' {
      $win = New-El 'win' $CT::Window 'チャット | Microsoft 365 Copilot | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Copilot'
      [void](Add-Box $win 'SEARCH' 'Copilot で検索または質問する (Ctrl+E)' 400 10 700 30)
      $L.Cop = Add-Box $win 'COP-box' 'Copilot にメッセージを送信する' 350 800 1000 60
      $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1300 815
      $L.Focus = $null
    }
    'panel' {   # an ordinary chat in the main area, Copilot as a right-side panel; the other chat holds a draft
      $win = New-El 'win' $CT::Window 'チャット | Project chat | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Project chat'
      $L.Other = Add-Box $win 'MAIN-box' 'メッセージを入力' 350 800 650 60
      $L.Other.Text = '自分の下書き（送らない）'
      $L.OtherSend = Add-Send $win 'MAIN-send' $L.Other 1040 815   # the other chat's button sits right next to the panel, left of it
      $L.Cop = Add-Box $win 'COP-box' 'Message Copilot' 1100 800 450 60
      $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1500 815
      $L.Focus = $L.Other
    }
    { $_ -in 'meeting', 'meeting-nobutton' } {   # a meeting chat (tab bar, own compose box and Send) next to the Copilot panel
      $win = New-El 'win' $CT::Window 'チャット | 定例会議 | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Project chat'
      Add-MeetingTabs $win
      $L.Other = Add-Box $win 'MEET-box' 'メッセージを入力' 350 800 650 60
      $L.Other.Text = '会議チャットの下書き（送らない）'
      $L.OtherSend = Add-Send $win 'MEET-send' $L.Other 950 815       # first Send button in tree order
      $L.Cop = Add-Box $win 'COP-box' 'Copilot にメッセージを送信する' 1100 800 450 60
      if ($layout -eq 'meeting') { $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1500 815 }
      $L.Focus = $L.Other
    }
    'meeting-unnamed' {   # nothing says Copilot on either box, the title and the selected row do, a meeting tab bar is visible
      $win = New-El 'win' $CT::Window 'チャット | Microsoft 365 Copilot | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Copilot'
      Add-MeetingTabs $win
      $L.Other = Add-Box $win 'MEET-box' 'メッセージを入力' 350 800 650 60
      $L.Other.Text = '会議チャットの下書き（送らない）'
      $L.OtherSend = Add-Send $win 'MEET-send' $L.Other 950 815
      $L.Cop = Add-Box $win 'COP-box' 'メッセージを入力' 1100 800 450 60
      $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1500 815
      $L.Focus = $L.Other
    }
    { $_ -in 'unknown-other-chat', 'other-chat-navigable' } {   # the title still says Copilot, but the selected row is another chat and no box names Copilot
      $win = New-El 'win' $CT::Window 'チャット | Microsoft 365 Copilot | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Project chat'
      $L.Other = Add-Box $win 'MAIN-box' 'メッセージを入力' 350 800 1000 60
      $L.Other.Text = '自分の下書き（送らない）'
      $L.OtherSend = Add-Send $win 'MAIN-send' $L.Other 1300 815
      $L.Focus = $L.Other
      if ($layout -eq 'unknown-other-chat') { $L.Cop = $L.Other; $script:SelectBlocked = $true }   # selecting the Copilot row does not change the pane
      else {
        # selecting the Copilot row really opens it: the other chat's composer is replaced by Copilot's own
        $L.Cop = New-El 'COP-box' $CT::Edit 'Copilot にメッセージを送信する' 350 800 1000 60
        $L.Cop.Editable = $true; $L.Cop.Parent = $win
        $L.CopSend = New-El 'COP-send' $CT::Button '送信' 1300 815 40 30
        $L.CopSend.Invokable = $true; $L.CopSend | Add-Member NoteProperty BoxOwner $L.Cop; $L.CopSend.Parent = $win
        $script:OnSelect = { param($row) if ($row.CurData.Name -eq 'Copilot') { Remove-El $script:L.Other; Remove-El $script:L.OtherSend; $script:L.Cop.Gone = $false; $script:L.CopSend.Gone = $false; [void]$script:L.Win.Kids.Add($script:L.Cop); [void]$script:L.Win.Kids.Add($script:L.CopSend) } }
      }
    }
    'ambiguous-two-named' {   # Copilot on the full page AND as a side panel: two boxes that both name Copilot
      $win = New-El 'win' $CT::Window 'チャット | Microsoft 365 Copilot | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Copilot'
      $L.Cop = Add-Box $win 'COP-box' 'Copilot にメッセージを送信する' 350 800 700 60
      $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1000 815
      $L.Cop2 = Add-Box $win 'COP2-box' 'Message Copilot' 1100 800 450 60
      $L.Focus = $null
    }
    { $_ -in 'unnamed-single', 'unnamed-two-editors' } {   # composers that do not name Copilot; the pane itself is proven (selected row, no tab bar)
      $win = New-El 'win' $CT::Window 'チャット | Microsoft 365 Copilot | Microsoft Teams' 0 0 1600 900
      Add-ChatList $win 'Copilot'
      $L.Cop = Add-Box $win 'COP-box' '' 400 800 900 60
      $L.CopSend = Add-Send $win 'COP-send' $L.Cop 1260 815
      if ($layout -eq 'unnamed-two-editors') { $L.Other = Add-Box $win 'MAIN-box' '' 400 700 900 60; $L.Other.Text = '' }
      $L.Focus = $null
    }
    default { throw "unknown layout $layout" }
  }
  $L.Win = $win
  $L.Boxes['COP-box'] = $L.Cop
  if ($L.Other) { $L.Boxes[$L.Other.Tag] = $L.Other }
  $L
}

# ---------------------------------------------------------------- one scenario
function Run-Scenario($s) {
  $script:NextRid = 0; $script:Keys = New-Object System.Collections.ArrayList; $script:Sent = New-Object System.Collections.ArrayList
  $script:Clip = 'CLIP-BEFORE'; $script:Focus = $null; $script:FocusLocked = $false; $script:PasteMangle = $false
  $script:OnPaste = $null; $script:OnButtonSearch = $null; $script:OnSelect = $null; $script:ForegroundLost = $false
  $script:SelectBlocked = $false
  $script:BoxNamed = $false; $script:BoxEl = $null; $script:BoxId = ''; $script:BoxRect = $null; $script:BoxHow = ''; $script:BoxCandidates = 0
  $L = Build-Layout $s.layout
  $script:L = $L
  $script:Win = $L.Win
  $script:Focus = $L.Focus
  $prompt = [string]$s.prompt
  $mut = [string]$s.mutation
  # plain script blocks only (a closure would run in its own module scope and not see the sim state)
  $steal = { if ($script:L.Other) { $script:Focus = $script:L.Other } }
  $stealLocked = { if ($script:L.Other) { $script:Focus = $script:L.Other; $script:FocusLocked = $true } }
  $vanish = { Remove-El $script:L.Cop; if ($script:L.CopSend) { Remove-El $script:L.CopSend } }
  $between = $null    # runs after the pre-flight, before Send-ToCopilot looks at the screen again
  switch ($mut) {
    'none' { }
    'target-gone-before-paste' { $between = $vanish }
    'second-copilot-box-appears' { $between = { [void](Add-Box $script:L.Win 'COP2-box' 'Message Copilot' 1100 700 450 60) } }
    'paste-mangled' { $script:PasteMangle = $true }
    'focus-stolen-after-paste' { $script:OnPaste = $steal }
    'focus-stolen-after-paste-locked' { $script:OnPaste = $stealLocked }
    'focus-stolen-before-paste' { $script:FocusLocked = $true; $script:Focus = $L.Other }
    'target-gone-after-paste' { $script:OnPaste = $vanish }
    'late-focus-stolen' { $script:OnButtonSearch = $steal }
    'late-focus-stolen-locked' { $script:OnButtonSearch = $stealLocked }
    'late-target-gone' { $script:OnButtonSearch = $vanish }
    'late-second-copilot' { $script:OnButtonSearch = { [void](Add-Box $script:L.Win 'COP2-box' 'Message Copilot' 1100 700 450 60) } }
    'late-text-changed' { $script:OnButtonSearch = { $script:L.Cop.Text = 'User changed this draft' } }
    'draft-in-box' { $L.Cop.Text = 'Private draft that is not kimeru text' }
    'stale-kimeru-text' { $L.Cop.Text = 'あなたはプロジェクトマネージャーの下書き係です。古い依頼文。' }
    default { throw "unknown mutation $mut" }
  }
  $failed = $false; $err = ''
  try {
    $w = Get-TeamsWindow
    Invoke-Expression $PreFlight
    if ($between) { & $between }
    Send-ToCopilot $prompt
  } catch { $failed = $true; $err = [string]$_.Exception.Message }
  $texts = [ordered]@{}
  foreach ($k in $L.Boxes.Keys) { $texts[$k] = [string]$L.Boxes[$k].Text }
  [ordered]@{ id = $s.id; failed = $failed; error = ($err -replace '\s+', ' '); keys = @($script:Keys); sent = @($script:Sent); texts = $texts
              boxNamed = [bool]$script:BoxNamed; sendButtonHelper = $UsesSendButtonHelper }
}

$scenarios = Get-Content -Raw -Encoding UTF8 $ScenarioFile | ConvertFrom-Json
foreach ($s in @($scenarios)) {
  $r = try { Run-Scenario $s } catch { [ordered]@{ id = $s.id; harnessError = [string]$_.Exception.Message } }
  [Console]::Out.WriteLine(($r | ConvertTo-Json -Compress -Depth 6))
}

<#
.SYNOPSIS
  Structure-only probe of the Teams window via built-in Windows UI Automation.
  No downloads, no admin rights. Output contains NO message text: only control types,
  counts, name lengths, and a whitelist of UI chrome labels (buttons/tabs).

.USAGE
  1. Open Teams and show the screen to test (e.g. a meeting's Recap tab, or a chat).
  2. powershell -NoProfile -ExecutionPolicy Bypass -File .\probe-teams.ps1 -Label recap
  3. Send back the generated probe-<label>.txt (review it first; it has no message text).
#>
param(
  [string]$Label = "probe",
  [string]$Process = "ms-teams",
  [int]$MaxDepth = 25
)

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

$pids = @(Get-Process -Name $Process -ErrorAction SilentlyContinue | ForEach-Object Id)
$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$wins = $desktop.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
$root = $wins | Where-Object { $pids -contains $_.Current.ProcessId -and $_.Current.Name } | Select-Object -First 1
if (-not $root) { Write-Error "No visible window for process '$Process'. Open Teams first."; exit 1 }
$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
$types = @{}; $named = @{}; $chrome = New-Object System.Collections.Generic.HashSet[string]
$lines = New-Object System.Collections.Generic.List[string]
$keywords = 'Copilot|要約|まとめ|Recap|Summary|アクション|Action item|決定|Decision|メモ|Notes|トランスクリプト|Transcript'
$sw = [Diagnostics.Stopwatch]::StartNew()
$count = 0

function Walk($el, $depth) {
  if ($depth -gt $MaxDepth -or $script:count -gt 20000) { return }
  $script:count++
  $t = $el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
  $n = [string]$el.Current.Name
  $types[$t] = 1 + [int]$types[$t]
  if ($n) { $named[$t] = 1 + [int]$named[$t] }
  # UI chrome labels are safe to show; content text is reduced to its length
  $isChrome = $t -in @('Button', 'TabItem', 'MenuItem', 'ToolBar', 'Hyperlink') -and $n.Length -le 40
  if ($isChrome -and $n -match $keywords) { [void]$chrome.Add("$t | $n") }
  if ($depth -le $MaxDepth -and $t -notin @('Pane')) {
    $shown = if ($isChrome) { $n } else { "len=$($n.Length)" }
    $lines.Add(("  " * [Math]::Min($depth, 30)) + "$t $shown")
  }
  $c = $walker.GetFirstChild($el)
  while ($c) { Walk $c ($depth + 1); $c = $walker.GetNextSibling($c) }
}

Walk $root 0
$sw.Stop()

$out = "probe-$Label.txt"
$report = @(
  "label=$Label process=$Process elements=$count ms=$($sw.ElapsedMilliseconds) title_len=$($root.Current.Name.Length)",
  ("types: " + (($types.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', ')),
  ("named: " + (($named.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', ')),
  "copilot/recap-like UI labels:",
  ($chrome | Sort-Object | ForEach-Object { "  $_" }),
  "--- tree (content names replaced by length) ---"
) + $lines
$report | Out-File -Encoding utf8 $out
Write-Host "wrote $out ($count elements, $($sw.ElapsedMilliseconds) ms)"

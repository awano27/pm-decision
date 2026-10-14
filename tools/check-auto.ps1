<#
.SYNOPSIS
  Every check that needs nobody and sends nothing, in one go (run via run-auto-check.cmd):
  1. tools\check-update.ps1  - the latest changes, in a temporary folder (U0-U9)
  2. tools\check.ps1 -Auto   - environment, tests, sample judgments, Teams read-only checks when Teams is already running,
                               the local Kev when it is already running, a Windows notification (T0 T1 T3 T6 T7 T8 T12 T13 T14 T16 T23)
  Every question is answered "no": nothing is sent, no chat is opened, nothing is downloaded, Teams and Kev are not started.
  The result (OK/NG and counts only, no message text) goes to the clipboard and kimeru-auto-check-result.txt, with the list
  of the checks that need a person (sending to your own chat, a reply from your phone, writing to ADO, opening a chat).
#>
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "kimeru 自動の確認（何も送らない・チャットを開かない・質問しない）" -ForegroundColor Green
$sw = [Diagnostics.Stopwatch]::StartNew()

Write-Host ""
Write-Host "== 1/2 更新の確認（一時フォルダ）" -ForegroundColor Cyan
'' | & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'check-update.ps1') | Out-Host
$update = Join-Path $root 'kimeru-update-check-result.txt'

Write-Host ""
Write-Host "== 2/2 環境・テスト・読み取りだけの確認" -ForegroundColor Cyan
'' | & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'check.ps1') -Auto auto | Out-Host
$check = Join-Path $root 'kimeru-check-result.txt'

$manual = @(
  '手で確かめるもの（送信・スマホでの返信・画面の目視・組織のサービスへの書き込みがあるもの）:',
  '  .\run-check.cmd T5 T9 T20   自分とのチャットへの投稿と、スマホからの返信の読み取り（宛先は自分だけ）',
  '  .\run-check.cmd T21         通知の経路へ試験の 1 行（件数も本文もなし。経路を設定している場合）',
  '  .\run-check.cmd T22         試験用の作業項目に ADO のコメントを 1 件（az のサインインが要る）',
  '  .\run-check.cmd T24         既読のチャットを 1 件開いて読む（開いたチャットは既読になる）',
  '  .\run-check.cmd T10 T11     ADO・アラートの取り込み / Jev（架空のサンプル 1 件）',
  '  .\run-check.cmd T15 T17 T18 T19   文面の下書き（Copilot）'
)
$sheet = @("kimeru 自動の確認 $(Get-Date -Format 'yyyy-MM-dd HH:mm')（$([math]::Round($sw.Elapsed.TotalMinutes, 1)) 分）", '')
foreach ($f in @($update, $check)) {
  if (Test-Path $f) { $sheet += Get-Content $f -Encoding UTF8; $sheet += '' }
  else { $sheet += "（$(Split-Path -Leaf $f) ができていません）"; $sheet += '' }
}
# files an older version left behind (the update zip is extracted over the old folder, which keeps deleted files)
$listed = Join-Path $root 'FILES'
if (Test-Path $listed) {
  $known = @{}
  Get-Content $listed -Encoding UTF8 | ForEach-Object { $known[$_.Trim()] = $true }
  $stale = @(foreach ($d in 'docs', 'tests', 'kimeru', 'tools', 'graphs', 'playbooks', 'eval', 'examples', '.github') {
    $p = Join-Path $root $d
    if (Test-Path $p) {
      Get-ChildItem -Path $p -Recurse -File | Where-Object { $_.FullName -notlike '*\__pycache__\*' } | ForEach-Object {
        $r = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
        if (-not $known.ContainsKey($r)) { $r }
      }
    }
  })
  if ($stale.Count) {
    $line = "stale  NG 前の版のファイルが $($stale.Count) 件残っています（テストが落ちる原因になります。消してから zip を展開し直してください）: " + ($stale -join ', ')
  } else { $line = 'stale  OK 前の版のファイルは残っていません' }
} else { $line = 'stale  SKIP FILES がありません（更新 zip 以外から入れた場合）' }
$sheet = @($sheet[0], $line, '') + $sheet[2..($sheet.Count - 1)]
$ng = @($sheet | Where-Object { $_ -match '^\S+\s+NG' }).Count
$sheet[0] += "  NG=$ng"
$sheet += $manual
$out = Join-Path $root 'kimeru-auto-check-result.txt'
$sheet | Out-File -Encoding utf8 $out
try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = "$out を開いてコピーしてください" }
Write-Host ""
Write-Host "== 結果（$clip。共有する前に目を通してください）" -ForegroundColor Cyan
$sheet | ForEach-Object { Write-Host $_ }

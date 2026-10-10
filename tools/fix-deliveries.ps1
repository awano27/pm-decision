<#
.SYNOPSIS
  One-click fix for deliveries left as "unknown" (run via run-fix-deliveries.cmd). Asks nothing and posts nothing.
  Runs `python -m kimeru delivery reconcile` on the data folder (%LOCALAPPDATA%\kimeru, or KIMERU_STATE_DIR): the self chat is
  read once (read only) and each unknown delivery is settled - seen in the chat: confirmed; not seen: sent again by the next
  cycle; not seen and older than 24 hours: closed without sending (a case stays pending and shows in the morning brief).
  The result (kind, number and decision only; no message text) goes to kimeru-fix-deliveries-result.txt next to the scripts, the
  clipboard and, when a Google Drive folder named kimeru-release exists, into it.
  Nothing is done while another kimeru run holds approvals.lock.
#>
param(
  [switch]$NoClipboard,   # tests: leave the clipboard alone
  [switch]$NoDrive,       # tests: do not copy into Google Drive
  [string]$ResultFile = ''
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$data = if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }
if (-not $ResultFile) { $ResultFile = Join-Path $root 'kimeru-fix-deliveries-result.txt' }
Write-Host 'kimeru 配信結果不明の整理（質問しない・何も送らない）' -ForegroundColor Green

function Find-Python {
  $emb = Join-Path $root '.python\python.exe'
  if (Test-Path $emb) { return $emb }
  foreach ($c in 'python', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $v = cmd /c "`"$($cmd.Source)`" --version 2>&1"
    if ("$v" -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) { return $cmd.Source }
  }
  $null
}
$py = Find-Python
if (-not $py) { Write-Host 'Python 3.10 以上が見つかりません（run-check.cmd を一度実行すると取得できます）' -ForegroundColor Yellow; exit 1 }
if (-not (Test-Path $data)) { Write-Host 'データフォルダがありません（自動運転をまだ一度も動かしていない可能性があります）' -ForegroundColor Yellow; exit 1 }

$a = @('-m', 'kimeru', '--out', "`"$($data.TrimEnd([char]92))`"", 'delivery', 'reconcile', '--write', "`"$ResultFile`"")
if (-not $NoDrive) { $a += '--drive' }
Write-Host '自分とのチャットを 1 回読みます（Teams が自分とのチャットに切り替わることがあります。何も入力・送信しません）'
# python's stderr goes to a file: under Windows PowerShell 5.1 a native program's stderr would otherwise become an error record
$err = [IO.Path]::GetTempFileName(); $so = [IO.Path]::GetTempFileName()
$env:PYTHONIOENCODING = 'utf-8'
$p = Start-Process -FilePath $py -ArgumentList ($a -join ' ') -NoNewWindow -Wait -PassThru -RedirectStandardOutput $so -RedirectStandardError $err
$shown = @(Get-Content -Encoding UTF8 $so)
$problem = @(Get-Content -Encoding UTF8 $err | Select-Object -Last 3)
Remove-Item $so, $err -Force -ErrorAction SilentlyContinue
$shown | ForEach-Object { Write-Host $_ }
if (-not (Test-Path $ResultFile)) {
  Write-Host "整理の結果を作れませんでした（終了コード $($p.ExitCode)）" -ForegroundColor Yellow
  $problem | ForEach-Object { Write-Host $_ }
  exit 1
}
if ($NoClipboard) { $clip = 'クリップボードは使っていません' }
else { try { (@(Get-Content -Encoding UTF8 $ResultFile)) -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'クリップボードにコピーできませんでした' } }
Write-Host ''
Write-Host "結果ファイル: $ResultFile（$clip）" -ForegroundColor Cyan
exit 0

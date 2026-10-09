<#
.SYNOPSIS
  One-click diagnosis (run via run-diagnose.cmd). Asks nothing, posts nothing, changes no record.
  Runs `python -m kimeru diagnose` on the data folder (%LOCALAPPDATA%\kimeru, or KIMERU_STATE_DIR) and writes
  kimeru-diagnose-result.txt next to the scripts, copies it to the clipboard and, when a Google Drive folder named
  kimeru-release exists, into it. The sheet holds numbers, yes/no answers, graph node names, exception class names and
  kimeru's own case numbers: no message text, title, name or path.
  The self chat is read once (the same read as the approvals step: it may switch Teams to the self chat; nothing is typed
  or sent), only when Teams is already running and no kimeru cycle holds approvals.lock.
#>
param(
  [switch]$NoProbe,       # tests: do not read the self chat
  [switch]$NoClipboard,   # tests: leave the clipboard alone
  [switch]$NoDrive,       # tests: do not copy into Google Drive
  [string]$ResultFile = ''
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$data = if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }
if (-not $ResultFile) { $ResultFile = Join-Path $root 'kimeru-diagnose-result.txt' }
Write-Host 'kimeru 診断（質問しない・何も送らない・記録を変えない）' -ForegroundColor Green

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
if (-not (Test-Path $data)) { Write-Host 'データフォルダがありません（自動運転をまだ一度も動かしていない可能性があります）' -ForegroundColor Yellow }

$teamsVer = ''
try { $teamsVer = [string](Get-AppxPackage -Name MSTeams -ErrorAction SilentlyContinue).Version } catch {}
$a = @('-m', 'kimeru', '--out', "`"$($data.TrimEnd([char]92))`"", 'diagnose', '--write', "`"$ResultFile`"")
if ($teamsVer) { $a += @('--teams-version', $teamsVer) }
if ($NoProbe) { $a += '--no-probe' }
if (-not $NoDrive) { $a += '--drive' }
if (-not $NoProbe) { Write-Host '自分とのチャットを 1 回読みます（Teams が自分とのチャットに切り替わることがあります。何も入力・送信しません）' }
# python's stderr goes to a file: under Windows PowerShell 5.1 a native program's stderr would otherwise become an error record
$err = [IO.Path]::GetTempFileName(); $so = [IO.Path]::GetTempFileName()
$env:PYTHONIOENCODING = 'utf-8'
$p = Start-Process -FilePath $py -ArgumentList ($a -join ' ') -NoNewWindow -Wait -PassThru -RedirectStandardOutput $so -RedirectStandardError $err
$console = @(Get-Content -Encoding UTF8 $so | Where-Object { $_ -like 'Google *' })
$problem = @(Get-Content -Encoding UTF8 $err | Select-Object -Last 3)
Remove-Item $so, $err -Force -ErrorAction SilentlyContinue
if (-not (Test-Path $ResultFile) -or $p.ExitCode -ne 0) {
  Write-Host "診断を作れませんでした（終了コード $($p.ExitCode)）" -ForegroundColor Yellow
  $problem | ForEach-Object { Write-Host $_ }
  exit 1
}
$sheet = @(Get-Content -Encoding UTF8 $ResultFile)
$sheet | ForEach-Object { Write-Host $_ }
if ($NoClipboard) { $clip = 'クリップボードは使っていません' }
else { try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'クリップボードにコピーできませんでした' } }
Write-Host ''
$console | ForEach-Object { Write-Host $_ }
Write-Host "結果ファイル: $ResultFile（$clip。共有する前に目を通してください）" -ForegroundColor Cyan
exit 0

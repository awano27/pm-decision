<#
.SYNOPSIS
  One-time setup of kimeru + local Kev on a company PC. No admin rights.

  install  1) Startup-folder shortcut that starts Kev at logon (minimized)
           2) user environment variable KIMERU_BACKEND=kev
           3) Task Scheduler entry "kimeru-daily": one daily cycle every N minutes,
              hidden (VBS runner), data kept in %LOCALAPPDATA%\kimeru (survives re-downloading the ZIP)
  remove   undoes 1-3 (keeps %LOCALAPPDATA%\kimeru data)
  status   shows what is installed and whether Kev answers

  Usage: setup-company.cmd install [-KevDir C:\kev] [-Minutes 5]
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [Parameter(Position = 0)][ValidateSet('install', 'remove', 'status')][string]$Action = 'status',
  [string]$KevDir = 'C:\kev',
  [int]$Minutes = 5,
  [string]$AdoOrg = '',       # with -AdoProject: the daily loop also pulls new ADO work items (needs az, e.g. C:\az)
  [string]$AdoProject = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$data = Join-Path $env:LOCALAPPDATA 'kimeru'
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'kimeru-kev.lnk'
$task = 'kimeru-daily'

function Find-Python {
  $emb = Join-Path $root '.python\python.exe'
  if (Test-Path $emb) { return $emb }
  foreach ($c in 'python', 'py') {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $v = & $cmd.Source --version 2>&1 | Out-String
    if ($v -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) { return $cmd.Source }
  }
  $null
}

function Kev-Up {
  try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 5; $true } catch { $false }
}

function Show-Status {
  $taskInfo = cmd /c "schtasks /Query /TN $task /FO LIST 2>nul"   # via cmd: PS 5.1 + Stop turns native stderr into errors
  $user = [Environment]::GetEnvironmentVariable('KIMERU_BACKEND', 'User')
  $last = Join-Path $data 'daily.log.jsonl'
  ([ordered]@{
    'Kev フォルダ'           = $(if (Test-Path (Join-Path $KevDir 'start-kev.cmd')) { "あり ($KevDir)" } else { "なし ($KevDir)" })
    'Kev 自動起動'           = $(if (Test-Path $lnk) { 'あり' } else { 'なし' })
    'Kev 応答'               = $(if (Kev-Up) { 'OK (127.0.0.1:8009)' } else { '応答なし' })
    'KIMERU_BACKEND'         = $(if ($user) { $user } else { '未設定' })
    '自動運転 (kimeru-daily)' = $(if ($LASTEXITCODE -eq 0 -and $taskInfo) { ($taskInfo | Select-String '状態|Status' | Select-Object -First 1).Line.Trim() } else { '未登録' })
    'データ'                 = $data
    '最後のサイクル'          = $(if (Test-Path $last) { (Get-Content $last -Tail 1 -Encoding UTF8) -replace '^(.{0,160}).*$', '$1' } else { 'まだなし' })
  }).GetEnumerator() | ForEach-Object { '{0,-24} {1}' -f $_.Key, $_.Value }
}

switch ($Action) {
  'status' { Show-Status }

  'install' {
    $kevCmd = Join-Path $KevDir 'start-kev.cmd'
    if (-not (Test-Path $kevCmd)) { throw "start-kev.cmd not found in $KevDir (copy the kev bundle there first, or pass -KevDir)" }
    $py = Find-Python
    if (-not $py) { throw 'Python 3.10+ not found. Run run-company-check.cmd once (it can fetch the no-install Python into .python).' }

    # 1) Kev at logon (minimized window)
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $kevCmd
    $s.WorkingDirectory = $KevDir
    $s.WindowStyle = 7
    $s.Description = 'kimeru: local Kev decision model'
    $s.Save()
    Write-Host "1) Kev 自動起動: $lnk"

    # 2) backend for interactive kimeru commands
    [Environment]::SetEnvironmentVariable('KIMERU_BACKEND', 'kev', 'User')
    Write-Host '2) KIMERU_BACKEND=kev（ユーザー環境変数）'

    # 3) daily cycle every N minutes, hidden, data in %LOCALAPPDATA%\kimeru
    New-Item -ItemType Directory -Force $data | Out-Null
    Push-Location $root
    $extra = if ($AdoOrg -and $AdoProject) { "--ado-org `"$AdoOrg`" --ado-project `"$AdoProject`"" } else { '' }
    try { & $py -m kimeru --backend kev --out $data schedule install --minutes $Minutes --extra $extra; if ($LASTEXITCODE -ne 0) { throw 'schedule install failed' } }
    finally { Pop-Location }
    Write-Host "3) 自動運転: $Minutes 分ごと（データ: $data）"

    if (-not (Kev-Up)) {
      Write-Host ''
      Write-Host 'Kev がまだ起動していません。今すぐ使う場合は start-kev.cmd を実行してください（次回ログオンからは自動）。' -ForegroundColor Yellow
      Write-Host 'Kev が起動するまでの間に届いたイベントは受信箱に残り、起動後のサイクルで判断されます。'
    }
    Write-Host ''
    Show-Status
  }

  'remove' {
    cmd /c "schtasks /Delete /TN $task /F >nul 2>nul"
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    [Environment]::SetEnvironmentVariable('KIMERU_BACKEND', $null, 'User')
    Write-Host "削除しました（自動運転・Kev 自動起動・KIMERU_BACKEND）。データは残しています: $data"
  }
}

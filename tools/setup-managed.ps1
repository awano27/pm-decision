<#
.SYNOPSIS
  One-time setup of kimeru + local Kev on a managed PC. No admin rights.

  install  1) Startup-folder shortcut that starts Kev at logon (minimized)
           2) user environment variable KIMERU_BACKEND=kev
           3) Task Scheduler entry "kimeru-daily": one daily cycle every N minutes,
              hidden (VBS runner), data kept in %LOCALAPPDATA%\kimeru (survives re-downloading the ZIP)
  remove   undoes 1-3 (keeps %LOCALAPPDATA%\kimeru data)
  status   shows what is installed and whether Kev answers
  trial    install for a limited time (-Hours, default 8): records the window, registers a one-shot task
           "kimeru-trial-end" that runs remove and writes the result sheet at the end (also after the PC was
           asleep: StartWhenAvailable). -Sample adds one fictional ADO work item that lacks information.
  report   writes kimeru-autorun-result.txt (numbers and OK/NG only) and copies it to the clipboard

  Usage: setup-managed.cmd install [-KevDir <kev folder>] [-Minutes 5]
         setup-managed.cmd trial [-Hours 8] [-Minutes 5] [-AdoOrg X -AdoProject Y] [-Sample]
         setup-managed.cmd report
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [Parameter(Position = 0)][ValidateSet('install', 'remove', 'status', 'trial', 'report', 'trial-end')][string]$Action = 'status',
  [string]$KevDir = '',        # default: the kev folder next to this kimeru folder, else C:\kev
  [int]$Minutes = 5,
  [ValidateRange(0.1, 72)][double]$Hours = 8,   # trial: how long the automatic run stays on
  [switch]$Sample,                              # trial: one fictional ADO work item that lacks information

  [string]$AdoOrg = '',       # with -AdoProject: the daily loop also pulls new ADO work items (needs az, e.g. C:\az)
  [string]$AdoProject = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
if (-not $KevDir) { $KevDir = @((Join-Path (Split-Path -Parent $root) 'kev'), (Join-Path $root 'kev'), 'C:\kev') | Where-Object { Test-Path (Join-Path $_ 'start-kev.cmd') } | Select-Object -First 1; if (-not $KevDir) { $KevDir = 'C:\kev' } }
$data = if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'kimeru-kev.lnk'
$task = 'kimeru-daily'
$endTask = 'kimeru-trial-end'
$trialFile = Join-Path $data 'trial-autorun.json'
$resultFile = Join-Path $root 'kimeru-autorun-result.txt'

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

function Read-Trial {
  # the trial window recorded by `trial` (a missing or unreadable file gives $null)
  try { if (Test-Path $trialFile) { return (Get-Content -Raw -Encoding UTF8 $trialFile | ConvertFrom-Json) } } catch {}
  $null
}
function Get-TrialEnd($t) {
  if ($t.end -is [datetime]) { return $t.end }   # PowerShell 7 turns a date-like string into a date
  try { return [datetime]::ParseExact([string]$t.end, 'yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture) } catch { return $null }
}
function Test-TrialRunning {
  $t = Read-Trial
  [bool]($t -and -not $t.PSObject.Properties['ended'])
}
function Test-TrialOverdue {
  # a trial that is still recorded as running after its planned end (the PC was off or asleep, or the end task was removed)
  $t = Read-Trial
  if (-not $t -or $t.PSObject.Properties['ended']) { return $false }
  $e = Get-TrialEnd $t
  [bool]($e -and (Get-Date) -ge $e)
}
function Get-TrialText {
  $t = Read-Trial
  if (-not $t) { return 'なし' }
  if ($t.PSObject.Properties['ended']) { return "終了済み（$($t.ended)）。結果: $resultFile" }
  $e = Get-TrialEnd $t
  if ($e) { return "実行中。$($e.ToString('MM/dd HH:mm')) に自動で止まります" }
  '実行中'
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
    '試験運転'               = $(Get-TrialText)
    '最後のサイクル'          = $(if (Test-Path $last) { (Get-Content $last -Tail 1 -Encoding UTF8) -replace '^(.{0,160}).*$', '$1' } else { 'まだなし' })
  }).GetEnumerator() | ForEach-Object { '{0,-24} {1}' -f $_.Key, $_.Value }
}

$prevFile = Join-Path $data 'setup-previous.json'
$cfgFile = Join-Path $data 'config.json'
$cfgKeys = @('backend', 'ado_org', 'ado_project')   # the settings `schedule install` writes into config.json
function Get-ConfigValues {
  # the values of $cfgKeys in config.json (a missing key or file gives $null for that key); an unreadable file gives all $null
  $r = [ordered]@{}
  foreach ($k in $cfgKeys) { $r[$k] = $null }
  try {
    if (Test-Path $cfgFile) {
      $c = Get-Content -Raw -Encoding UTF8 $cfgFile | ConvertFrom-Json
      foreach ($k in $cfgKeys) { if ($c.PSObject.Properties[$k]) { $r[$k] = [string]$c.$k } }
    }
  } catch {}
  $r
}
function Get-PreviousConfigValues($prev) {
  # what the install recorded for config.json: the current form (configValues: backend, ado_org, ado_project) or the form an
  # older install wrote (configBackend only); nothing recorded gives $null
  if (-not $prev) { return $null }
  if ($prev.PSObject.Properties['configValues']) { return $prev.configValues }
  if ($prev.PSObject.Properties['configBackend']) { return [pscustomobject]@{ backend = $prev.configBackend } }
  $null
}
function Restore-ConfigValues($prevValues) {
  # puts back, in config.json, the settings the install recorded, as they were before the first install; other settings stay.
  # A setting the record does not mention was not written by the install (an older install wrote only backend): it is left alone
  if (-not $prevValues -or -not (Test-Path $cfgFile)) { return }
  try {
    $c = Get-Content -Raw -Encoding UTF8 $cfgFile | ConvertFrom-Json
    foreach ($k in $cfgKeys) {
      if (-not $prevValues.PSObject.Properties[$k]) { continue }
      $v = $prevValues.$k
      if ($null -ne $v) { $c | Add-Member -NotePropertyName $k -NotePropertyValue $v -Force }
      else { $c.PSObject.Properties.Remove($k) }
    }
    ($c | ConvertTo-Json -Depth 5) | Set-Content -Path $cfgFile -Encoding UTF8
  } catch { Write-Host "config.json の backend / ado_org / ado_project を戻せませんでした（手で kimeru config unset <設定> を実行してください）: $_" -ForegroundColor Yellow }
}
function Restore-Previous {
  # puts back KIMERU_BACKEND, the settings in config.json (backend, ado_org, ado_project) and the logon shortcut as they were before the first install
  $prev = if (Test-Path $prevFile) { Get-Content -Raw -Encoding UTF8 $prevFile | ConvertFrom-Json } else { $null }
  Restore-ConfigValues (Get-PreviousConfigValues $prev)
  cmd /c "schtasks /Delete /TN $task /F >nul 2>nul"
  if ((Test-Path $lnk) -and -not ($prev -and $prev.shortcut)) { Remove-Item $lnk -Force }
  [Environment]::SetEnvironmentVariable('KIMERU_BACKEND', $(if ($prev) { $prev.backend } else { $null }), 'User')
  if (Test-Path $prevFile) { Remove-Item $prevFile -Force }
}

function Write-Report([string]$how) {
  # kimeru-autorun-result.txt (numbers and OK/NG only) next to the scripts, and a copy on the clipboard
  $py = Find-Python
  if (-not $py) { Write-Host 'Python 3.10+ が見つからないため、結果ファイルを作れません（run-check.cmd を一度実行すると取得できます）' -ForegroundColor Yellow; return }
  $a = @('-m', 'kimeru', '--out', $data, 'schedule', 'report', '--write', $resultFile)
  if ($how) { $a += @('--end-trial', $how) }
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'   # python's stderr must not become a terminating error under PS 5.1
  Push-Location $root
  try { $lines = @(& $py @a 2>&1 | ForEach-Object { "$_" }) } finally { Pop-Location; $ErrorActionPreference = $prevEap }
  $lines | ForEach-Object { Write-Host $_ }
  try { ($lines -join "`r`n") | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = "$resultFile を開いてコピーしてください" }
  Write-Host ''
  Write-Host "結果ファイル: $resultFile（$clip。共有する前に目を通してください）"
}

function Remove-EndTask {
  cmd /c "schtasks /Delete /TN $endTask /F >nul 2>nul"
}

function Remove-Everything {
  # the same as `remove`: the scheduled run, the end task, Kev autostart, KIMERU_BACKEND and the config.json settings
  cmd /c "schtasks /Delete /TN $task /F >nul 2>nul"
  Remove-EndTask
  Restore-Previous
}

function End-Trial([string]$how) {
  # stop everything the trial turned on, then write the result sheet
  Remove-Everything
  Write-Host '試験運転を終えました（自動運転・Kev 自動起動を外し、環境変数と設定は導入前に戻しました）。'
  Write-Report $how
}

function Register-EndTask([datetime]$end) {
  # one-shot task at the planned end: no admin (least privilege), hidden (VBS runner), and StartWhenAvailable so a PC that was
  # asleep or off at that time runs it as soon as it can
  $vbs = Join-Path $data 'run-trial-end.vbs'
  $ps1 = Join-Path $PSScriptRoot 'setup-managed.ps1'
  $state = if ($env:KIMERU_STATE_DIR) { "cmd /c set ""KIMERU_STATE_DIR=$($env:KIMERU_STATE_DIR)"" && " } else { '' }
  $line = "${state}powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$ps1"" trial-end"
  $vbsText = 'WScript.Quit CreateObject("WScript.Shell").Run("' + $line.Replace('"', '""') + '", 0, True)' + "`r`n"
  [IO.File]::WriteAllText($vbs, $vbsText, [Text.Encoding]::Unicode)   # UTF-16: WSH reads UTF-8 as ANSI
  $vbsXml = [Security.SecurityElement]::Escape($vbs)
  $when = $end.ToString('yyyy-MM-ddTHH:mm:ss')
  $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>kimeru: stops the trial run and writes the result sheet</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$when</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$vbsXml"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
  $xmlFile = Join-Path $data 'trial-end-task.xml'
  [IO.File]::WriteAllText($xmlFile, $xml, [Text.Encoding]::Unicode)
  $res = cmd /c "schtasks /Create /TN $endTask /XML ""$xmlFile"" /F 2>&1"
  if ($LASTEXITCODE -ne 0) { throw "終了用のタスクを登録できませんでした: $res" }
}

function Start-Trial {
  # after `install`: record the window (and the fictional item), then register the end task. If that fails, nothing is left running.
  $py = Find-Python
  $a = @('-m', 'kimeru', '--out', $data, 'schedule', 'trial-start', '--hours', "$Hours", '--minutes', "$Minutes")
  if ($Sample) { $a += '--sample' }
  if ($AdoOrg -and $AdoProject) { $a += @('--ado-org', $AdoOrg, '--ado-project', $AdoProject) }
  Push-Location $root
  try { $res = @(& $py @a); if ($LASTEXITCODE -ne 0) { throw 'trial-start failed' } } finally { Pop-Location }
  $endLine = $res | Where-Object { $_ -like 'end=*' } | Select-Object -First 1
  $end = [datetime]::ParseExact(([string]$endLine).Substring(4), 'yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
  Register-EndTask $end
  $res | Where-Object { $_ -notlike 'end=*' } | ForEach-Object { Write-Host $_ }
}

switch ($Action) {
  'status' {
    if (Test-TrialOverdue) { Write-Host '試験運転の終了時刻を過ぎていたため、ここで止めて結果ファイルを作ります。'; End-Trial 'timer'; Write-Host '' }
    Show-Status
  }

  'report' {
    if (Test-TrialOverdue) { Write-Host '試験運転の終了時刻を過ぎていたため、ここで止めます。'; End-Trial 'timer' }
    else { Write-Report '' }
  }

  'trial-end' {
    # run by the end task (hidden); a trial that was already stopped by hand has nothing left to do
    if (Test-TrialRunning) { End-Trial 'timer' } else { Remove-EndTask }
  }

  { $_ -in 'install', 'trial' } {
    $kevCmd = Join-Path $KevDir 'start-kev.cmd'
    if (-not (Test-Path $kevCmd)) { throw "start-kev.cmd not found in $KevDir (copy the kev bundle there first, or pass -KevDir)" }
    $py = Find-Python
    if (-not $py) { throw 'Python 3.10+ not found. Run run-check.cmd once (it can fetch the no-install Python into .python).' }

    # remember what was there before the first install, so remove (or a failed install) can put it back
    New-Item -ItemType Directory -Force $data | Out-Null
    if (-not (Test-Path $prevFile)) {
      @{ backend = [Environment]::GetEnvironmentVariable('KIMERU_BACKEND', 'User'); shortcut = [bool](Test-Path $lnk); configValues = (Get-ConfigValues) } |
        ConvertTo-Json | Set-Content -Path $prevFile -Encoding UTF8
    }
    try {
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
    Push-Location $root
    # arguments as an array, optional ones only when set: PS 5.1 drops an empty string argument
    $sched = @('-m', 'kimeru', '--backend', 'kev', '--out', $data, 'schedule', 'install', '--minutes', "$Minutes")
    if ($AdoOrg -and $AdoProject) { $sched += @('--ado-org', $AdoOrg, '--ado-project', $AdoProject) }
    try { & $py @sched; if ($LASTEXITCODE -ne 0) { throw 'schedule install failed' } }
    finally { Pop-Location }
    Write-Host "3) 自動運転: $Minutes 分ごと（データ: $data）"
    } catch {
      Write-Host "導入に失敗したので元に戻します: $_" -ForegroundColor Yellow
      Restore-Previous
      throw
    }

    if (-not (Kev-Up)) {
      Write-Host ''
      Write-Host 'Kev がまだ起動していません。今すぐ使う場合は start-kev.cmd を実行してください（次回ログオンからは自動）。' -ForegroundColor Yellow
      Write-Host 'Kev が起動するまでの間に届いたイベントは受信箱に残り、起動後のサイクルで判断されます。'
    }
    Write-Host ''
    Show-Status
    if ($Action -eq 'trial') {
      try { Start-Trial }
      catch {
        Write-Host "試験運転を始められなかったので、導入したものを外します: $_" -ForegroundColor Yellow
        Remove-Everything
        throw
      }
    }
  }

  'remove' {
    if (Test-TrialRunning) {
      # stopped by hand during a trial: the end task goes too, and the result sheet is written now
      Write-Host '試験運転の途中で止めます。'
      End-Trial 'manual'
    } else {
      Remove-Everything
    }
    Write-Host "削除しました（自動運転・Kev 自動起動）。KIMERU_BACKEND と設定ファイルの backend・ado_org・ado_project は導入前の値に戻しました。データは残しています: $data"
  }
}

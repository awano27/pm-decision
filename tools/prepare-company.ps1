<#
.SYNOPSIS
  Monday prep on a company PC: check everything that must be carried in, start Kev, sign in to az.
  Changes nothing except starting Kev (minimized) and, if you agree, `az login`. No admin rights.

  Usage: prepare-company.cmd [-KevDir C:\kev] [-AzDir C:\az]
#>
[CmdletBinding(PositionalBinding = $false)]
param([string]$KevDir = 'C:\kev', [string]$AzDir = 'C:\az', [int]$KevWaitSec = 600)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$missing = New-Object System.Collections.Generic.List[string]

function Line($ok, $what, $detail) {
  $mark = if ($ok) { 'OK ' } else { 'NG ' }
  Write-Host ("  {0} {1,-22} {2}" -f $mark, $what, $detail) -ForegroundColor $(if ($ok) { 'Green' } else { 'Yellow' })
  if (-not $ok) { $missing.Add("${what}: $detail") }
}
function KevUp { try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 3; $true } catch { $false } }

Write-Host "kimeru 月曜の準備チェック" -ForegroundColor Cyan

# 1) placement
$py = Join-Path $root '.python\python.exe'
$sysPy = foreach ($c in 'python', 'py') { $cmd = Get-Command $c -ErrorAction SilentlyContinue; if ($cmd -and ((& $cmd.Source --version 2>&1 | Out-String) -match 'Python 3\.(1\d|[2-9]\d)')) { $cmd.Source; break } }
Line (Test-Path (Join-Path $root 'kimeru\cli.py')) 'kimeru' $root
Line ((Test-Path $py) -or $sysPy) 'Python' $(if (Test-Path $py) { $py } elseif ($sysPy) { $sysPy } else { '.python がない → 前回の .python フォルダをこのフォルダにコピー（または run-company-check.cmd で取得）' })
Line (Test-Path (Join-Path $KevDir 'start-kev.cmd')) 'Kev フォルダ' $(if (Test-Path (Join-Path $KevDir 'start-kev.cmd')) { $KevDir } else { "$KevDir にない → 開発 PC の C:\develop\kev-bundle をコピー" })
Line (Test-Path (Join-Path $KevDir 'models\kev-4b\head.pt')) 'Kev モデル' $(if (Test-Path (Join-Path $KevDir 'models\kev-4b\head.pt')) { 'kev-4b' } else { 'models\kev-4b がない（コピー途中？）' })
$az = @($env:KIMERU_AZ, (Get-Command az -ErrorAction SilentlyContinue).Source, (Join-Path $AzDir 'bin\az.cmd')) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
Line ([bool]$az) 'Azure CLI' $(if ($az) { $az } else { "$AzDir にない → 開発 PC の C:\develop\az-bundle\az をコピー" })

# 2) resources
$os = Get-CimInstance Win32_OperatingSystem
$freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$freeDisk = [math]::Round((Get-PSDrive C).Free / 1GB)
if (KevUp) { Line $true '空きメモリ' "${freeRam}GB（Kev は起動済みで使用中）" }
else { Line ($freeRam -ge 11) '空きメモリ' "${freeRam}GB（Kev に約 10GB 必要）$(if ($freeRam -lt 11) { ' → ブラウザのタブ等を閉じる' })" }
Line ($freeDisk -ge 2) '空きディスク' "${freeDisk}GB"

# 3) Kev
if (KevUp) { Line $true 'Kev の起動' '起動済み（127.0.0.1:8009）' }
elseif (Test-Path (Join-Path $KevDir 'start-kev.cmd')) {
  Write-Host "  .. Kev を起動します（最小化したウィンドウ。閉じないでください）"
  Start-Process -FilePath (Join-Path $KevDir 'start-kev.cmd') -WorkingDirectory $KevDir -WindowStyle Minimized
  $deadline = (Get-Date).AddSeconds($KevWaitSec)
  while ((Get-Date) -lt $deadline -and -not (KevUp)) { Start-Sleep -Seconds 5; Write-Host -NoNewline '.' }
  Write-Host ''
  Line (KevUp) 'Kev の起動' $(if (KevUp) { '起動しました' } else { "$KevWaitSec 秒で応答なし → 最小化された Kev のウィンドウのエラーを確認" })
}

# 4) az sign-in
if ($az) {
  & $az account show -o none 2>$null
  if ($LASTEXITCODE -ne 0) {
    $ans = Read-Host "  az にサインインしていません。今サインインしますか（ブラウザが開きます）[y/N]"
    if ($ans -match '^\s*[yY]') { & $az login -o none 2>&1 | Out-Null }
    & $az account show -o none 2>$null
  }
  Line ($LASTEXITCODE -eq 0) 'az サインイン' $(if ($LASTEXITCODE -eq 0) { 'サインイン済み' } else { '未サインイン（条件付きアクセスで拒否された場合はその旨を記録）' })
}

Write-Host ''
if ($missing.Count -eq 0) {
  Write-Host "準備 OK。次は .\run-company-check.cmd monday" -ForegroundColor Green
} else {
  Write-Host "足りないもの（$($missing.Count) 件）:" -ForegroundColor Yellow
  $missing | ForEach-Object { Write-Host "  - $_" }
}

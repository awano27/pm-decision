<#
.SYNOPSIS
  Build the small update zip for the company PC: the committed files at HEAD plus a VERSION line.
.DESCRIPTION
  The first delivery (Python, Kev, Azure CLI, ...) is tools\make-bundle.ps1 -Zip. Later updates only need this
  (0.2 MB): extract it over C:\kimeru-pc on the company PC and check `Get-Content kimeru\VERSION`.
  -CopyTo <folder> also copies the zip there (a synced Google Drive / OneDrive folder, for example).
#>
param(
  [string]$Stage = (Join-Path $env:TEMP 'kimeru-update'),
  [string]$Zip = (Join-Path $env:TEMP 'kimeru-update.zip'),
  [string]$CopyTo = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pack = Join-Path $Stage 'kimeru-pc'
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force (Join-Path $pack 'kimeru') | Out-Null
$src = Join-Path $env:TEMP 'kimeru-update-src.zip'
git -C $root archive --format=zip -o $src HEAD
Expand-Archive -Path $src -DestinationPath (Join-Path $pack 'kimeru') -Force
Remove-Item -Force $src
$ver = "kimeru $(git -C $root rev-parse --short HEAD) built $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Set-Content -Path (Join-Path $pack 'kimeru\VERSION') -Value $ver -Encoding ASCII
if (Test-Path $Zip) { Remove-Item -Force $Zip }
& "$env:SystemRoot\System32\tar.exe" -a -c -f $Zip -C $Stage 'kimeru-pc'
Write-Host ("{0}  ({1:N1} MB)" -f $ver, ((Get-Item $Zip).Length / 1MB))
if ($CopyTo) {
  if (-not (Test-Path $CopyTo)) { throw "copy target not found: $CopyTo" }
  Copy-Item -Force $Zip $CopyTo
  Write-Host "copied to $CopyTo"
}

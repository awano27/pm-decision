<#
.SYNOPSIS
  On the development PC: build ONE folder to carry to a company PC (Remote Desktop copy).

    <Out>\kimeru   this repo at HEAD (git archive: committed files only, no private data)
    <Out>\kev      the Kev bundle (start-kev.cmd, Python, torch, model)
    <Out>\az       the no-install Azure CLI ZIP, unpacked

  On the company PC: copy <Out> anywhere, then double-click kimeru\START.cmd.

  Usage: powershell -ExecutionPolicy Bypass -File tools\make-bundle.ps1 [-Out C:\develop\kimeru-pc]
           [-KevSrc C:\develop\kev-bundle] [-AzSrc C:\develop\az-bundle\az]
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Out = 'C:\develop\kimeru-pc',
  [string]$KevSrc = 'C:\develop\kev-bundle',
  [string]$AzSrc = 'C:\develop\az-bundle\az'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

foreach ($p in @(@($KevSrc, 'start-kev.cmd'), @($AzSrc, 'bin\az.cmd'))) {
  if (-not (Test-Path (Join-Path $p[0] $p[1]))) { throw "$($p[1]) not found in $($p[0])" }
}
New-Item -ItemType Directory -Force $Out | Out-Null

# kimeru: committed files only, so nothing private or half-edited is carried
$zip = Join-Path $env:TEMP 'kimeru-bundle.zip'
git -C $root archive --format=zip -o $zip HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$dst = Join-Path $Out 'kimeru'
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Expand-Archive -Path $zip -DestinationPath $dst
Remove-Item $zip

# kev and az: mirror (only changed files are copied again on a rebuild)
foreach ($pair in @(@($KevSrc, 'kev'), @($AzSrc, 'az'))) {
  robocopy $pair[0] (Join-Path $Out $pair[1]) /MIR /NFL /NDL /NJH /NP /R:1 /W:1 | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "copy failed: $($pair[0])" }
}

$size = [math]::Round(((Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum) / 1GB, 1)
$head = git -C $root rev-parse --short HEAD
Write-Host "done: $Out (${size}GB, kimeru $head)"
Write-Host "company PC: copy this folder anywhere, then double-click kimeru\START.cmd"

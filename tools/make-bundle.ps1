<#
.SYNOPSIS
  On the development PC: build ONE folder to carry to a company PC (Remote Desktop copy).

    <Out>\kimeru   this repo at HEAD (git archive: committed files only, no private data)
    <Out>\kev      the Kev bundle (start-kev.cmd, Python, torch, model)
    <Out>\az       the no-install Azure CLI ZIP, unpacked
    <Out>\kimeru\.python  python.org's no-install Python (embeddable, signature checked)

    <Out>\START.cmd  the one thing to double-click on the company PC

  On the company PC: copy <Out> (the whole folder) anywhere, then double-click <Out>\START.cmd.

  -Zip also writes <Out>.zip: ONE file is far faster to copy over Remote Desktop than ~45,000 small
  ones. On the company PC:  C:\Windows\System32\tar.exe -xf C:\kimeru-pc.zip -C C:\   (built into Windows, faster than Explorer)

  Usage: powershell -ExecutionPolicy Bypass -File tools\make-bundle.ps1 [-Out C:\develop\kimeru-pc]
           [-KevSrc C:\develop\kev-bundle] [-AzSrc C:\develop\az-bundle\az]
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Out = 'C:\develop\kimeru-pc',
  [string]$KevSrc = 'C:\develop\kev-bundle',
  [string]$AzSrc = 'C:\develop\az-bundle\az',
  [string]$PyZip = 'C:\develop\bundle-cache\python-3.12.10-embed-amd64.zip',  # downloaded once, then reused
  [switch]$Zip
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

foreach ($p in @(@($KevSrc, 'start-kev.cmd'), @($AzSrc, 'bin\az.cmd'))) {
  if (-not (Test-Path (Join-Path $p[0] $p[1]))) { throw "$($p[1]) not found in $($p[0])" }
}
New-Item -ItemType Directory -Force $Out | Out-Null

# kimeru: committed files only, so nothing private or half-edited is carried
$srcZip = Join-Path $env:TEMP 'kimeru-bundle.zip'
git -C $root archive --format=zip -o $srcZip HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$buildId = "kimeru $(git -C $root rev-parse --short HEAD) built $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
$dst = Join-Path $Out 'kimeru'
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Expand-Archive -Path $srcZip -DestinationPath $dst
Set-Content -Path (Join-Path $dst 'VERSION') -Value $buildId -Encoding ASCII
Remove-Item $srcZip

# no-install Python inside kimeru, so the company PC never has to fetch it
if (-not (Test-Path $PyZip)) {
  New-Item -ItemType Directory -Force (Split-Path $PyZip) | Out-Null
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile $PyZip -UseBasicParsing
}
$pyDir = Join-Path $dst '.python'
Expand-Archive -Path $PyZip -DestinationPath $pyDir -Force
$sig = Get-AuthenticodeSignature (Join-Path $pyDir 'python.exe')
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
  Remove-Item -Recurse -Force $pyDir
  throw "python.exe signature not valid ($($sig.Status)); removed"
}
# embeddable Python ignores PYTHONPATH and the current folder: add the repo root to its ._pth
$pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
Add-Content -Path $pth.FullName -Value '..' -Encoding ASCII

# kev and az: mirror (only changed files are copied again on a rebuild). Link-time files
# (*.lib import libraries, *.pdb debug symbols, ~0.85GB) and bytecode caches are never loaded at run time.
foreach ($pair in @(@($KevSrc, 'kev'), @($AzSrc, 'az'))) {
  robocopy $pair[0] (Join-Path $Out $pair[1]) /MIR /XF *.lib *.pdb /XD __pycache__ /NFL /NDL /NJH /NP /R:1 /W:1 | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "copy failed: $($pair[0])" }
}

Set-Content -Path (Join-Path $Out 'START.cmd') -Encoding ASCII -Value @(
  '@echo off', 'rem kimeru: double-click this after copying the whole folder', 'call "%~dp0kimeru\START.cmd"')

$size = [math]::Round(((Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum) / 1GB, 1)
$head = git -C $root rev-parse --short HEAD
Write-Host "done: $Out (${size}GB, kimeru $head)"
Write-Host "company PC: copy this folder anywhere, then double-click START.cmd in it"

if ($Zip) {
  $zipPath = "$Out.zip"
  if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
  # built-in bsdtar: zip format, so the company PC needs nothing extra to unpack it
  $bsdtar = Join-Path $env:SystemRoot 'System32\tar.exe'   # not Git's GNU tar, which cannot write zip
  & $bsdtar -a -c -f $zipPath -C (Split-Path -Parent $Out) (Split-Path -Leaf $Out)
  if ($LASTEXITCODE -ne 0) { throw 'zip failed' }
  $zs = [math]::Round((Get-Item $zipPath).Length / 1GB, 1)
  Write-Host "zip:  $zipPath (${zs}GB). company PC: C:\Windows\System32\tar.exe -xf <zip> -C C:\  then C:\$(Split-Path -Leaf $Out)\START.cmd"
}

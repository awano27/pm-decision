<#
.SYNOPSIS
  Build the small update zip for the managed PC: the committed files at HEAD plus a VERSION line.
.DESCRIPTION
  The first delivery (Python, Kev, Azure CLI, ...) is tools\make-bundle.ps1 -Zip. Later updates only need this
  (0.2 MB): extract it over C:\kimeru-pc on the managed PC and check `Get-Content kimeru\VERSION`.
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
# the file list of this version: check-auto.ps1 names the files an older version left behind (extracting over the old
# folder keeps files this version deleted)
git -C $root -c core.quotepath=off ls-files | Set-Content -Path (Join-Path $pack 'kimeru\FILES') -Encoding UTF8
$ver = "kimeru $(git -C $root rev-parse --short HEAD) built $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Set-Content -Path (Join-Path $pack 'kimeru\VERSION') -Value $ver -Encoding ASCII
# SHA256SUMS: tools\sha256sums.ps1 -Check C:\kimeru-pc\kimeru checks the extracted files against it
& (Join-Path $PSScriptRoot 'sha256sums.ps1') -Write (Join-Path $pack 'kimeru')
if (Test-Path $Zip) { Remove-Item -Force $Zip }
& "$env:SystemRoot\System32\tar.exe" -a -c -f $Zip -C $Stage 'kimeru-pc'
Write-Host ("{0}  ({1:N1} MB)" -f $ver, ((Get-Item $Zip).Length / 1MB))
# the zip's SHA-256 next to it; tools\ship.ps1 also puts it in the tag, and so in the GitHub Release notes
$sha = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Path "$Zip.sha256" -Value "$sha  $(Split-Path -Leaf $Zip)" -Encoding ASCII
Write-Host "SHA-256 $sha"
if ($CopyTo) {
  if (-not (Test-Path $CopyTo)) { throw "copy target not found: $CopyTo" }
  Copy-Item -Force $Zip, "$Zip.sha256" $CopyTo
  Write-Host "copied to $CopyTo"
}

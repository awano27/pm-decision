<#
.SYNOPSIS
  The release pipeline: prepare the version commit, then tag, publish and build the update zip.
.DESCRIPTION
  1. ship.ps1 -Version 1.4.0 -Step prepare
       Sets the version (pyproject, package, README badge) and turns the unreleased CHANGELOG heading into
       '## 1.4.0（today）'. Write the summary paragraph, commit 'Release 1.4.0' and merge it to main.
  2. ship.ps1 -Version 1.4.0            (-Step publish, the default)
       On a clean main that matches origin/main: tools\ship.py check, the unit tests and graph validation, then the
       annotated tag 'v1.4.0' and its push. The tag push runs .github\workflows\release.yml, which tests again and
       publishes the GitHub Release. Last, tools\release.ps1 builds the update zip and copies it to -CopyTo
       (default: the KIMERU_RELEASE_COPYTO environment variable; none means the zip stays in TEMP).
  -DryRun runs every check and says what it would tag, push and copy, without doing it.
#>
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [ValidateSet('prepare', 'publish')][string]$Step = 'publish',
  [string]$CopyTo = $env:KIMERU_RELEASE_COPYTO,
  [switch]$SkipTests,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$Version = $Version.TrimStart('v')
$tag = "v$Version"
function Run([string]$what, [scriptblock]$cmd) {
  Write-Host "== $what"
  & $cmd
  if ($LASTEXITCODE) { throw "$what failed (exit $LASTEXITCODE)" }
}

Push-Location $root
try {
  if ($Step -eq 'prepare') {
    Run 'bump' { python tools/ship.py bump $Version }
    git status --short
    return
  }

  Run 'fetch' { git fetch -q origin main --tags }
  $branch = git rev-parse --abbrev-ref HEAD
  if ($branch -ne 'main') { throw "publish from main (now on $branch)" }
  if (git status --porcelain) { throw 'the working tree has changes; commit or stash them first' }
  if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) { throw 'main differs from origin/main; pull or push first' }
  git rev-parse -q --verify "refs/tags/$tag" | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "$tag already exists" }
  $global:LASTEXITCODE = 0

  Run 'version and CHANGELOG' { python tools/ship.py check $Version }
  if (-not $SkipTests) {
    Run 'unit tests' { python -m unittest -q }
    Run 'graph validation' { python -m kimeru validate }
  }

  $head = git rev-parse --short HEAD
  if ($DryRun) {
    Write-Host "dry run: would tag $tag at $head, push it (release.yml publishes 'kimeru $tag'), build the update zip$(if ($CopyTo) { " and copy it to $CopyTo" })"
    return
  }
  # the update zip first: its SHA-256 goes into the tag, and release.yml copies it into the Release notes, so the PC that
  # receives the zip can check it against a second channel (tools\sha256sums.ps1 -Zip <zip> -Sha256 <hash>)
  $zip = Join-Path $env:TEMP 'kimeru-update.zip'
  & (Join-Path $PSScriptRoot 'release.ps1') -Zip $zip
  $sha = ((Get-Content -LiteralPath "$zip.sha256" -TotalCount 1) -split '\s+')[0]
  if ($sha -notmatch '^[0-9a-f]{64}$') { throw "no SHA-256 for $zip" }
  Run "tag $tag" { git tag -a $tag -m "kimeru $Version" -m "kimeru-update.zip SHA-256: $sha" }
  Run "push $tag" { git push origin $tag }
  Write-Host "release.yml now publishes 'kimeru $tag': gh run list --workflow release.yml -L 1"
  if ($CopyTo) {
    if (-not (Test-Path $CopyTo)) { throw "copy target not found: $CopyTo" }
    Copy-Item -Force $zip, "$zip.sha256" $CopyTo
    Write-Host "copied to $CopyTo"
  }
}
finally {
  Pop-Location
}

<#
.SYNOPSIS
  SHA-256 lists for what is carried to another PC, and the check on arrival.
.DESCRIPTION
  -Write <folder>   writes <folder>\SHA256SUMS (every file but bytecode caches) and prints its fingerprint.
                    tools\make-bundle.ps1 runs it for kimeru, kev and az; tools\release.ps1 for the update zip.
  -Check <folder>   checks every SHA256SUMS in <folder> and below it (C:\kimeru-pc: kimeru, kimeru\.python, kev, az).
                    A folder with its own list is left out of the list of the folder above it.
                    NG: a listed file that is missing or differs, or an unlisted file of a kind that can run code
                    (.py .pyd .dll .exe .cmd .ps1 .pth ...; outside __pycache__). It prints each list's fingerprint:
                    compare it with the one printed where the folder was built (a changed list changes it).
  -Zip <file> [-Sha256 <hex>]   prints the zip's SHA-256 and, with -Sha256, compares (the GitHub Release of each
                    version names the SHA-256 of its update zip). Run the copy of this script that is already on
                    the PC, not the one inside the zip.
  Exit code 1 on any NG.
#>
param([string]$Write = '', [string]$Check = '', [string]$Zip = '', [string]$Sha256 = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Name = 'SHA256SUMS'
$Code = @('.py', '.pyc', '.pyd', '.pyw', '.pth', '.dll', '.exe', '.cmd', '.bat', '.ps1', '.psm1', '.vbs', '.js', '.jar', '.so')

function Rel([string]$base, [string]$full) { $full.Substring($base.Length + 1).Replace('\', '/') }
function Lists([string]$base) {
  # folders below $base that carry their own list (kimeru\.python from make-bundle): checked on their own
  @(Get-ChildItem -LiteralPath $base -Recurse -File -Force -Filter $Name | Where-Object { $_.DirectoryName -ne $base } | ForEach-Object { $_.DirectoryName + '\' })
}
function Files([string]$base) {
  $own = Lists $base
  Get-ChildItem -LiteralPath $base -Recurse -File -Force | Where-Object {
    $f = $_.FullName
    $_.Name -ne $Name -and $_.Extension -ne '.pyc' -and $f -notmatch '\\__pycache__\\' -and
      -not ($own | Where-Object { $f.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })
  }
}
function Hash([string]$path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Fingerprint([string]$list) { (Hash $list).Substring(0, 16) }

if ($Write) {
  $base = (Get-Item -LiteralPath $Write).FullName.TrimEnd('\')
  $lines = Files $base | ForEach-Object { "{0}  {1}" -f (Hash $_.FullName), (Rel $base $_.FullName) } | Sort-Object { $_.Substring(66) }
  $list = Join-Path $base $Name
  [IO.File]::WriteAllText($list, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
  Write-Host ("{0}: {1} files, fingerprint {2}" -f $list, @($lines).Count, (Fingerprint $list))
  exit 0
}

if ($Zip) {
  $h = Hash (Get-Item -LiteralPath $Zip).FullName
  Write-Host "SHA-256 $h  $Zip"
  if (-not $Sha256) { exit 0 }
  if ($h -eq $Sha256.Trim().ToLowerInvariant()) { Write-Host 'OK 一致しました' -ForegroundColor Green; exit 0 }
  Write-Host 'NG 一致しません。この zip は展開しないでください（取得し直すか、作った人に確かめる）' -ForegroundColor Red
  exit 1
}

if (-not $Check) { Get-Help $PSCommandPath -Detailed; exit 2 }
$top = (Get-Item -LiteralPath $Check).FullName.TrimEnd('\')
$folders = @(@($top) + @(Get-ChildItem -LiteralPath $top -Recurse -File -Force -Filter $Name | ForEach-Object DirectoryName) |
  Select-Object -Unique | Where-Object { Test-Path -LiteralPath (Join-Path $_ $Name) })
if (-not $folders) { Write-Host "NG $Name がありません: $top"; exit 1 }
$bad = 0
foreach ($base in $folders) {
  $want = @{}
  foreach ($line in [IO.File]::ReadAllLines((Join-Path $base $Name))) {
    if ($line -match '^([0-9a-f]{64})  (.+)$') { $want[$Matches[2]] = $Matches[1] }
  }
  $ng = New-Object Collections.Generic.List[string]
  foreach ($rel in $want.Keys) {
    $p = Join-Path $base $rel
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { $ng.Add("ありません: $rel") }
    elseif ((Hash $p) -ne $want[$rel]) { $ng.Add("違います: $rel") }
  }
  foreach ($f in Files $base) {
    $rel = Rel $base $f.FullName
    if (-not $want.ContainsKey($rel) -and $Code -contains $f.Extension.ToLowerInvariant()) { $ng.Add("一覧に無いコードのファイル: $rel（古い版の残りか、後から置かれたもの）") }
  }
  $fp = Fingerprint (Join-Path $base $Name)
  if ($ng.Count -eq 0) { Write-Host ("OK {0}: {1} ファイル一致（一覧の指紋 {2}）" -f $base, $want.Count, $fp) -ForegroundColor Green }
  else {
    Write-Host ("NG {0}: {1} 件（一覧の指紋 {2}）" -f $base, $ng.Count, $fp) -ForegroundColor Red
    $ng | Select-Object -First 20 | ForEach-Object { Write-Host "   $_" }
    $bad += $ng.Count
  }
}
exit $(if ($bad) { 1 } else { 0 })

<#
.SYNOPSIS
  Split a big zip into parts small enough for Remote Desktop copy, plus a JOIN.cmd that puts it
  back together on the company PC (checks the SHA-256, unpacks to C:\, starts START.cmd).

  Usage: powershell -ExecutionPolicy Bypass -File tools\split-zip.ps1 -Zip C:\develop\zipbuild\kimeru-pc.zip [-PartMB 1000]
  Output: <zip folder>\parts\kimeru-pc.zip.001 ... and JOIN.cmd  (copy the whole parts folder)
#>
[CmdletBinding(PositionalBinding = $false)]
param([Parameter(Mandatory = $true)][string]$Zip, [int]$PartMB = 1000)
$ErrorActionPreference = 'Stop'

$src = Get-Item $Zip
$outDir = Join-Path $src.DirectoryName 'parts'
if (Test-Path $outDir) { Get-ChildItem $outDir -File | Remove-Item -Force } else { New-Item -ItemType Directory $outDir | Out-Null }

$size = [int64]$PartMB * 1MB
$buf = New-Object byte[] (4MB)
$in = [IO.File]::OpenRead($src.FullName)
$names = New-Object System.Collections.Generic.List[string]
try {
  $n = 0
  while ($in.Position -lt $in.Length) {
    $n++
    $name = '{0}.{1:D3}' -f $src.Name, $n
    $names.Add($name)
    $out = [IO.File]::Create((Join-Path $outDir $name))
    try {
      $left = $size
      while ($left -gt 0) {
        $r = $in.Read($buf, 0, [int][math]::Min($buf.Length, $left))
        if ($r -le 0) { break }
        $out.Write($buf, 0, $r); $left -= $r
      }
    } finally { $out.Dispose() }
  }
} finally { $in.Dispose() }

$hash = (Get-FileHash $src.FullName -Algorithm SHA256).Hash
$folder = [IO.Path]::GetFileNameWithoutExtension($src.Name)     # kimeru-pc
$join = @(
  '@echo off'
  'rem kimeru: join the parts, check them, unpack to C:\ and start. Double-click this file.'
  'cd /d "%~dp0"'
  ('if exist "C:\{0}" (echo C:\{0} already exists. Delete or rename it, then run JOIN.cmd again.& pause & exit /b 1)' -f $folder)
  ('copy /b {0} "{1}" >nul' -f (($names | ForEach-Object { '"' + $_ + '"' }) -join '+'), $src.Name)
  ('powershell -NoProfile -Command "if ((Get-FileHash ''{0}'' -Algorithm SHA256).Hash -ne ''{1}'') {{ Write-Host ''NG: a part is broken or missing. Copy the parts folder again.''; exit 1 }} else {{ Write-Host ''OK: all parts are intact'' }}"' -f $src.Name, $hash)
  'if errorlevel 1 (pause & exit /b 1)'
  'echo Unpacking to C:\ (about 2 minutes)...'
  ('"%SystemRoot%\System32\tar.exe" -xf "{0}" -C C:\' -f $src.Name)
  'if errorlevel 1 (echo Unpack failed.& pause & exit /b 1)'
  ('del "{0}"' -f $src.Name)
  ('call "C:\{0}\START.cmd"' -f $folder)
)
Set-Content -Path (Join-Path $outDir 'JOIN.cmd') -Value $join -Encoding ASCII
Write-Host ("parts: {0} x up to {1}MB in {2} (+ JOIN.cmd)" -f $names.Count, $PartMB, $outDir)

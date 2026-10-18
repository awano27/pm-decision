# kimeru: the try run from a double-click (try.cmd). Finds Python 3.10 or later, runs `python -m kimeru try`, opens the page.
# When no Python is found, says how to install one and stops: nothing is installed or downloaded from here.
param([switch]$NoOpen)   # -NoOpen: do not open the browser (for a check of this script)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-Python($exe, $pre) {
    try {
        & $exe @pre -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)' 2>$null | Out-Null
        return $LASTEXITCODE
    } catch {
        return -1
    }
}

$found = $null
foreach ($c in @(@{exe = 'python'; pre = @()}, @{exe = 'py'; pre = @('-3')})) {
    if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
    $code = Test-Python $c.exe $c.pre
    if ($code -eq 0) { $found = $c; break }
    if ($code -eq 3) { Write-Host "見つかった Python は 3.10 より古いので使いません（$($c.exe)）。" }
}

if (-not $found) {
    Write-Host ''
    Write-Host 'Python 3.10 以上が見つかりませんでした。kimeru の体験には Python だけが必要です。'
    Write-Host ''
    Write-Host '入れ方（どちらか 1 つ）:'
    Write-Host '  1. PowerShell で次を実行する:  winget install Python.Python.3.12'
    Write-Host '  2. https://www.python.org/downloads/ からインストーラーを入れる（最初の画面で「Add python.exe to PATH」に印を付ける）'
    Write-Host ''
    Write-Host '入れたあと、この画面を閉じて try.cmd をもう一度ダブルクリックしてください。'
    Write-Host '会社の PC で入れられない場合は docs\managed-environments.md を見てください。'
    exit 2
}

& $found.exe @($found.pre) -m kimeru try
$rc = $LASTEXITCODE
$page = Join-Path $root 'out\try\report.html'
if ($rc -eq 0 -and (Test-Path $page) -and -not $NoOpen) {
    Write-Host ''
    Write-Host 'レポートをブラウザで開きます。'
    Start-Process $page
}
exit $rc

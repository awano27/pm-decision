<#
.SYNOPSIS
  On the development PC: build the Kev folder (kev-bundle) that make-bundle.ps1 carries to a managed PC.
  One command: build-kev-bundle.cmd. Needs git and uv, about 10GB of download, and about 15GB free on the drive of -Out.
  It asks before it sends anything (a reachability check, HEAD requests only) and before it downloads.
  It writes nothing to kimeru's records or settings. -DryRun fetches nothing, sends nothing and creates nothing.

  Usage: powershell -ExecutionPolicy Bypass -File tools\build-kev-bundle.ps1 [-Out C:\develop\kev-bundle]
           [-KevRepo C:\develop\kev-local] [-Yes] [-DryRun] [-NoStartCheck]
  -Yes answers yes to every question, the start check included. A run that stopped can be started again: what is
  already there is skipped (the model download resumes).
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [string]$Out = 'C:\develop\kev-bundle',
  [string]$KevRepo = 'C:\develop\kev-local',
  [switch]$Yes,
  [switch]$DryRun,
  [switch]$NoStartCheck
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
$Out = [IO.Path]::GetFullPath($Out)
$KevRepo = [IO.Path]::GetFullPath($KevRepo)

# the eight steps: number, what it does, where it fetches from, size (estimates)
$steps = @(
  @(1, '上流の Kev を取得する（既にあれば git pull）', 'github.com', '数 MB'),
  @(2, 'Python 3.13 を uv で入れ、その単体版を python\ へコピーする', 'github.com（uv が取得する Python）', '約 0.05GB（取得）。展開後の python 全体で約 1.9GB'),
  @(3, 'torch（CPU 版）を入れる', 'download.pytorch.org', '約 0.2GB'),
  @(4, 'kev（<KevRepo>[serve]）を入れる', 'pypi.org（依存パッケージ）', '数百 MB'),
  @(5, 'モデル 2 つを取得する（再実行は再開）', 'huggingface.co', '約 9.5GB（kev-4b 約 0.2GB・土台のモデル約 9.3GB）'),
  @(6, '起動用のファイル（tools\kev-bundle\*）をコピーする', 'この PC の中', '数十 KB'),
  @(7, '完成の検査と、精度・参照先の設定（pick-dtype.py・localize.py）', 'この PC の中', 'なし'),
  @(8, '起動の確認（任意。-NoStartCheck で省略）', '127.0.0.1 だけ（この PC の中）', 'なし')
)
$targets = @(
  @{ Name = 'github.com'; Url = 'https://github.com/' },
  @{ Name = 'pypi.org'; Url = 'https://pypi.org/simple/' },
  @{ Name = 'download.pytorch.org'; Url = 'https://download.pytorch.org/whl/cpu/' },
  @{ Name = 'huggingface.co'; Url = 'https://huggingface.co/' }
)

function Show-Plan {
  Write-Host '  Kev の持ち込み用フォルダを作る手順（-DryRun: 何も取得せず、何も作りません）'
  Write-Host "  作る場所:   $Out"
  Write-Host "  上流の Kev: $KevRepo"
  foreach ($s in $steps) {
    Write-Host ("  手順 {0}  {1}" -f $s[0], $s[1])
    Write-Host ("          取得先: {0}   容量の目安: {1}" -f $s[2], $s[3])
  }
  Write-Host '  取得の合計: 約 10GB（作ったあとのディスク 約 11.4GB。作者の環境での値）。-Out のドライブに約 15GB の空きが必要'
  Write-Host '  送るもの: 取得先への到達の確認（HEAD の要求）と、上の取得だけ。本文・名前・キーは送りません'
  Write-Host '  確認: 送る前に 1 回と、取得の前に 1 回 [y/N]（-Yes で省略）'
}

if ($DryRun) {
  Show-Plan
  exit 0
}

function Say($text) { Write-Host $text }
function Fail($text) { Write-Host "NG: $text" -ForegroundColor Yellow; exit 1 }
function Show-Step($n, $title) { Write-Host ''; Write-Host "手順 $n  $title" -ForegroundColor Cyan }
function Ask($question) {
  if ($Yes) { Write-Host "$question [y/N] -> -Yes: はい"; return $true }
  return [bool]((Read-Host "$question [y/N]") -match '^\s*[yY]')
}
function KevUp { try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 3; $true } catch { $false } }

# 0) the prerequisites and the disk, checked on this PC before anything is sent
$missing = @()
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $missing += 'git（git-scm.com から入れて、開き直す）' }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { $missing += 'uv（powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" の 1 行で入れて、開き直す）' }
if ($missing.Count -gt 0) { Fail ('前提が見つかりません: ' + ($missing -join ' / ')) }

$driveRoot = [IO.Path]::GetPathRoot($Out)
try { $freeGB = [math]::Floor((New-Object IO.DriveInfo $driveRoot).AvailableFreeSpace / 1GB) }
catch { Fail "-Out のドライブを確かめられません: $Out" }
if ($freeGB -lt 15) { Fail "空きが約 15GB ありません（$driveRoot 約 ${freeGB}GB）" }

# 1) the first question: the reachability check sends HEAD requests only
if (-not (Ask '取得先（github.com・pypi.org・download.pytorch.org・huggingface.co）に届くかを確かめます。HEAD の要求だけで、本文は送りません。続けますか')) {
  Fail '中止しました（何も送っていません）'
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$unreachable = @()
foreach ($t in $targets) {
  try { $null = Invoke-WebRequest -Uri $t.Url -Method Head -UseBasicParsing -TimeoutSec 20 } catch { $unreachable += $t.Name }
}
if ($unreachable.Count -gt 0) { Fail ('届かない取得先があります: ' + ($unreachable -join '、') + '（ネットかプロキシを確かめて再実行）') }

# 2) the second question: the download itself
if (-not (Ask "約 10GB を取得して $Out に作ります。続けますか")) { Fail '中止しました（取得は始めていません）' }
New-Item -ItemType Directory -Force $Out | Out-Null
$py = Join-Path $Out 'python\python.exe'

# 3) the eight steps; a step that is already done is skipped (or resumed)
Show-Step 1 '上流の Kev を取得する'
if (Test-Path (Join-Path $KevRepo '.git')) { git -C $KevRepo pull --ff-only } else { git clone https://github.com/jaredpalmer/kev $KevRepo }
if ($LASTEXITCODE -ne 0) { Fail "手順 1: git が失敗しました（$KevRepo。ネットと、フォルダの中の変更を確かめて再実行）" }

Show-Step 2 'Python 3.13 を用意する'
if (-not (Test-Path $py)) {
  uv python install 3.13
  if ($LASTEXITCODE -ne 0) { Fail '手順 2: uv python install 3.13 が失敗しました（ネットを確かめて再実行）' }
  $found = [string](uv python find 3.13 | Select-Object -Last 1)
  $found = $found.Trim()
  if (-not $found -or -not (Test-Path $found)) { Fail '手順 2: uv が Python 3.13 の場所を返しませんでした' }
  robocopy (Split-Path -Parent $found) (Join-Path $Out 'python') /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
  if ($LASTEXITCODE -ge 8) { Fail '手順 2: Python のフォルダのコピーに失敗しました' }
}
if (-not (Test-Path $py)) { Fail '手順 2: python\python.exe がありません' }

Show-Step 3 'torch（CPU 版）を入れる'
if (Test-Path (Join-Path $Out 'python\Lib\EXTERNALLY-MANAGED')) {
  Fail '手順 3: この Python は pip を拒否する設定です（Lib\EXTERNALLY-MANAGED）。python フォルダを消して、手順 2 からやり直す'
}
& $py -c 'import torch' 2>$null
if ($LASTEXITCODE -ne 0) {
  & $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  if ($LASTEXITCODE -ne 0) { Fail '手順 3: torch（CPU 版）を入れられませんでした（download.pytorch.org を確かめて再実行）' }
}

Show-Step 4 'kev を入れる'
& $py -m pip install "${KevRepo}[serve]"
if ($LASTEXITCODE -ne 0) { Fail '手順 4: pip で kev を入れられませんでした（依存の取得を確かめて再実行）' }

Show-Step 5 'モデル 2 つを取得する（再実行は続きから）'
$fetch = "from huggingface_hub import snapshot_download as s; import sys; m = sys.argv[1]; s('jaredpalmer/kev-4b', local_dir=m + '/kev-4b'); s('Qwen/Qwen3.5-4B-Base', local_dir=m + '/Qwen3.5-4B-Base')"
& $py -c $fetch (Join-Path $Out 'models')
if ($LASTEXITCODE -ne 0) { Fail '手順 5: モデルの取得に失敗しました（同じ手順を再実行すると続きから）' }

Show-Step 6 '起動用のファイルを置く'
$bundle = Join-Path $PSScriptRoot 'kev-bundle'
try { Copy-Item -Path (Join-Path $bundle '*') -Destination $Out -Force -ErrorAction Stop }
catch { Fail "手順 6: 起動用のファイルをコピーできませんでした（$bundle）" }

Show-Step 7 '完成の検査と、起動用の設定'
$need = @('python\python.exe', 'models\kev-4b\head.pt', 'models\Qwen3.5-4B-Base\config.json', 'start-kev.cmd')
$lack = @($need | Where-Object { -not (Test-Path (Join-Path $Out $_)) })
if ($lack.Count -gt 0) { Fail ('手順 7: 足りないもの: ' + ($lack -join '、') + '（該当する手順から再実行）') }
$dtype = [string](& $py (Join-Path $Out 'pick-dtype.py') | Select-Object -Last 1)
Say ("  この CPU の精度: {0}（start-kev.cmd も同じ判定）" -f $dtype.Trim())
& $py (Join-Path $Out 'localize.py') 'kev-4b'
if ($LASTEXITCODE -ne 0) { Fail '手順 7: localize.py が失敗しました（models\kev-4b の中身を確かめる）' }

Show-Step 8 '起動の確認（この PC の中だけ。ネットは使いません）'
if ($NoStartCheck) {
  Say '  手順 8 は省略しました（-NoStartCheck）'
} elseif (Ask 'Kev を起動して、この PC の 127.0.0.1 で応答するかを確かめますか') {
  if (KevUp) {
    Say '  Kev は起動済みです（127.0.0.1:8009）'
  } else {
    Start-Process -FilePath (Join-Path $Out 'start-kev.cmd') -WorkingDirectory $Out -WindowStyle Minimized
    $deadline = (Get-Date).AddSeconds(600)
    while ((Get-Date) -lt $deadline -and -not (KevUp)) { Start-Sleep -Seconds 5; Write-Host -NoNewline '.' }
    Write-Host ''
    if (-not (KevUp)) { Fail '手順 8: 600 秒たっても応答しません（最小化された Kev のウィンドウのエラーを確かめる）' }
    Say '  Kev が 127.0.0.1:8009 で応答しました（そのウィンドウは閉じないでください）'
  }
} else {
  Say '  手順 8 は省略しました（後で start-kev.cmd を実行して確かめられます）'
}

Say ''
Say "できました: $Out"
Say '次に、kimeru と az と一緒に持ち込むフォルダを作ります（-KevSrc にこのフォルダを指定）:'
Say ('  powershell -ExecutionPolicy Bypass -File tools\make-bundle.ps1 -Zip -KevSrc "{0}" -AzSrc <az の ZIP 展開先>' -f $Out)
exit 0

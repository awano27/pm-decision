<#
.SYNOPSIS
  Check, on a managed PC, the latest changes in one go (run via run-update-check.cmd):
  - U1-U6  the demo never clears real records (own folder out\demo, stops on real records, --fresh moves aside,
           a command that writes records takes the folder back, an unreachable judge touches nothing)
  - U7     optional: the demo with the Kev running on this PC
  - U8     schedule install says the task always posts (the task is NOT registered: schtasks is replaced)
  - U9     run-check.cmd T13 asks before the reachability probe (No -> net: SKIP; no network call)
  Nothing is sent anywhere and Teams is not touched. Everything runs in a temporary folder with its own state folder,
  without the KIMERU_* settings and keys of this PC (so no writer such as Copilot is called).
  U0 confirms that out\ and %LOCALAPPDATA%\kimeru were not changed.
  The result sheet (OK/NG and counts only, no message text) goes to the clipboard and kimeru-update-check-result.txt.
#>
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$Results = [ordered]@{}
function Say($s) { Write-Host ""; Write-Host "== $s" -ForegroundColor Cyan }
function Rec($k, $v) { $Results[$k] = $v; $c = if ([string]$v -match '^OK') { 'Green' } elseif ([string]$v -match '^SKIP') { 'DarkYellow' } else { 'Red' }; Write-Host ("   {0}: {1}" -f $k, $v) -ForegroundColor $c }
function YesNo($q) { (Read-Host "$q [y/N]") -match '^\s*([yYｙＹ]|はい)' }
function Short($s) { $x = ([string]$s -replace '\s+', ' ').Trim(); if ($x.Length -gt 160) { $x.Substring(0, 160) } else { $x } }
function Hashes($folder) {
  # file name -> SHA-256 for every file under a folder (null when the folder does not exist)
  if (-not (Test-Path $folder)) { return $null }
  $base = (Get-Item -LiteralPath $folder -Force).FullName.TrimEnd('\')   # long form: TEMP may be an 8.3 short path
  $h = [ordered]@{}
  Get-ChildItem -LiteralPath $base -Recurse -File -Force -ErrorAction SilentlyContinue | Sort-Object FullName | ForEach-Object {
    $h[$_.FullName.Substring($base.Length)] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
  }
  return $h
}
function Same($a, $b) {
  if ($null -eq $a -or $null -eq $b) { return ($null -eq $a) -and ($null -eq $b) }
  if ($a.Count -ne $b.Count) { return $false }
  foreach ($k in $a.Keys) { if (-not $b.Contains($k) -or $b[$k] -ne $a[$k]) { return $false } }
  return $true
}

Write-Host "kimeru 更新の確認（送信なし・Teams 操作なし。一時フォルダだけを使います）" -ForegroundColor Green

# ---- version ----
$verFile = Join-Path $root 'VERSION'
Rec 'build' $(if (Test-Path $verFile) { (Get-Content -TotalCount 1 $verFile).Trim() } else { '不明（VERSION なし）' })
$log = Get-Content (Join-Path $root 'CHANGELOG.md') -Raw -Encoding UTF8
Rec 'version' $(if ($log -match 'デモが記録を消さない' -and $log -match '送信の確認と開示') { 'OK 今回の変更を含む版' } else { 'NG 古い版です（CHANGELOG に今回の変更がありません）。持ち込み直してください' })

# ---- Python ----
$py = $null
foreach ($c in @(@('python'), @('py', '-3'))) {
  if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
  $v = & $c[0] $c[1..9] --version 2>&1 | Out-String
  if ($v -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) { $py = $c; break }
}
$emb = Join-Path $root '.python\python.exe'
if (-not $py -and (Test-Path $emb)) { $py = @($emb) }
if (-not $py) { Rec 'python' 'NG Python 3.10 以上がありません（run-check.cmd の T6 で取得できます）' }

# ---- isolation: temp work folder, own state folder, none of this PC's kimeru settings or keys ----
$tmp = Join-Path $env:TEMP ("kimeru-update-" + (Get-Random -Minimum 10000 -Maximum 99999))
$work = Join-Path $tmp 'work'
New-Item -ItemType Directory -Force $work | Out-Null
$realOut = Join-Path $root 'out'
$realState = Join-Path $env:LOCALAPPDATA 'kimeru'
$before = @{ out = (Hashes $realOut); state = (Hashes $realState) }
Get-ChildItem env: | Where-Object { $_.Name -like 'KIMERU_*' -or $_.Name -in @('TYPESAFE_API_KEY', 'KEV_API_KEY', 'CLM_API_KEY') } |
  ForEach-Object { Remove-Item "env:$($_.Name)" }
$env:KIMERU_STATE_DIR = Join-Path $tmp 'state'
$env:KIMERU_TOAST = '0'
$env:PYTHONPATH = $root   # `-m kimeru` from the temp folder (the no-install Python reads the repo root from its ._pth)
Set-Location $work
function K([string[]]$a) {
  # run kimeru; returns @{ rc; text } (stdout and stderr together)
  $t = & $py[0] $py[1..9] -m kimeru @a 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { [string]$_ } } | Out-String
  return @{ rc = $LASTEXITCODE; text = $t }
}
$alert = Join-Path $root 'examples\monitor_alert.json'

if ($py) {
  # ---- U1: the demo uses out\demo and leaves out\ alone ----
  Say "U1 デモは out\demo に書き、out\ の記録に触れない"
  $r = K @('--backend', 'stub', '--out', 'out', 'run', $alert)
  $o1 = Hashes (Join-Path $work 'out')
  $d = K @('--backend', 'stub', 'demo', '--pace', '0')
  $o2 = Hashes (Join-Path $work 'out')
  if ($o2) { foreach ($k in @($o2.Keys)) { if ($k -like '\demo\*') { $o2.Remove($k) } } }
  $ok = $r.rc -eq 0 -and $d.rc -eq 0 -and (Test-Path (Join-Path $work 'out\demo\decisions.jsonl')) -and (Test-Path (Join-Path $work 'out\demo\report.html')) -and (Same $o1 $o2) -and $d.text -match 'out\\demo\\report\.html'
  Rec 'U1' $(if ($ok) { "OK out\demo に出力、out\ の記録 $($o1.Count) 件は不変" } else { "NG run=$($r.rc) demo=$($d.rc) out不変=$(Same $o1 $o2) " + (Short ($d.text -split "`n" | Select-Object -Last 3)) })

  # ---- U2: a second demo in its own folder works ----
  Say "U2 2 回目のデモは前回のデモ分だけを消して動く"
  $d2 = K @('--backend', 'stub', 'demo', '--pace', '0')
  Rec 'U2' $(if ($d2.rc -eq 0) { 'OK' } else { "NG rc=$($d2.rc) " + (Short $d2.text) })

  # ---- U3: a folder with real records stops the demo, untouched ----
  Say "U3 デモ以外の記録があるフォルダでは、消さずに止まる"
  $h1 = Hashes (Join-Path $work 'out')
  $d3 = K @('--backend', 'stub', '--out', 'out', 'demo', '--pace', '0')
  $h2 = Hashes (Join-Path $work 'out')
  $ok = $d3.rc -eq 2 -and $d3.text -match '消さずに止めました' -and $d3.text -notmatch 'Traceback' -and (Same $h1 $h2)
  Rec 'U3' $(if ($ok) { 'OK 終了コード 2、ファイル不変' } else { "NG rc=$($d3.rc) 不変=$(Same $h1 $h2) " + (Short $d3.text) })

  # ---- U4: --fresh moves the records aside, deletes nothing ----
  Say "U4 --fresh は記録を before-demo-<日時> へ移す（消さない）"
  $rec = Join-Path $work 'rec'
  $null = K @('--backend', 'stub', '--out', $rec, 'run', $alert)
  $h1 = Hashes $rec
  $d4 = K @('--backend', 'stub', '--out', $rec, 'demo', '--pace', '0', '--fresh')
  $kept = Get-ChildItem $rec -Directory -Filter 'before-demo-*' -ErrorAction SilentlyContinue | Select-Object -First 1
  $moved = if ($kept) { Hashes $kept.FullName } else { $null }
  $ok = $d4.rc -eq 0 -and $kept -and $moved -and $moved.Count -eq $h1.Count
  if ($ok) { foreach ($k in $h1.Keys) { if ($moved[$k] -ne $h1[$k]) { $ok = $false } } }
  Rec 'U4' $(if ($ok) { "OK $($h1.Count) 件を移動、中身は不変" } else { "NG rc=$($d4.rc) 退避フォルダ=$([bool]$kept) " + (Short $d4.text) })

  # ---- U5: a command that writes records takes the demo folder back ----
  Say "U5 デモのフォルダに run で書くと、次のデモはそのフォルダを消さない"
  $dd = Join-Path $work 'out\demo'
  $mark1 = Test-Path (Join-Path $dd '.kimeru-demo')
  $null = K @('--backend', 'stub', '--out', $dd, 'run', $alert)
  $mark2 = Test-Path (Join-Path $dd '.kimeru-demo')
  $h1 = Hashes $dd
  $d5 = K @('--backend', 'stub', 'demo', '--pace', '0')
  $ok = $mark1 -and -not $mark2 -and $d5.rc -eq 2 -and (Same $h1 (Hashes $dd))
  Rec 'U5' $(if ($ok) { 'OK 印が外れ、次のデモは止まった' } else { "NG 印(前)=$mark1 印(後)=$mark2 rc=$($d5.rc)" })

  # ---- U6: an unreachable judge touches nothing ----
  Say "U6 判断モデルに接続できないとき、1 行で止まり何も作らない"
  $env:KIMERU_KEV_URL = 'http://127.0.0.1:1/v1'   # a closed port on this PC (the real Kev, if running, is not used)
  $nf = Join-Path $work 'kevtest'
  $d6 = K @('--backend', 'kev', '--out', $nf, 'demo', '--pace', '0')
  $h1 = Hashes $rec
  $d6b = K @('--backend', 'kev', '--out', $rec, 'demo', '--pace', '0', '--fresh')
  Remove-Item env:KIMERU_KEV_URL
  $ok = $d6.rc -eq 1 -and $d6.text -match '判断モデルに接続できません' -and $d6.text -notmatch 'Traceback' -and -not (Test-Path $nf) -and $d6b.rc -eq 1 -and (Same $h1 (Hashes $rec))
  Rec 'U6' $(if ($ok) { 'OK 1 行で終了、フォルダも作らず、既存の記録も不変' } else { "NG rc=$($d6.rc)/$($d6b.rc) 作成=$(Test-Path $nf) " + (Short $d6.text) })

  # ---- U7 (optional): the demo with the Kev on this PC ----
  Say "U7 （任意）この PC の Kev でデモ"
  $up = $false
  try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 5; $up = $true } catch {}
  if (-not $up) { Rec 'U7' 'SKIP Kev 未起動（START.cmd か kev\start-kev.cmd で起動してから再実行すると確かめられます）' }
  elseif (-not (YesNo "   Kev でデモを 1 回動かします（PC の中だけ。数分かかることがあります）。よろしいですか")) { Rec 'U7' 'SKIP' }
  else {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $d7 = K @('--backend', 'kev', '--out', (Join-Path $work 'kevdemo'), 'demo', '--pace', '0')
    $sec = [math]::Round($sw.Elapsed.TotalSeconds)
    $ok = $d7.rc -eq 0 -and $d7.text -match '判断: Kev'
    Rec 'U7' $(if ($ok) { "OK Kev で 9 件を判断（${sec} 秒）" } else { "NG rc=$($d7.rc) " + (Short ($d7.text -split "`n" | Select-Object -Last 3)) })
  }

  # ---- U8: schedule install says it posts (schtasks is replaced: nothing is registered) ----
  Say "U8 自動運転の登録時に「実際に投稿する」と表示する（登録はしません）"
  $prog = Join-Path $tmp 'u8.py'
  @(
    'import subprocess, sys, types',
    'from unittest import mock',
    'from kimeru import cli',
    'with mock.patch.object(subprocess, ''run'', return_value=types.SimpleNamespace(returncode=0)) as run:',
    '    rc = cli.main([''--backend'', ''stub'', ''--out'', sys.argv[1], ''schedule'', ''install''])',
    'calls = [c.args[0][0] for c in run.call_args_list]',
    'print(''SCHTASKS_CALLS='' + str(calls))',
    'print(''RC='' + str(rc))'
  ) | Set-Content -Path $prog -Encoding UTF8
  $u8 = & $py[0] $py[1..9] $prog (Join-Path $work 'sched') 2>&1 | Out-String
  $ok = $u8 -match 'RC=0' -and $u8 -match '自分とのチャットへ実際に投稿します' -and $u8 -match 'schedule remove' -and $u8 -match 'daily --once --send'
  Rec 'U8' $(if ($ok) { 'OK 投稿すること・止め方を表示（タスクは登録していない）' } else { 'NG ' + (Short $u8) })
} else {
  foreach ($k in 'U1', 'U2', 'U3', 'U4', 'U5', 'U6', 'U7', 'U8') { Rec $k 'SKIP Python なし' }
}

# ---- U9: run-check.cmd T13 asks first; No -> net: SKIP with no network call ----
Say "U9 run-check.cmd の T13 は外部への到達確認の前に確認する（N なら送らない）"
try {
  $src = Get-Content (Join-Path $PSScriptRoot 'check.ps1') -Raw -Encoding UTF8
  $i = $src.IndexOf('# ---- T13:'); $j = $src.IndexOf('# ---- T14:')
  $block = $src.Substring($i, $j - $i)
  $script:asked = $false; $script:netCalled = $false
  $stub = @'
$Results = [ordered]@{}
function Want($t) { $true }
function Say($s) { }
function Rec($k, $v) { $Results[$k] = $v }
function YesNo($q) { $script:asked = $true; $false }
function Invoke-WebRequest { $script:netCalled = $true; throw 'blocked in this check' }
'@
  $sb = [scriptblock]::Create($stub + "`n" + $block + "`n" + '$Results')
  $res = & $sb
  $line = [string]$res['T13']
  $ok = $script:asked -and -not $script:netCalled -and $line -match 'net: SKIP$' -and $line -match 'RAM='
  Rec 'U9' $(if ($ok) { "OK 確認あり・N で送信なし（$line）" } else { "NG 確認=$($script:asked) 通信=$($script:netCalled) $line" })
} catch { Rec 'U9' ('SKIP この環境では部分実行できません: ' + (Short $_.Exception.Message) + '。.\run-check.cmd T13 で直接確かめてください') }

# ---- U0: nothing real was changed ----
Set-Location $root
Say "U0 この PC の記録（out\ と %LOCALAPPDATA%\kimeru）が変わっていない"
$after = @{ out = (Hashes $realOut); state = (Hashes $realState) }
$ok = (Same $before.out $after.out) -and (Same $before.state $after.state)
Rec 'U0' $(if ($ok) { 'OK 変化なし' } else { 'NG 変化あり（不変=True）: out=' + (Same $before.out $after.out) + ' state=' + (Same $before.state $after.state) + '。自動運転（daily）が動いていた場合は、止めてから再実行してください' })

# ---- result ----
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
$ng = @($Results.GetEnumerator() | Where-Object { [string]$_.Value -match '^NG' }).Count
$sheet = @("kimeru 更新の確認結果 $(Get-Date -Format 'yyyy-MM-dd HH:mm')  NG=$ng") + ($Results.GetEnumerator() | ForEach-Object { "{0,-8} {1}" -f $_.Key, $_.Value })
$sheet | Out-File -Encoding utf8 (Join-Path $root 'kimeru-update-check-result.txt')
try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'kimeru-update-check-result.txt を開いてコピーしてください' }
Say "結果（$clip）"
$sheet | ForEach-Object { Write-Host $_ }

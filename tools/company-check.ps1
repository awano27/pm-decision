<#
.SYNOPSIS
  One-shot company PC check for kimeru (run via run-company-check.cmd).
  Runs every level of docs/company-pc-test.md automatically, asks only when a human
  is required (sending to your own self chat, iPhone replies, opening a screen, az / Jev),
  and writes a result sheet (no message text, names or tokens) to the clipboard and
  kimeru-check-result.txt.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [int]$WaitSec = 180,
  # run-company-check.cmd T9  -> only T9 (plus the steps it depends on: T3, T6, T8)
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$Only = @()
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$Results = [ordered]@{}   # not $R: PowerShell names are case-insensitive ($r is used below)
$tmp = Join-Path $env:TEMP ("kimeru-check-" + (Get-Random -Minimum 10000 -Maximum 99999))
New-Item -ItemType Directory -Force $tmp | Out-Null

if ($Only -contains 'monday') { $Only = @('T2', 'T9', 'T12', 'T13', 'T14') }   # short Monday session (T3/T6/T8 run anyway)
function Want($t) { -not $Only -or $Only -contains $t }
function Fails($s) {
  # prefer our one-line "... failed: ..." message, else the last traceback line
  # PowerShell 5.1 prefixes native stderr with "python.exe : " and adds "+ CategoryInfo" noise lines
  $lines = @(([string]$s -split "`n") | ForEach-Object { ($_ -replace '^\S+\.exe : ', '').Trim() } |
    Where-Object { $_ -and $_ -notmatch '^(\+ |At .*char:|発生場所)' })
  $f = $lines | Where-Object { $_ -match 'failed: ' } | Select-Object -Last 1
  if (-not $f) { $f = $lines | Where-Object { $_ -match '^\w+(Error|Exception)\b' } | Select-Object -Last 1 }
  if ($f) { Short ($f -replace '^.*?(\w+ failed: )', '$1') } else { Short $s }
}
function Say($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Rec($k, $v) { $Results[$k] = $v; Write-Host ("   {0}: {1}" -f $k, $v) -ForegroundColor Yellow }
function Short($s) { $x = ([string]$s -replace '\s+', ' ').Trim(); if ($x.Length -gt 160) { $x.Substring(0, 160) } else { $x } }
function YesNo($q) { (Read-Host "$q [y/N]") -match '^\s*([yYｙＹ]|はい)' }   # tolerate stray keys after y ("y[")
function Self($action, [string[]]$extra = @()) {
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action $action @extra 2>&1 | Out-String
  try { return ($out.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json) } catch { return [pscustomobject]@{ ok = $false; error = (Short $out) } }
}
function Probe($label, $keepAs = $null) {
  $o = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'probe-teams.ps1') -Label $label 2>&1 | Out-String
  $f = Join-Path $root "probe-$label.txt"
  if (-not (Test-Path $f)) { return @{ ok = $false; error = (Short $o) } }
  $lines = Get-Content $f -Encoding UTF8
  if ($keepAs) {
    # keep the structure for building the recap parser; mask every control name except known UI words
    $ui = 'Copilot|要約|まとめ|Recap|Summary|アクション|Action|決定|Decision|メモ|Notes|トランスクリプト|Transcript|チャット|Chat|ファイル|Files|詳細|Details|表示|Show|More|その他'
    $masked = foreach ($l in $lines) {
      if ($l -match '^(\s*)(Button|TabItem|MenuItem|ToolBar|Hyperlink|  Button|  TabItem|  MenuItem|  ToolBar|  Hyperlink)(?:\s*\|)?\s(.+)$' -and $Matches[3] -notmatch '^len=') {
        $pre = $l.Substring(0, $l.Length - $Matches[3].Length); $name = $Matches[3]
        $pre + [regex]::Replace($name, '[\p{L}\p{N}]+', { param($m) if ($m.Value -match "^($ui)$") { $m.Value } else { 'x' } })
      } else { $l }
    }
    $masked | Out-File -Encoding utf8 (Join-Path $root $keepAs)
  }
  $n = if ($lines[0] -match 'elements=(\d+)') { [int]$Matches[1] } else { 0 }
  $i = [array]::IndexOf($lines, 'copilot/recap-like UI labels:')
  $labels = 0
  if ($i -ge 0) { for ($j = $i + 1; $j -lt $lines.Count -and $lines[$j].StartsWith('  '); $j++) { $labels++ } }
  $types = ($lines | Where-Object { $_ -like 'types:*' } | Select-Object -First 1)
  Remove-Item $f -ErrorAction SilentlyContinue   # may contain names; only counts are kept
  return @{ ok = ($n -ge 100); elements = $n; labels = $labels; types = (Short $types) }
}
$script:PostSeen = $false
function WaitReply($id, $word) {
  $deadline = (Get-Date).AddSeconds($WaitSec)
  while ((Get-Date) -lt $deadline) {
    $r = Self 'read'
    if ($r.ok -and $r.timeline) {
      $tl = @($r.timeline); $last = -1
      for ($i = 0; $i -lt $tl.Count; $i++) { if ($tl[$i] -eq "P:$id") { $last = $i; $script:PostSeen = $true } }
      for ($i = $last + 1; $last -ge 0 -and $i -lt $tl.Count; $i++) { if ($tl[$i] -eq "R:$word $id") { return $true } }
    }
    Start-Sleep -Seconds 10
  }
  return $false
}

Write-Host "kimeru 会社PCチェック（外部送信の前には必ず確認します。Ctrl+C でいつでも中止できます）" -ForegroundColor Green

# ---- Level 0: PowerShell only ----
Say "T0 環境"
$lang = [string]$ExecutionContext.SessionState.LanguageMode
$teamsVer = (Get-AppxPackage -Name MSTeams -ErrorAction SilentlyContinue).Version
Rec 'T0' ("PS={0} LanguageMode={1} Teams={2}" -f $PSVersionTable.PSVersion, $lang, ($(if ($teamsVer) { $teamsVer } else { 'new Teams なし' })))
$uia = $lang -eq 'FullLanguage'
if (-not $uia) { Rec 'T1-T5' 'SKIP（ConstrainedLanguage: 組織ポリシーで画面操作不可）' }

$selfOk = $false
if ($uia) {
  Say "Teams を起動"
  if (-not (Self 'status').teams) {
    Start-Process 'explorer.exe' 'shell:AppsFolder\MSTeams_8wekyb3d8bbwe!MSTeams'
    for ($i = 0; $i -lt 12 -and -not (Self 'status').teams; $i++) { Start-Sleep -Seconds 5 }
    Start-Sleep -Seconds 10   # let the chat list render
  }

  if (Want 'T1') {
    Say "T1 チャット画面の読み取り"
    $p = Probe 'chat'
    Rec 'T1' $(if ($p.ok) { "OK elements=$($p.elements) $($p.types)" } else { "NG elements=$($p.elements) $($p.error)" })
  }

  if (Want 'T2') { Say "T2 Copilot 会議まとめ画面の読み取り"
  Write-Host "   Teams で Copilot のまとめ（要約）タブを表示してから Enter。無ければ s + Enter でスキップ" }
  if ((Want 'T2') -and (Read-Host) -ne 's') {
    $p = Probe 'recap' 'kimeru-recap-structure.txt'
    Rec 'T2' $(if ($p.ok) { "OK elements=$($p.elements) recap系ラベル=$($p.labels) 構造=kimeru-recap-structure.txt（名前は伏せ字。中身を確認してから共有）" } else { "NG elements=$($p.elements) $($p.error)" })
  } elseif (Want 'T2') { Rec 'T2' 'SKIP' }

  Say "T3 自分とのチャット"
  $s = Self 'open'; $r = Self 'read'
  $selfOk = [bool]($s.ok -and $r.ok)
  Rec 'T3' $(if ($selfOk) { "OK open/read（timeline=$(@($r.timeline).Count)件）" } else { "NG $($s.error) $($r.error)" })
  if (-not $selfOk) {
    # names are masked by diag; this tells us how the work account labels the self chat
    $d = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action diag 2>&1 | Out-String
    Rec 'T3-diag' (($d -replace '\s+', ' ').Trim())
    Write-Host "   自分とのチャットを自動で見つけられませんでした。" -ForegroundColor Green
    Write-Host "   Teams で「自分とのチャット」を手で開いてください（最大 90 秒、開いたら自動で検出します。Ctrl+C で中止）" -ForegroundColor Green
    $l = $null; $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
      $l = Self 'learn'   # remembers your display name locally (%LOCALAPPDATA%\kimeru); not shown here
      if ($l.ok) { Write-Host "   検出しました"; break }
      Start-Sleep -Seconds 3
    }
    if ($l) {
      $s = Self 'open'; $r = Self 'read'
      $selfOk = [bool]($l.ok -and $s.ok -and $r.ok)
      Rec 'T3-learn' $(if ($selfOk) { "OK 表示名を学習（一覧で発見=$($l.itemFound)）" } else { "NG $($l.error) $($s.error) $($r.error)" })
    }
  }

  if ($selfOk -and (Want 'T5')) {
    Say "T4/T5 貼り付け・送信・iPhone 返信"
    if (YesNo "   自分とのチャットにテストメッセージを1通送信します（宛先は自分だけ）。よろしいですか") {
      $n = Get-Random -Minimum 100 -Maximum 999
      Write-Host "   数秒間マウス・キーボードに触らないでください"
      $p1 = Self 'post' @('-Text', "[kimeru #$n] 会社PCテスト\n返信: OK $n")
      $s1 = if ($p1.ok -and $p1.typed) { Self 'send' } else { $p1 }
      if ($s1.ok) {
        Write-Host "   会社の iPhone の Teams で、自分とのチャットに「OK $n」と返信してください（全角でも可。最大 $WaitSec 秒待ちます）" -ForegroundColor Green
        Write-Host "   ※ 自分宛てのメッセージなので iPhone に通知は来ないことがあります。Teams アプリで自分とのチャットを開いてください" 
        $got = WaitReply $n 'OK'
        $push = Read-Host "   iPhone に通知は届きましたか？ [y/n]"
        Rec 'T4' ("OK 送信方法=" + $s1.via)
        $replied = if ($got) { 'y' } else { Read-Host "   iPhone から返信しましたか？ [y/n]" }
        Rec 'T5' ("送信=OK 投稿を画面で確認={0} iPhone通知={1} 返信した={2} 返信読取={3}" -f $(if ($script:PostSeen) { 'あり' } else { 'なし' }), $(if ($push -match '^\s*[yY]') { 'あり' } else { 'なし' }), $(if ($replied -match '^\s*[yY]') { 'はい' } else { 'いいえ' }), $(if ($got) { 'OK' } else { 'NG(タイムアウト)' }))
        $selfOk = $got
      } else { Rec 'T4' "NG $($p1.error)"; Rec 'T5' "NG $($s1.error)"; $selfOk = $false }
    } else { Rec 'T4' 'SKIP'; Rec 'T5' 'SKIP'; $selfOk = $false }
  }
}

# ---- Level 1: Python ----
Say "T6 Python"
$py = $null
foreach ($c in @(@('python'), @('py', '-3'))) {
  if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
  $v = & $c[0] $c[1..9] --version 2>&1 | Out-String
  if ($v -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) { $py = $c; break }
}
$emb = Join-Path $root '.python\python.exe'
if (-not $py -and (Test-Path $emb)) { $py = @($emb); $v = & $emb --version 2>&1 | Out-String }
if (-not $py) {
  Write-Host "   Python 3.10 以上が見つかりません（'python' が Microsoft Store を開くだけの状態を含む）"
  if (YesNo "   python.org からインストール不要版 Python 3.12.10（約 11MB の zip、展開するだけ・管理者権限不要）を取得して、このフォルダの .python に置きますか") {
    $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
    $zip = Join-Path $tmp 'python-embed.zip'
    $dst = Join-Path $root '.python'
    try {
      [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
      Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
      Expand-Archive $zip $dst -Force
      $sig = Get-AuthenticodeSignature $emb
      if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
        Remove-Item -Recurse -Force $dst
        throw "python.exe の署名を確認できないため削除しました: $($sig.Status)"
      }
      # embeddable Python ignores PYTHONPATH and the current folder; add the repo root to its ._pth
      $pth = Get-ChildItem $dst -Filter 'python*._pth' | Select-Object -First 1
      Add-Content -Path $pth.FullName -Value '..' -Encoding ASCII
      $py = @($emb); $v = & $emb --version 2>&1 | Out-String
      Rec 'T6-dl' 'OK python.org インストール不要版を取得（署名: Python Software Foundation）'
    } catch { Rec 'T6-dl' ('NG ' + (Short $_.Exception.Message)) }
  }
}
Rec 'T6' $(if ($py) { (Short $v) + " ($($py -join ' '))" } else { 'NG Python 3.10+ なし（Level 1 以降は SKIP）' })
function Py([string[]]$a) { & $py[0] $py[1..9] @a 2>&1 | Out-String }

if ($py) {
  if (Want 'T7') {
  Say "T7 テストと検証"
  $t = Py @('-m', 'unittest', '-q'); $tOk = $LASTEXITCODE -eq 0
  $v = Py @('-m', 'kimeru', 'validate'); $vOk = $LASTEXITCODE -eq 0
  Rec 'T7' $(if ($tOk -and $vOk) { "OK " + (Short (($t -split "`n") | Where-Object { $_ -match '^Ran ' })) } else { "NG " + (Short (($t + $v) -split "`n" | Select-Object -Last 8)) })

  }

  Say "T8 サンプルで判断と朝のまとめ"
  $out = Join-Path $tmp 'out'
  $ex = @('examples\teams_chat.json', 'examples\monitor_alert.json', 'examples\ado_workitem.json', 'examples\meeting_minutes.json')
  $o = Py (@('-m', 'kimeru', '--out', $out, 'run') + $ex); $rOk = $LASTEXITCODE -eq 0
  $b = Py @('-m', 'kimeru', '--out', $out, 'brief')
  $nDec = ([regex]::Matches($o, '(?m)^\[')).Count
  Rec 'T8' $(if ($rOk -and $b -match 'kimeru brief') { "OK 判断=$nDec plan=$(([regex]::Matches($o, 'plan:')).Count)" } else { "NG " + (Short $o) })

  if (Want 'T9') { Say "T9 承認の流れ（iPhone から OK / NG）" }
  if (-not (Want 'T9')) { }
  elseif ($selfOk -and (YesNo "   確認待ち2件を自分とのチャットに送信します（宛先は自分だけ）。よろしいですか")) {
    $base = Get-Random -Minimum 100 -Maximum 899
    [IO.File]::WriteAllText((Join-Path $out 'approvals.json'), "{`"next`": $base, `"items`": {}}")
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out, 'notify', '--send')
    Rec 'T9-notify' $(if ($nt -match 'posted: \[') { 'OK ' + (Short (($nt -split "`n") | Where-Object { $_ -match 'posted:' })) } else { 'NG ' + (Fails $nt) })
    Write-Host ("   iPhone から「OK {0}」と「NG {1}」を別々に返信してください（最大 {2} 秒）" -f $base, ($base + 1), $WaitSec) -ForegroundColor Green
    $acc = ''; $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline -and -not ($acc -match 'approved' -and $acc -match 'rejected')) {
      Start-Sleep -Seconds 10
      $acc += Py @('-m', 'kimeru', '--out', $out, 'approvals')
    }
    $again = Py @('-m', 'kimeru', '--out', $out, 'approvals')
    $ok9 = $acc -match 'approved' -and $acc -match 'rejected'
    $shown = if ($acc -match 'failed: |Traceback|Error') { Fails $acc } else { Short $acc }
    Rec 'T9' ("{0} approvals={1} 2回目={2}" -f $(if ($ok9) { 'OK' } else { 'NG' }), $shown, $(if (-not $again.Trim()) { '（なし＝二重処理なし）' } elseif ($again -match 'failed: |Traceback') { Fails $again } else { Short $again }))
  } else { Rec 'T9' 'SKIP' }
}

# ---- Level 2: Azure CLI ----
if (Want 'T10') { Say "T10 Azure CLI で取り込み" }
$az = Get-Command az -ErrorAction SilentlyContinue
if (-not (Want 'T10')) { }
elseif (-not $az -or -not $py) { Rec 'T10' $(if (-not $az) { 'SKIP az なし' } else { 'SKIP Python なし' }) }
else {
  & az account show -o none 2>$null
  if ($LASTEXITCODE -ne 0 -and (YesNo "   az にサインインしていません。az login を実行しますか（ブラウザが開きます）")) { & az login -o none 2>&1 | Out-Null }
  & az account show -o none 2>$null
  if ($LASTEXITCODE -ne 0) { Rec 'T10' 'NG az login できず（条件付きアクセス等）' }
  else {
    $inbox = Join-Path $tmp 'inbox'; $res = @()
    $org = Read-Host "   ADO の組織名（dev.azure.com/<ここ>。Enter でスキップ）"
    if ($org) {
      $proj = Read-Host "   ADO のプロジェクト名"
      $a = Py @('-m', 'kimeru', '--out', $out, 'pull', 'ado', '--org', $org, '--project', $proj, '--inbox', $inbox)
      $res += "ado=" + (Short $a)
    }
    $sub = (& az account show --query id -o tsv 2>$null)
    if ($sub -and (YesNo "   現在のサブスクリプションの発報中アラートを読み取りますか（読み取りのみ）")) {
      $a = Py @('-m', 'kimeru', '--out', $out, 'pull', 'alerts', '--subscription', $sub, '--inbox', $inbox)
      $res += "alerts=" + (Short $a)
    }
    if (Test-Path $inbox) {
      $w = Py @('-m', 'kimeru', '--out', $out, 'watch', $inbox, '--once')
      $res += "判断=" + ([regex]::Matches($w, '(?m)^\[')).Count
    }
    Rec 'T10' ('az=ログイン済み / ' + $(if ($res) { $res -join ' / ' } else { '取り込み対象の指定なし' }))
  }
}

# ---- Level 3: Jev ----
if (Want 'T11') { Say "T11 Jev で判断（架空のサンプルのみ送信）" }
# Only when a key is already in the environment: the key is never asked for or stored here
if (-not (Want 'T11')) { }
elseif (-not $env:TYPESAFE_API_KEY) { Rec 'T11' 'SKIP（TYPESAFE_API_KEY 未設定。Jev は個人 PC で確認済み）' }
elseif ($py -and (YesNo "   Jev（社外クラウド）に架空のサンプル1件を送信して判断させますか")) {
  $j = Py @('-m', 'kimeru', '--backend', 'jev', '--out', (Join-Path $tmp 'jev'), 'run', 'examples\teams_chat.json')
  Rec 'T11' $(if ($j -match 'plan:') { 'OK ' + (Short (($j -split "`n") | Where-Object { $_ -match 'path:' } | Select-Object -First 1)) } else { 'NG ' + (Short ($j -replace '[A-Za-z0-9_\-]{24,}', '<redacted>')) })
} else { Rec 'T11' 'SKIP' }

# ---- T12: Teams chat list for `kimeru pull teams` (read-only, counts only) ----
if ($uia -and (Want 'T12')) {
  Say "T12 チャット一覧の読み取り（pull teams の試運転・読み取りのみ）"
  $c = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action chats 2>&1 | Out-String
  try {
    $cs = @(($c.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json).chats)
    $kinds = ($cs | Group-Object kind | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ' '
    $withText = @($cs | Where-Object { $_.title -and $_.preview }).Count
    Rec 'T12' ("OK chats={0} [{1}] title+preview={2} unread={3} mention={4}" -f $cs.Count, $kinds, $withText,
      @($cs | Where-Object unread).Count, @($cs | Where-Object mention).Count)
  } catch { Rec 'T12' ('NG ' + (Short $c)) }
}

# ---- T13: can Kev (local, Jev-compatible model) run here? (no downloads) ----
if (Want 'T13') {
  Say "T13 ローカル判断モデル（kev）の事前チェック（ダウンロードなし）"
  $cs = Get-CimInstance Win32_ComputerSystem; $cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1)
  $free = [math]::Round((Get-PSDrive C).Free / 1GB)
  $vc = Test-Path "$env:SystemRoot\System32\vcruntime140.dll"
  $net = foreach ($u in 'https://pypi.org/simple/', 'https://github.com', 'https://huggingface.co') {
    try { $null = Invoke-WebRequest $u -Method Head -UseBasicParsing -TimeoutSec 8; ($u -replace 'https://|/.*$', '') + '=OK' }
    catch { ($u -replace 'https://|/.*$', '') + '=NG' }
  }
  $lp = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction SilentlyContinue).LongPathsEnabled
  Rec 'T13' ("RAM={0}GB CPU={1}C/{2}T free={3}GB VCruntime={4} LongPaths={5} net: {6}" -f
    [math]::Round($cs.TotalPhysicalMemory / 1GB), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors, $free,
    $(if ($vc) { 'あり' } else { 'なし' }), $(if ($lp -eq 1) { 'on' } else { 'off' }), ($net -join ' '))
}

# ---- T14: local Kev server (start-kev.cmd from the kev bundle) ----
if ($py -and (Want 'T14')) {
  Say "T14 ローカル判断モデル（kev）で判断"
  $up = $false
  try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 5; $up = $true } catch {}
  if (-not $up) { Rec 'T14' 'SKIP（kev 未起動: C:\kev\start-kev.cmd を先に実行）' }
  else {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $k = Py @('-m', 'kimeru', '--backend', 'kev', '--out', (Join-Path $tmp 'kev'), 'run', 'examples\teams_chat.json')
    $sw.Stop()
    Rec 'T14' $(if ($k -match 'plan:|path:') { ("OK {0}s " -f [math]::Round($sw.Elapsed.TotalSeconds)) + (Short (($k -split "`n") | Where-Object { $_ -match 'path:' } | Select-Object -First 1)) } else { 'NG ' + (Fails $k) })
  }
}

# ---- Result ----
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
$sheet = @("kimeru 会社PCチェック結果 $(Get-Date -Format 'yyyy-MM-dd HH:mm')") + ($Results.GetEnumerator() | ForEach-Object { "{0,-6} {1}" -f $_.Key, $_.Value })
$sheet | Out-File -Encoding utf8 (Join-Path $root 'kimeru-check-result.txt')
try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'kimeru-check-result.txt を開いてコピーしてください' }
Say "結果（$clip）"
$sheet | ForEach-Object { Write-Host $_ }

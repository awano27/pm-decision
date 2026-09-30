<#
.SYNOPSIS
  One-shot managed PC check for kimeru (run via run-check.cmd).
  Runs every level of docs/managed-pc-check.md automatically, asks only when a human
  is required (sending to your own self chat, iPhone replies, opening a screen, az / Jev),
  and writes a result sheet (no message text, names or tokens) to the clipboard and
  kimeru-check-result.txt.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [int]$WaitSec = 180,
  # run-check.cmd T9  -> only T9 (plus the steps it depends on: T3, T6, T8)
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

if ($Only -contains 'V1') { $Only = @('T9', 'T15', 'T16', 'T19', 'T20') }   # v1.0 set: approval flow, Copilot CLI, toast, Copilot paste test, ask-back / paste-back replies
if ($Only -contains 'monday') { $Only = @('T9', 'T10', 'T12', 'T13', 'T14', 'T15', 'T16', 'T17') }   # short Monday session (T3/T6/T8 run anyway); T2 dropped: minutes come as .txt (docs/minutes-format.md)
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
function Safe-Error($raw) {
  # teams-self.ps1 errors are kimeru's own fixed messages; anything else (screen text) is not recorded
  try { $j = $raw.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json; if ($j.error) { return 'error=' + (Short $j.error) } } catch {}
  "出力を解析できず（$(([string]$raw).Length) 文字、内容は記録しない）"
}
function Safe-Diag($raw) {
  # whitelisted fields only: no chat titles, tab names or name-like markers on the result sheet
  try {
    $j = $raw.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json
    if ($j.error) { return 'error=' + (Short $j.error) }
    $marks = @($j.parenMarkers.PSObject.Properties).Count
    $shape = ([string]$j.titleShape) -replace '\((?!(あなた|自分|You|Me)\))[^)]*\)', '(x)'
    # the focus line holds numbers and a control type name only (Test-TeamsInUse decides from it): what T24 needs on a real PC
    $focus = ([string]$j.focus) -replace '[^A-Za-z0-9=/. _]', ''
    return "focus=[$focus] teamsInUse=$($j.teamsInUse) selfItemFound=$($j.selfItemFound) learnedName=$($j.learnedName) treeItems=$($j.treeItems) listItems=$($j.listItems) 括弧表記の種類=$marks title=$shape"
  } catch { return "診断出力を解析できず（$(([string]$raw).Length) 文字、内容は記録しない）" }
}
function Rec($k, $v) { $Results[$k] = $v; Write-Host ("   {0}: {1}" -f $k, $v) -ForegroundColor Yellow }
function Short($s) { $x = ([string]$s -replace '\s+', ' ').Trim(); if ($x.Length -gt 160) { $x.Substring(0, 160) } else { $x } }
function DiagText {
  # the content-free layout dump the Copilot script writes when it stops (types, masked ids, name lengths, rectangles)
  $f = Join-Path $(if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }) 'copilot-diag.txt'
  if (-not (Test-Path $f)) { return '（診断ファイルなし）' }
  $t = (Get-Content -Raw -Encoding UTF8 $f) -replace '\s*\r?\n', ' / '
  if ($t.Length -gt 2600) { $t = $t.Substring(0, 2600) + '…' }
  "$f :: $t"
}
function YesNo($q) { (Read-Host "$q [y/N]") -match '^\s*([yYｙＹ]|はい)' }   # tolerate stray keys after y ("y[")
function Self($action, [string[]]$extra = @()) {
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action $action @extra 2>&1 | Out-String
  # the script's own output is JSON with fixed messages; anything else (screen text, a stack trace) is never recorded: only its length
  try { return ($out.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json) } catch { return [pscustomobject]@{ ok = $false; error = "出力を解析できず（$($out.Length) 文字、内容は記録しない）" } }
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

Write-Host "kimeru 管理PCチェック（外部送信の前には必ず確認します。Ctrl+C でいつでも中止できます）" -ForegroundColor Green

# ---- Level 0: PowerShell only ----
Say "T0 環境"
$lang = [string]$ExecutionContext.SessionState.LanguageMode
$teamsVer = (Get-AppxPackage -Name MSTeams -ErrorAction SilentlyContinue).Version
$verFile = Join-Path $root 'VERSION'
$build = if (Test-Path $verFile) { (Get-Content -TotalCount 1 $verFile).Trim() } else { '不明（VERSION なし: 古い版の可能性）' }
Rec 'build' $build
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
    Rec 'T3-diag' (Safe-Diag $d)
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
      $p1 = Self 'post' @('-Text', "[kimeru #$n] 管理PCテスト\n返信: OK $n")
      $s1 = if ($p1.ok -and $p1.typed) { Self 'send' } else { $p1 }
      if ($s1.ok) {
        Write-Host "   仕事用の iPhone の Teams で、自分とのチャットに「OK $n」と返信してください（全角でも可。最大 $WaitSec 秒待ちます）" -ForegroundColor Green
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

# ---- T23: how long is the preview Teams gives? (read only; lengths only, never text) ----
if ($uia -and $selfOk -and (Want 'T23')) {
  Say "T23 チャット一覧のプレビューの長さ（読み取りだけ。文字数の分布だけを記録します）"
  $c = Self 'chats'
  if (-not $c.ok) { Rec 'T23' ('NG ' + $c.error) }
  else {
    $pv = @($c.chats | Where-Object { $_.preview } | ForEach-Object { [string]$_.preview })
    if (-not $pv.Count) { Rec 'T23' 'SKIP プレビューのあるチャットがありません' }
    else {
      $len = @($pv | ForEach-Object { $_.Length } | Sort-Object)
      $cutL = @($pv | Where-Object { $_ -match '(…|\.\.\.)$' } | ForEach-Object { $_.Length } | Sort-Object)
      $whole = @($pv | Where-Object { $_ -notmatch '(…|\.\.\.)$' } | ForEach-Object { $_.Length } | Sort-Object)
      $cut = $cutL.Count
      $nl = @($pv | Where-Object { $_ -match "[\r\n]" }).Count
      # preview_cut_len (a length at which a preview counts as cut even without a mark): between the longest preview that was not
      # cut and the shortest that was. Lengths only, never text.
      $cutMin = if ($cutL.Count) { $cutL[0] } else { '-' }
      $wholeMax = if ($whole.Count) { $whole[-1] } else { '-' }
      $hint = if ($cutL.Count -and $whole.Count -and $whole[-1] -lt $cutL[0]) { "preview_cut_len の目安={0}〜{1}" -f ($whole[-1] + 1), $cutL[0] } elseif ($cutL.Count -and $whole.Count) { 'preview_cut_len は決められない（切れていないものが、切れたものより長い）' } else { 'preview_cut_len は決められない（切れた例か、切れていない例が無い）' }
      Rec 'T23' ("件数={0} 文字数 最小={1} 中央値={2} 最大={3} 末尾が省略記号={4} 切れたものの最短={5} 切れていないものの最長={6} 改行あり={7} {8}" -f $pv.Count, $len[0], $len[[int]($len.Count / 2)], $len[-1], $cut, $cutMin, $wholeMax, $nl, $hint)
    }
  }
}

# ---- T24: open one chat, read it, go back (marks the chat as read; you choose to run it) ----
if ($uia -and $selfOk -and (Want 'T24')) {
  Say "T24 チャットを開いて全文を読む（開くと、そのチャットは既読になります）"
  $c = Self 'chats'
  $pick = if ($c.ok) { @($c.chats | Where-Object { $_.kind -ne 'self' -and ([string]$_.preview).Trim().Length -ge 12 -and -not $_.unread } | Select-Object -First 1) } else { @() }
  if (-not $pick.Count) { Rec 'T24' 'SKIP 既読で、プレビューが 12 字以上のチャットが一覧にありません（未読の印を変えないため、既読のものだけを使います）' }
  elseif (-not (YesNo "   既読のチャット 1 件を開いて、直近のメッセージを読みます（読み取りだけ。終わったら元のチャットへ戻します）。よろしいですか")) { Rec 'T24' 'SKIP' }
  else {
    Write-Host "   6 秒間、マウス・キーボードに触らず、Teams も前面に出さないでください（本番の待ちは 30 秒。試験では 5 秒にします）"
    Start-Sleep -Seconds 6
    $savedIdle = $env:KIMERU_READ_IDLE_SEC; $env:KIMERU_READ_IDLE_SEC = '5'
    # the start of the list preview goes with it: a screen that reports no selection is checked against it
    $head = (([string]$pick[0].preview -replace '\s+', ' ').Trim()).TrimEnd(' ', '.', [char]0x2026)
    if ($head.Length -gt 40) { $head = $head.Substring(0, 40) }
    try { $r = Self 'readchat' @('-ChatId', [string]$pick[0].id, '-Count', '5', '-Preview', $head) }
    finally { $env:KIMERU_READ_IDLE_SEC = $savedIdle }
    # what the script says about putting the chat back is on the sheet even when the reading failed (fixed words only)
    $back = "元のチャットへ戻れた={0} 復元={1}" -f $r.returned, $r.restore
    if (-not $r.ok -and ([string]$r.error) -match '^(Teams is in use|the keyboard / mouse has been in use)') { Rec 'T24' ("SKIP Teams が前面か、入力欄にフォーカスがあるか、操作中でした（何も開いていません）。{0}" -f $back) }
    elseif (-not $r.ok) { Rec 'T24' ("NG {0} {1}" -f (Short $r.error), $back) }
    else {
      $lens = @($r.messages | ForEach-Object { ([string]$_.text).Length })
      $last = if ($r.messages) { [string]@($r.messages)[-1].text } else { '' }
      # the script's own verdict (previewMatched): it also handles a leading "name: " in the preview, which a plain Contains would miss
      $fits = if ($head.Length -lt 12) { '（プレビューが短く、確かめられない）' } else { [string]$r.previewMatched }
      Rec 'T24' ("{0} 読めた件数={1} 文字数=[{2}] {3} 選択の報告={4} 読んだ文がプレビューと合った={5} 取り方={6}" -f $(if ($lens.Count -gt 0 -and $r.returned -and $fits -eq 'True') { 'OK' } else { 'NG' }), $lens.Count, ($lens -join ','), $back, $r.verified, $fits, $r.how)
    }
    # after the read (Teams is not in front again if the window that was in front came back): where the focus is, as numbers and a type
    # name only. focus= gives the ProcessId of the focused element, the handle of the window that holds it (hwnd / top) and that
    # window's process (topPid); fgPid is the process of the window in front now, teamsPids are those of Teams. To decide the
    # input-box check on this PC: put the cursor in a compose box, run `teams-self.ps1 -Action diag` by hand, and compare topPid with teamsPids
    $dg = Self 'diag'
    Rec 'T24-diag' $(if ($dg.ok) { "focus=[{0}] teamsInUse={1}" -f (([string]$dg.focus) -replace '[^A-Za-z0-9=/. _]', ''), $dg.teamsInUse } else { 'NG ' + (Short $dg.error) })
  }
}

# ---- T22: an approved ADO comment is written exactly once (a test work item you name) ----
if ($selfOk -and (Want 'T22')) {
  Say "T22 承認した ADO コメントが 1 回だけ書かれるか（試験用の作業項目）"
  $wi = Read-Host "   試験用の作業項目の番号（誰の邪魔にもならないもの。Enter でスキップ）"
  if (-not $wi -or $wi -notmatch '^\d+$') { Rec 'T22' 'SKIP' }
  elseif (-not (YesNo "   作業項目 $wi に、承認のあと、コメントを 1 件書きます（az のサインインが要ります）。よろしいですか")) { Rec 'T22' 'SKIP' }
  else {
    $out22 = Join-Path $tmp 'out22'
    New-Item -ItemType Directory -Force $out22 | Out-Null
    $base = Get-Random -Minimum 100 -Maximum 899
    [IO.File]::WriteAllText((Join-Path $out22 'approvals.json'), "{`"next`": $base, `"items`": {}}")
    # the target the work item "came from": the settings' organization and project (kimeru writes only where the item came from)
    $tgt = (Py @('-c', 'import json; from kimeru import config, pull; config.apply([]); o, p = pull.ado_names(config.value("ado_org"), config.value("ado_project")); print(json.dumps({"org": o, "project": p}))')).Trim()
    $origin = try { $tgt | ConvertFrom-Json } catch { $null }
    $rec = [ordered]@{ graph = 'check'; event_kind = 'ado.workitem.created'; event_id = "t22-$wi"; node = 'request_info'; outcome = 'decide'; needs_human = $true; advice = ''
      event = @{ origin = @{ org = [string]$origin.org; project = [string]$origin.project } }
      actions = @(@{ type = 'ado.comment'; id = $wi; text = 'kimeru の試験です。このコメントは、承認のあとに 1 回だけ書かれます。' }) }
    [IO.File]::WriteAllText((Join-Path $out22 'queue.jsonl'), (($rec | ConvertTo-Json -Compress -Depth 8) + "`n"), (New-Object Text.UTF8Encoding $false))
    $env:KIMERU_EXECUTE = 'ado.comment'
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out22, 'notify', '--send')
    if ($nt -notmatch 'posted: \[') { Rec 'T22' ('NG 投稿できません ' + (Fails $nt)) }
    else {
      Write-Host ("   Teams の自分とのチャットで「OK {0}」と返信してください（最大 {1} 秒）" -f $base, $WaitSec) -ForegroundColor Green
      $deadline = (Get-Date).AddSeconds($WaitSec); $ex = Join-Path $out22 'executions.jsonl'
      while ((Get-Date) -lt $deadline -and -not (Test-Path $ex)) { Start-Sleep -Seconds 10; [void](Py @('-m', 'kimeru', '--out', $out22, 'approvals', '--send')) }
      # two more reads: the same reply is still on the screen, and it must not write again
      [void](Py @('-m', 'kimeru', '--out', $out22, 'approvals', '--send')); [void](Py @('-m', 'kimeru', '--out', $out22, 'approvals', '--send'))
      $rows = if (Test-Path $ex) { @(Get-Content -Encoding UTF8 $ex | ForEach-Object { $_ | ConvertFrom-Json }) } else { @() }
      $done = @($rows | Where-Object { $_.state -eq 'done' }).Count
      $failed = @($rows | Where-Object { $_.state -eq 'failed' } | ForEach-Object { $_.error })
      if ($done -eq 1) {
        $seen = YesNo "   ADO の作業項目 $wi に、kimeru のコメントが **1 件だけ** ありますか"
        Rec 'T22' $(if ($seen) { 'OK 書かれた回数=1、ADO でも 1 件' } else { 'NG kimeru は 1 回書いたと記録したが、ADO で 1 件と確認できない' })
      } elseif ($failed.Count) { Rec 'T22' ('NG 書けませんでした: ' + (Short ($failed -join ' / '))) }
      else { Rec 'T22' ("NG 書かれた回数=$done（返信を待つ時間が足りなかった可能性）") }
    }
    Remove-Item Env:\KIMERU_EXECUTE -ErrorAction SilentlyContinue
  }
}

# ---- T21: the phone notification routes (counts only; you say whether it reached the iPhone) ----
if (Want 'T21') {
  Say "T21 iPhone への通知の経路（件数だけの試験の 1 行を送ります）"
  $o = $null
  # the routes may come from the environment or from the config file: ask kimeru (config show prints no value of a secret)
  $pushSet = @(((Py @('-m', 'kimeru', 'config', 'show')) -split "`n") | Where-Object { $_ -match '^push {2,}[^-\s]' }).Count -gt 0   # the table row only: 'push routes: none' (a status line) is not a setting
  if (-not $pushSet) { Rec 'T21' 'SKIP 経路が未設定（docs/push-notification.md。config set push と URL の環境変数）' }
  elseif (-not (YesNo "   設定した経路に、固定の試験の 1 行（件数も本文もありません）を送ります。よろしいですか")) { Rec 'T21' 'SKIP' }
  else {
    $o = Py @('-m', 'kimeru', 'push', 'test')
    if ($o -match '\(none\)') { Rec 'T21' 'SKIP 経路が未設定（config set push と URL の環境変数）'; $o = $null }
  }
  if ($o) {
    $sent = @(($o -split "`n") | Where-Object { $_ -match ': sent' }).Count
    $bad = @(($o -split "`n") | Where-Object { $_ -match ': failed' } | ForEach-Object { ($_ -replace '\s+$', '') })
    if ($sent -eq 0) { Rec 'T21' ('NG ' + (Short ($bad -join ' / '))) }
    else {
      $seen = YesNo "   iPhone（や指定した先）に「kimeru: 試験の通知です」が届きましたか"
      Rec 'T21' ("{0} 送れた経路={1} 失敗={2}" -f $(if ($seen) { 'OK' } else { 'NG 届かない' }), $sent, $(if ($bad.Count) { (Short ($bad -join ' / ')) } else { 'なし' }))
    }
  }
}

# ---- T20: "聞き返し N" and "下書き N <文面>" are read from the real Teams chat (fictional items) ----
if ($selfOk -and (Want 'T20')) {
  Say "T20 聞き返し・下書き（M365 Copilot の文面の貼り戻し）を Teams から読めるか"
  if (-not (YesNo "   架空の確認待ち2件を自分とのチャットに送信します（宛先は自分だけ）。よろしいですか")) { Rec 'T20' 'SKIP' }
  else {
    $out20 = Join-Path $tmp 'out20'
    New-Item -ItemType Directory -Force $out20 | Out-Null
    $base = Get-Random -Minimum 100 -Maximum 899
    [IO.File]::WriteAllText((Join-Path $out20 'approvals.json'), "{`"next`": $base, `"items`": {}}")
    $recA = [ordered]@{ graph = 'teams-chat-triage'; event_kind = 'teams.chat'; event_id = 'v1-a'; node = 'reply'; outcome = 'decide'; needs_human = $true; advice = ''
      material_event = @{ author = 'Sato'; text = 'リリースは来週火曜にずらせますか' }
      actions = @(@{ type = 'teams.reply'; to = 'Sato'; text = '受領しました。内容を確認して返信します。'; ask_back = '延期の希望日と、影響する顧客を教えていただけますか。' }) }
    $recB = [ordered]@{ graph = 'teams-chat-triage'; event_kind = 'teams.chat'; event_id = 'v1-b'; node = 'reply'; outcome = 'decide'; needs_human = $true; advice = ''
      material_event = @{ author = 'Ito'; text = '仕様の確認をお願いします' }
      copilot_request = '次の件について、私（PM）の名前で送る文面を書いてください。（架空の試験）'
      actions = @(@{ type = 'teams.reply'; to = 'Ito'; text = '受領しました。内容を確認して返信します。'; held_for = 'm365' }) }
    $q = (@($recA, $recB) | ForEach-Object { $_ | ConvertTo-Json -Compress -Depth 8 }) -join "`n"
    [IO.File]::WriteAllText((Join-Path $out20 'queue.jsonl'), $q + "`n", (New-Object Text.UTF8Encoding $false))
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out20, 'notify', '--send')
    if ($nt -notmatch 'posted: \[') { Rec 'T20' ('NG 投稿できません ' + (Fails $nt)) }
    else {
      Write-Host ("   Teams の自分とのチャットで、次の 2 つを別々に返信してください（最大 {0} 秒）" -f $WaitSec) -ForegroundColor Green
      Write-Host ("     聞き返し {0}" -f $base) -ForegroundColor Green
      Write-Host ("     下書き {0} ご連絡ありがとうございます。仕様を確認して、改めてご連絡します。" -f ($base + 1)) -ForegroundColor Green
      $deadline = (Get-Date).AddSeconds($WaitSec); $okA = $false; $okB = $false
      while ((Get-Date) -lt $deadline -and -not ($okA -and $okB)) {
        Start-Sleep -Seconds 10
        [void](Py @('-m', 'kimeru', '--out', $out20, 'approvals'))
        try {
          $ap = Get-Content -Raw -Encoding UTF8 (Join-Path $out20 'approvals.json') | ConvertFrom-Json
          $ia = $ap.items.PSObject.Properties[[string]$base].Value; $ib = $ap.items.PSObject.Properties[[string]($base + 1)].Value
          $okA = ($ia.record.actions[0].variant -eq 'ask_back')
          $okB = ($ib.record.actions[0].drafted_by -like 'm365*')
        } catch {}
      }
      Rec 'T20' ("{0} 聞き返し={1} 下書き={2}" -f $(if ($okA -and $okB) { 'OK' } else { 'NG' }), $(if ($okA) { '読めた' } else { '読めない' }), $(if ($okB) { '読めた' } else { '読めない' }))
    }
  }
}

# ---- Level 2: Azure CLI ----
if (Want 'T10') { Say "T10 Azure CLI で取り込み" }
# az: KIMERU_AZ, then PATH, then the official no-install ZIP unpacked to C:\az (same order as kimeru/pull.py)
$az = @($env:KIMERU_AZ, (Get-Command az -ErrorAction SilentlyContinue).Source, (Join-Path (Split-Path -Parent $root) 'az\bin\az.cmd'), (Join-Path $root 'az\bin\az.cmd'), 'C:\az\bin\az.cmd') |
  Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($az) { $env:KIMERU_AZ = $az }
if (-not (Want 'T10')) { }
elseif (-not $az -or -not $py) { Rec 'T10' $(if (-not $az) { 'SKIP az なし（持ち込み用フォルダの az をこのフォルダの隣に置く）' } else { 'SKIP Python なし' }) }
else {
  & $az account show -o none 2>$null
  if ($LASTEXITCODE -ne 0 -and (YesNo "   az にサインインしていません。az login を実行しますか（ブラウザが開きます）")) { & $az login -o none 2>&1 | Out-Null }
  & $az account show -o none 2>$null
  if ($LASTEXITCODE -ne 0) { Rec 'T10' 'NG az login できず（条件付きアクセス等）' }
  else {
    $inbox = Join-Path $tmp 'inbox'; $res = @()
    $org = Read-Host "   ADO の組織名（dev.azure.com/<ここ>。URL や 組織/プロジェクト の形でも可。Enter でスキップ）"
    if ($org) {
      $proj = if ($org -match '/.+/.+|^[^/]+/[^/]+$') { '' } else { Read-Host "   ADO のプロジェクト名" }
      # PS 5.1 drops an empty argument: pass --project only when it has a value (a pasted URL carries it)
      $adoArgs = @('--org', $org) + $(if ($proj) { @('--project', $proj) } else { @() }) + @('--inbox', $inbox)
      $a = Py (@('-m', 'kimeru', '--out', $out, 'pull', 'ado') + $adoArgs)
      if ($a -match 'with --login' -and (YesNo "   ADO の組織は別のテナントにあります。そのテナントで az login しますか（ブラウザが開きます）")) {
        $a = Py (@('-m', 'kimeru', '--out', $out, 'pull', 'ado', '--login') + $adoArgs)
      }
      $res += "ado=" + $(if ($a -match '(\d+) new') { "$($Matches[1]) 件" } else { Fails $a })
    }
    $sub = (& $az account show --query id -o tsv 2>$null)
    if ($sub -and (YesNo "   現在のサブスクリプションの発報中アラートを読み取りますか（読み取りのみ）")) {
      $a = Py @('-m', 'kimeru', '--out', $out, 'pull', 'alerts', '--subscription', $sub, '--inbox', $inbox)
      $res += "alerts=" + $(if ($a -match '(\d+) new') { "$($Matches[1]) 件" } else { Fails $a })
    }
    if (Test-Path $inbox) {
      $w = Py @('-m', 'kimeru', '--out', $out, 'watch', $inbox, '--once')
      $res += "判断=" + ([regex]::Matches($w, '(?m)^\[')).Count
    }
    $azv = (& $az version --query '\"azure-cli\"' -o tsv 2>$null)
    Rec 'T10' ("az=$azv ログイン済み / " + $(if ($res) { $res -join ' / ' } else { '取り込み対象の指定なし' }))
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
  } catch { Rec 'T12' ('NG ' + (Safe-Error $c)) }
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
  if (-not $up) { Rec 'T14' 'SKIP（kev 未起動: START.cmd か kev\start-kev.cmd を先に実行）' }
  else {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $k = Py @('-m', 'kimeru', '--backend', 'kev', '--out', (Join-Path $tmp 'kev'), 'run', 'examples\teams_chat.json')
    $sw.Stop()
    $dtype = try { ((Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json).models[0].dtype } catch { '?' }
    $cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name -replace '\s+', ' '
    Rec 'T14' $(if ($k -match 'plan:|path:') { ("OK {0}s dtype={1} CPU={2} " -f [math]::Round($sw.Elapsed.TotalSeconds), $dtype, $cpu) + (Short (($k -split "`n") | Where-Object { $_ -match 'path:' } | Select-Object -First 1)) } else { 'NG ' + (Fails $k) })
  }
}

# ---- T15: GitHub Copilot CLI writes the follow-up text (fictional sample only) ----
if ($py -and (Want 'T15')) {
  Say "T15 GitHub Copilot で文面の下書き（架空のサンプル 1 件。業務のデータは送りません）"
  $cop = (Get-Command copilot -ErrorAction SilentlyContinue).Source
  if (-not $cop -and $py) {
    $c = ((Py @('-c', 'from kimeru import writer; print(writer.find_exe("copilot", "KIMERU_COPILOT_EXE") or "")')) -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    if ($c -and (Test-Path ([string]$c).Trim())) { $cop = ([string]$c).Trim() }
  }
  if (-not $cop) {
    # not on PATH and not in the usual installer folders: look for it (bounded, read-only)
    $roots = @($env:LOCALAPPDATA, $env:APPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:USERPROFILE '.local')) | Where-Object { $_ -and (Test-Path $_) }
    $hits = @(foreach ($r in $roots) { Get-ChildItem -Path $r -Filter 'copilot*' -Recurse -Depth 5 -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -in '.exe', '.cmd', '.bat', '.ps1' } | Select-Object -First 4 -ExpandProperty FullName })
    $mask = { param($x) ([string]$x).Replace($env:USERPROFILE, '%USERPROFILE%') }
    if ($hits.Count) {
      $cop = $hits[0]
      $env:KIMERU_COPILOT_EXE = $cop
      Write-Host ("   copilot が見つかりました: " + (& $mask $cop)) -ForegroundColor Green
      Write-Host ("   これから常に使うなら:  setx KIMERU_COPILOT_EXE `"" + $cop + "`"") -ForegroundColor Green
    } else {
      $wg = try { (& winget --version 2>&1 | Out-String).Trim() } catch { 'なし' }
      Rec 'T15' ("SKIP copilot が見つかりません（PATH・winget・npm・アプリ内を検索済み、winget=$wg）。GitHub Copilot CLI: winget install GitHub.Copilot")
    }
  }
  if (-not $cop) { }
  elseif (-not (YesNo "   架空のチャット 1 件の返信とタスク説明を GitHub Copilot に書かせますか")) { Rec 'T15' 'SKIP' }
  else {
    $d = Py @('eval\drafts.py', '--backend', 'stub', '--writer', 'copilot', '--n', '1', '--kinds', 'teams.chat')
    $sum = ($d -split "`n") | Where-Object { $_ -match '^events=' } | Select-Object -First 1
    Rec 'T15' $(if ($sum -and $sum -match 'failed_events=0') { 'OK ' + (Short $sum) } else { 'NG ' + (Fails $d) })
  }
}

# ---- T19: paste test only (nothing is sent anywhere; the pasted test text is removed again) ----
if ($uia -and (Want 'T19')) {
  Say "T19 Copilot の入力欄への貼り付けテスト（送信しません。貼った文字は自動で消します）"
  if (-not (YesNo "   Teams で Copilot のチャットを開きましたか（他のチャットの側パネルは閉じてください）")) { Rec 'T19' 'SKIP' }
  else {
    $pt = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-copilot.ps1') -Action pastetest 2>&1 | Out-String
    $pj = $null; try { $pj = $pt.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json } catch {}
    if ($pj -and $pj.ok) { Rec 'T19' ('OK ' + $pj.info) } else { Rec 'T19' ('NG ' + (Safe-Error $pt)); Rec 'T19-diag' (DiagText) }
  }
}

# ---- T18: does Microsoft 365 Copilot name the mail / meetings it used? (needs a real subject you know) ----
if ($uia -and $py -and (Want 'T18')) {
  Say "T18 M365 Copilot の出典（実際のメール・会議を参照できるか）"
  $go = $false
  $topic = Read-Host "   Copilot が参照できる、最近の会議名かメールの件名の一部（Copilot にだけ送ります。結果シートには書きません。Enter でスキップ）"
  if (-not $topic) { Rec 'T18' 'SKIP' }
  elseif (-not (YesNo "   Teams で Copilot のチャットが開いていて、他のチャットの側パネル・別ウィンドウは閉じていますか（依頼文を貼って送信します）")) { Rec 'T18' 'SKIP' }
  else {
    # a check run is a deliberate attempt: ignore (and reset) the rest the automatic route took after an earlier failure
    $env:KIMERU_M365_NO_REST = '1'
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $(if ($env:KIMERU_STATE_DIR) { $env:KIMERU_STATE_DIR } else { Join-Path $env:LOCALAPPDATA 'kimeru' }) 'm365-auto.json')
    # nothing is sent until a paste test (paste a short text into the Copilot box, read it back, remove it) has passed
    $pt = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-copilot.ps1') -Action pastetest 2>&1 | Out-String
    $pj = $null; try { $pj = $pt.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json } catch {}
    if (-not $pj -or -not $pj.ok) {
      Rec 'T18' ("SKIP 送信せず: 貼り付けテストに通りませんでした " + (Safe-Error $pt))
      Rec 'T18-diag' (DiagText)
    } else { Rec 'T18-paste' ("OK " + $pj.info); $go = $true }
  }
  if ($go) {
    $d = Py @('eval\drafts.py', '--backend', 'stub', '--writer', 'm365-auto', '--n', '1', '--kinds', 'teams.chat', '--topic', $topic)
    $sum = ($d -split "`n") | Where-Object { $_ -match '^events=' } | Select-Object -First 1
    $m = [regex]::Match([string]$sum, 'texts=(\d+) drafted=(\d+) held=\d+ sources=(\d+) sources_key=(\d+) failed_events=(\d+)')
    $err = [string](($d -split "`n") | Where-Object { $_ -match 'ERROR ' } | Select-Object -First 1)
    $err = ($err -replace '.*ERROR ', '').Replace($topic, '<件名>')
    if ($err.Length -gt 300) { $err = $err.Substring(0, 300) }
    if (-not $m.Success) { Rec 'T18' 'NG 実行できませんでした（件名は記録しません）' }
    elseif ($m.Groups[5].Value -ne '0') { Rec 'T18' ("NG writer が失敗: " + $err); Rec 'T18-diag' (DiagText) }
    else { Rec 'T18' ("drafted=$($m.Groups[2].Value)/$($m.Groups[1].Value) 出典=$($m.Groups[3].Value) 件 出典欄への回答=$(if ($m.Groups[4].Value -ne '0') { 'あり' } else { 'なし' }) 失敗=$($m.Groups[5].Value)" + $(if ($m.Groups[3].Value -ne '0') { '' } elseif ($m.Groups[4].Value -ne '0') { '（Copilot は「参照なし」と答えた: 件名が見つからなかった可能性）' } else { '（Copilot は出典欄に答えなかった: 依頼文の反映を確認）' })) }
  }
}

# ---- T16: Windows notification on this PC (self-chat posts never notify your own devices) ----
if (Want 'T16') {
  Say "T16 PC への通知（Windows の通知を 1 件出します。クリックすると Teams の自分とのチャットが開きます）"
  $o = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'toast.ps1') -Title 'kimeru: テスト通知' -Body '確認待ちができるとこの通知が出ます' 2>&1 | Out-String
  $tj = $null; try { $tj = $o.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json } catch {}
  if ($o -notmatch '"ok":true') { Rec 'T16' ('NG ' + (Safe-Error $o)) }
  else {
    $why = if ($tj) { "設定=$($tj.setting) 通知センター内=$($tj.inCenter)件" } else { '' }
    $seen = YesNo "   画面右下に「kimeru: テスト通知」が出ましたか（集中モード中は通知センターに入ります）"
    Rec 'T16' $(if ($seen) { "OK 通知が表示された $why" } else { "NG 通知が見えない $why（DisabledForUser=設定または集中モード / DisabledByGroupPolicy=組織の設定。通知センターに 1 件以上あれば、届いているが画面には出ていない）" })
  }
}

# ---- T17: Microsoft 365 Copilot in Teams (m365-auto writer) ----
if ($uia -and (Want 'T17')) {
  Say "T17 Teams の Copilot チャット（画面構造の調査。業務のデータは送りません）"
  $pr = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-copilot.ps1') -Action probe 2>&1 | Out-String
  $pj = try { $pr.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json } catch { $null }
  if (-not $pj -or -not $pj.ok) { Rec 'T17-probe' ('NG ' + (Safe-Error $pr)) }
  else { Rec 'T17-probe' ("entries=$($pj.entries) [$($pj.entryTypes)] opened=$($pj.opened) title=$($pj.title) box=$($pj.composeBox) strictCopilotBox=$($pj.strictCopilotBox) pane=$($pj.paneCheck) cand=$($pj.strictCandidates) id=$($pj.boxId) send=$($pj.sendButton) edits=[$(@($pj.edits) -join ';')] docs=[$(@($pj.docs) -join ';')] buttons=[$(@($pj.buttons) -join ',')] boxTextLen=$($pj.boxTextLen) boxNameLen=$($pj.boxNameLen)") }
  if ($py -and $pj -and $pj.opened -and (YesNo "   架空のチャット 1 件の文面を Teams の Copilot に書かせますか（Copilot の履歴に残ります）")) {
    $env:KIMERU_DEBUG_WRITER = '1'   # the sample is fictional: the start of the answer may be shown on failure
    $d = Py @('eval\drafts.py', '--backend', 'stub', '--writer', 'm365-auto', '--n', '1', '--kinds', 'teams.chat')
    Remove-Item Env:KIMERU_DEBUG_WRITER -ErrorAction SilentlyContinue
    $sum = ($d -split "`n") | Where-Object { $_ -match '^events=' } | Select-Object -First 1
    $err = ($d -split "`n") | Where-Object { $_ -match 'ERROR' } | Select-Object -First 1
    $m = [regex]::Match([string]$sum, 'texts=(\d+) drafted=(\d+) held=(\d+) failed_events=(\d+)')
    $all = $m.Success -and $m.Groups[1].Value -eq $m.Groups[2].Value -and $m.Groups[4].Value -eq '0'
    Rec 'T17' $(if ($all) { 'OK ' + (Short $sum) } else { 'NG ' + (Short $sum) + $(if ($err) { ' | ' + ((($err -replace '\s+', ' ').Trim()) -replace '^(.{0,2400}).*$', '$1') } else { '' }) })
  } elseif (Want 'T17') { Rec 'T17' 'SKIP' }
}

# ---- Result ----
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
$sheet = @("kimeru 管理PCチェック結果 $(Get-Date -Format 'yyyy-MM-dd HH:mm')") + ($Results.GetEnumerator() | ForEach-Object { "{0,-6} {1}" -f $_.Key, $_.Value })
$sheet | Out-File -Encoding utf8 (Join-Path $root 'kimeru-check-result.txt')
try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'kimeru-check-result.txt を開いてコピーしてください' }
Say "結果（$clip）"
$sheet | ForEach-Object { Write-Host $_ }

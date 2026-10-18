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
  # -Auto: no question is asked. Every step that would send, open a chat or need your eyes is answered "no" and recorded as
  # SKIP; Teams is not started. Used by run-auto-check.cmd
  [switch]$Auto,
  # -SelfReply: the replies a human types in the steps T5/T9/T20/T25/T27 are typed by this script into the SELF chat only (a fixed
  # whitelist of short words with the number it just posted: teams-self.ps1 testreply); the [y/N] consents of those steps are answered y.
  # Also on with KIMERU_CHECK_SELF_REPLY=1. Never with -Auto
  [switch]$SelfReply,
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
if ($Only -contains 'managed' -or $Only -contains 'monday') { $Only = @('T9', 'T10', 'T12', 'T13', 'T14', 'T15', 'T16', 'T17') }   # short bring-in set (T3/T6/T8 run anyway); T2 dropped: minutes come as .txt (docs/minutes-format.md)
if ($Only -contains 'auto') { $Only = @('T1', 'T7', 'T8', 'T12', 'T13', 'T14', 'T16', 'T23') }   # what runs with nobody answering and nothing sent (T3/T6 run anyway)
if ($env:KIMERU_CHECK_SELF_REPLY -eq '1') { $SelfReply = [switch]$true }
if ($Auto -and $SelfReply) { $SelfReply = [switch]$false }   # the automatic check stays read only
# the steps that need another person or an outside service are not done with an automatic reply
$SelfReplySkip = @('T10', 'T11', 'T15', 'T16', 'T17', 'T18', 'T19', 'T21', 'T22', 'T24', 'T26')
function Want($t) {
  $w = (-not $Only) -or ($Only -contains $t)
  if ($w -and $SelfReply -and $SelfReplySkip -contains $t) {
    if (-not $Results.Contains($t)) { Rec $t 'SKIP（自動の返信では行わない: 相手・外部が要る）' }
    return $false
  }
  $w
}
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
    # the part names of the list items are UI identifiers (letters, digits, '-'), never names or text
    $parts = (@($j.itemParts) | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^[A-Za-z][A-Za-z0-9-]{1,40}=\d+$' }) -join ' '
    return "focus=[$focus] teamsInUse=$($j.teamsInUse) selfItemFound=$($j.selfItemFound) learnedName=$($j.learnedName) treeItems=$($j.treeItems) listItems=$($j.listItems) itemsWithChatId=$($j.itemsWithChatId) 括弧表記の種類=$marks title=$shape parts=[$parts]"
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
function Posted($t) {
  # `notify` printed "posted: [...]" (or "pasted (not sent): [...]"); the lock-held message ("another approvals run is in progress") is a failure, never a post
  ([string]$t -match '(?m)^(posted|pasted \(not sent\)): \[') -and ([string]$t -notmatch 'another approvals run is in progress')
}
function Focus-Numbers($dg) {
  # the focus line of `diag` as numbers, a control type name and booleans only: type, topPid, fgPid, whether topPid is one of teamsPids
  $f = [string]$dg.focus
  if ($f -match '^(none|error)$' -or -not $f) { return "focus=なし teamsInUse=$($dg.teamsInUse)" }
  $type = ([regex]::Match($f, '^([A-Za-z0-9.]+)').Groups[1].Value)
  $top = [regex]::Match($f, 'topPid=(\d+)').Groups[1].Value
  $fg = [regex]::Match($f, 'fgPid=(\d+)').Groups[1].Value
  $tp = @(([regex]::Match($f, 'teamsPids=([\d/]*)').Groups[1].Value) -split '/' | Where-Object { $_ })
  "type=$type topPid=$top fgPid=$fg topPidはTeamsのプロセス=$([bool]($top -and ($tp -contains $top))) fgPidはTeamsのプロセス=$([bool]($fg -and ($tp -contains $fg))) teamsInUse=$($dg.teamsInUse)"
}
function YesNo($q) { (Read-Host "$q [y/N]") -match '^\s*([yYｙＹ]|はい)' }   # tolerate stray keys after y ("y[")
if ($Auto) {
  # nobody answers: every yes/no is "no" (nothing is sent, opened or downloaded) and every typed answer is empty (skip)
  function YesNo($q) { Write-Host "$q [y/N] -> 自動: N" -ForegroundColor DarkGray; $false }
  function Read-Host { param([Parameter(Position = 0)]$Prompt) if ($Prompt) { Write-Host "$Prompt -> 自動: 空欄（スキップ）" -ForegroundColor DarkGray }; '' }
}
function Self($action, [string[]]$extra = @()) {
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action $action @extra 2>&1 | Out-String
  # the script's own output is JSON with fixed messages; anything else (screen text, a stack trace) is never recorded: only its length
  try { return ($out.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json) } catch { return [pscustomobject]@{ ok = $false; error = "出力を解析できず（$($out.Length) 文字、内容は記録しない）" } }
}
# -SelfReply: the consent prompts of T5/T9/T20/T25/T27 are answered y, the questions about what a person saw are answered n
function Consent($q) {
  if ($SelfReply) { Write-Host "$q [y/N] -> （自動: y）" -ForegroundColor DarkGray; return $true }
  YesNo $q
}
$replyWait = if ($SelfReply) { [Math]::Min($WaitSec, 60) } else { $WaitSec }   # the replies are typed already: a shorter wait
function AutoNo($q) {
  Write-Host "$q -> （自動: n）" -ForegroundColor DarkGray; 'n'
}
function AutoTag($k) { if ($SelfReply -and $Results.Contains($k)) { $Results[$k] = [string]$Results[$k] + ' 返信=自動'; Write-Host "   ${k}: 返信=自動" -ForegroundColor Yellow } }
# types one short reply into the SELF chat (testreply: a fixed whitelist, only a number this run posted). $true when it was typed and read back
function Auto-Reply([string]$text, [string]$nums) {
  $r = Self 'testreply' @('-Text', $text, '-Numbers', $nums)
  if ($r.ok -and $r.typed) { Write-Host "   （自動: 自分とのチャットに「$text」と返信）" -ForegroundColor DarkGray; return $true }
  Write-Host "   （自動の返信に失敗: $(Short $r.error)）" -ForegroundColor Yellow
  $script:ReplyErr = Short $r.error
  return $false
}
$script:ReplyErr = ''
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
function ReplySeen($id, $word) {   # one read of the self chat: is my post T:id there, with R:word id below it?
  $r = Self 'read'
  if ($r.ok -and $r.timeline) {
    $tl = @($r.timeline); $last = -1
    for ($i = 0; $i -lt $tl.Count; $i++) { if ($tl[$i] -eq "T:$id") { $last = $i; $script:PostSeen = $true } }
    for ($i = $last + 1; $last -ge 0 -and $i -lt $tl.Count; $i++) { if ($tl[$i] -eq "R:$word $id") { return $true } }
  }
  return $false
}
function WaitReply($id, $word, [int]$limitSec = $WaitSec) {
  # reads every 10 s, says "still waiting" every 30 s, and says so as soon as the reply is read
  $deadline = (Get-Date).AddSeconds($limitSec); $nextNote = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    if (ReplySeen $id $word) { Write-Host "   返信を読み取りました" -ForegroundColor Green; return $true }
    if ((Get-Date) -ge $nextNote) {
      $left = [Math]::Max(0, [int][Math]::Ceiling(($deadline - (Get-Date)).TotalSeconds))
      Write-Host ("   返信を待っています… 残り {0} 秒" -f $left) -ForegroundColor DarkGray
      $nextNote = $nextNote.AddSeconds(30); if ($nextNote -le (Get-Date)) { $nextNote = (Get-Date).AddSeconds(30) }
    }
    Start-Sleep -Seconds 10
  }
  return $false
}

# N4 (T25 to T27): Python judges with kimeru's own functions and prints only a count, a state or a fixed word as JSON; a row's text is never printed or kept
function Control-Word($rows, $timeline = $null) {
  # control.line_of: my own line of the self chat as 停止, 再開 or none (the daily cycle's judgment). With $timeline (the self chat's own
  # timeline, used when the list has no self row) control.line_of_timeline: the last control entry of the timeline
  $tf = Join-Path $tmp ('ctl-' + (Get-Random) + '.json')
  $payload = if ($null -ne $timeline) { [ordered]@{ timeline = @($timeline) } } else { @($rows) }
  [IO.File]::WriteAllText($tf, (ConvertTo-Json -InputObject $payload -Compress), (New-Object Text.UTF8Encoding $false))
  $j = Py @('-c', 'import json, sys; from kimeru import control; d = json.load(open(sys.argv[1], encoding=''utf-8'')); r = control.line_of_timeline(d[''timeline'']) if isinstance(d, dict) else control.line_of(d); print(json.dumps({''row'': r is not None, ''word'': r[1] if r else None}))', $tf)
  Remove-Item $tf -ErrorAction SilentlyContinue
  try { return (($j -split "`n") | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1 | ConvertFrom-Json) } catch { return $null }
}
function Sent-Classes($sendText, $shown) {
  # sent.classify: each of my rows in other chats against the text I was asked to send, as as_is / edited / other (counts only)
  $tf = Join-Path $tmp ('sent-' + (Get-Random) + '.json')
  [IO.File]::WriteAllText($tf, (ConvertTo-Json -InputObject ([ordered]@{ sent = [string]$sendText; shown = @($shown) }) -Compress), (New-Object Text.UTF8Encoding $false))
  $j = Py @('-c', 'import json, sys; from kimeru import sent; d = json.load(open(sys.argv[1], encoding=''utf-8'')); r = [sent.classify(d[''sent''], s)[0] for s in d[''shown'']]; print(json.dumps({''n'': len(r), ''as_is'': r.count(''as_is''), ''edited'': r.count(''edited''), ''other'': r.count(''other'')}))', $tf)
  Remove-Item $tf -ErrorAction SilentlyContinue
  try { return (($j -split "`n") | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1 | ConvertFrom-Json) } catch { return $null }
}

Write-Host "kimeru 管理PCチェック（外部送信の前には必ず確認します。Ctrl+C でいつでも中止できます）" -ForegroundColor Green
if ($SelfReply) { Write-Host "自動の返信（-SelfReply）: T5/T9/T20/T25/T27 の確認はすべて y とし、返信の短い語は、このスクリプトが自分とのチャットだけに打ちます" -ForegroundColor Green }

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

# N2: Teams that runs with no window on screen (tray) is not "not started": the two states get their own sentence
function Teams-Down($st) {
  if ($st.teams_process) { 'Teams は起動しているが、ウィンドウが表示されていない。Teams のウィンドウを開いてチャット一覧を表示してから実行する' } else { 'Teams が起動していない' }
}

$selfOk = $false
if ($uia -and $Auto -and -not ($Only | Where-Object { $_ -in @('T1', 'T3', 'T12', 'T23') })) { $uia = $false; Rec 'T1-T5' 'SKIP（自動: 画面の確認を選んでいない）' }
if ($uia -and $Auto -and -not (Self 'status').teams) { $uia = $false; Rec 'T1-T5' ("SKIP（自動: {0}。自動では起動しない）" -f (Teams-Down (Self 'status'))) }
if ($uia) {
  Say "Teams を起動"
  if (-not (Self 'status').teams) {
    $down = Self 'status'
    Write-Host ("   {0}（最大 60 秒、待ちます）" -f (Teams-Down $down)) -ForegroundColor Yellow
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
  if ($Auto) {
    # N5: the automatic check opens no chat. It reads the self chat only when the reader reports it open already (status.self_open)
    $st = Self 'status'
    $selfOk = [bool]$st.self_open
    if ($selfOk) {
      $r = Self 'read'
      $selfOk = [bool]$r.ok
      Rec 'T3' $(if ($selfOk) { "OK read（開いていた自分とのチャットを読んだ。timeline=$(@($r.timeline).Count)件）" } else { "NG $($r.error)" })
    } else { Rec 'T3' 'SKIP（自動: 自分とのチャットが開いていない。手で開いてから再実行するか run-check.cmd T3）' }
  } else {
    $s = Self 'open'; $r = Self 'read'
    $selfOk = [bool]($s.ok -and $r.ok)
    Rec 'T3' $(if ($selfOk) { "OK open/read（timeline=$(@($r.timeline).Count)件）" } else { "NG $($s.error) $($r.error)" })
  }
  if (-not $selfOk -and $Auto) { Rec 'T3-learn' 'SKIP（自動: 自分とのチャットを手で開く手順は行わない）' }
  elseif (-not $selfOk) {
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
    if (Consent "   自分とのチャットにテストメッセージを1通送信します（宛先は自分だけ）。よろしいですか") {
      $noWinMsg = 'Teams のウィンドウが表示されていません（起動はしています）。ウィンドウを開いてチャット一覧を表示してから run-check.cmd T5 をやり直してください'
      $st5 = Self 'status'
      if ($st5.teams_process -and -not $st5.teams_window) {
        Rec 'T4' "NG $noWinMsg"; Rec 'T5' "NG $noWinMsg"; $selfOk = $false
      } else {
      $n = Get-Random -Minimum 100 -Maximum 999
      Write-Host "   数秒間マウス・キーボードに触らないでください"
      $p1 = Self 'post' @('-Text', "[kimeru 試験 #$n] 管理PCテスト\n返信: OK $n")
      $s1 = if ($p1.ok -and $p1.typed) { Self 'send' } else { $p1 }
      if ($s1.ok) {
        # the reply may be typed on the PC or the iPhone and has been slower than 180 s: 300 s by default; KIMERU_T5_WAIT_SEC (30-900, otherwise ignored) changes it
        $t5Wait = 300
        if ($env:KIMERU_T5_WAIT_SEC -match '^\s*\d{1,4}\s*$') { $t5Env = [int]$env:KIMERU_T5_WAIT_SEC; if ($t5Env -ge 30 -and $t5Env -le 900) { $t5Wait = $t5Env } }
        if ($SelfReply) { [void](Auto-Reply "OK $n" "$n"); $t5Wait = [Math]::Min($t5Wait, 90) }
        else {
        Write-Host "   PC か iPhone の Teams で、自分とのチャットに「OK $n」と返信してください（全角でも可。最大 $t5Wait 秒待ちます）" -ForegroundColor Green
        Write-Host "   ※ 自分宛てのメッセージなので、スマホには通知されません。Teams アプリで自分とのチャットを開いてください"
        }
        $got = WaitReply $n 'OK' $t5Wait
        $late = $false
        if (-not $got) {   # the reply may have arrived just after the window: read the self chat once more before calling it a failure
          $late = ReplySeen $n 'OK'
          if ($late) { Write-Host "   返信を読み取りました（待ち時間のあとに届いていました）" -ForegroundColor Green }
        }
        $push = if ($SelfReply) { AutoNo "   iPhone に通知は届きましたか？ [y/n]" } else { Read-Host "   iPhone に通知は届きましたか？ [y/n]" }
        Rec 'T4' ("OK 送信方法=" + $s1.via)
        $replied = if ($got -or $late) { 'y' } elseif ($SelfReply) { AutoNo "   返信しましたか（PC でも iPhone でも）？ [y/n]" } else { Read-Host "   返信しましたか（PC でも iPhone でも）？ [y/n]" }
        Rec 'T5' ("送信=OK 投稿を画面で確認={0} iPhone通知={1} 返信した={2} 返信読取={3}" -f $(if ($script:PostSeen) { 'あり' } else { 'なし' }), $(if ($SelfReply) { '自動（確認せず）' } elseif ($push -match '^\s*[yY]') { 'あり' } else { 'なし' }), $(if ($replied -match '^\s*[yY]') { 'はい' } else { 'いいえ' }), $(if ($got) { 'OK' } elseif ($late) { 'OK(遅れて届いた)' } else { 'NG(タイムアウト)' }))
        AutoTag 'T5'
        $selfOk = [bool]($got -or $late)
      } else {
        $e4 = "$($p1.error)"; $e5 = "$($s1.error)"
        if ($st5.teams_process -and $e4 -match 'Teams window not found') { $e4 = $noWinMsg }
        if ($st5.teams_process -and $e5 -match 'Teams window not found') { $e5 = $noWinMsg }
        Rec 'T4' "NG $e4"; Rec 'T5' "NG $e5"; $selfOk = $false
      }
      }
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
# NOTE: -c programs passed through Py must not contain a double quote (Windows PowerShell 5.1 drops it from array elements); use '' for a Python single quote
# The kimeru commands of this check post and read TEST posts ("[kimeru 試験 #N]"), which the daily cycle never takes for an approval (KIMERU_TEST_MARK=1; notify.scoped_timeline)
function Py([string[]]$a) {
  $had = $env:KIMERU_TEST_MARK; if ($a -contains 'kimeru') { $env:KIMERU_TEST_MARK = '1' }
  # the steps that promise "no outside traffic" (T7 tests, T8 sample judgments, T14 Kev; every step with -Auto) run kimeru
  # with a fresh state folder (no saved settings) and none of this PC's KIMERU_* settings: a writer set here (Copilot,
  # m365-auto, ...) would otherwise be sent the sample texts, and a phone route or execution would be reached
  $saved = @{}
  if ($Auto -or $script:IsolatePy) {
    foreach ($n in @(Get-ChildItem env: | Where-Object { $_.Name -like 'KIMERU_*' -and $_.Name -notin @('KIMERU_TEST_MARK', 'KIMERU_KEV_URL', 'KIMERU_KEV_URL_REMOTE_OK') } | ForEach-Object Name)) {
      $saved[$n] = (Get-Item "env:$n").Value; Remove-Item "env:$n"
    }
    $saved['KIMERU_STATE_DIR'] = $env:KIMERU_STATE_DIR
    $env:KIMERU_STATE_DIR = Join-Path $tmp 'auto-state'
  }
  try { & $py[0] $py[1..9] @a 2>&1 | Out-String }
  finally {
    $env:KIMERU_TEST_MARK = $had
    foreach ($n in $saved.Keys) { if ($null -eq $saved[$n]) { Remove-Item "env:$n" -ErrorAction SilentlyContinue } else { Set-Item "env:$n" $saved[$n] } }
  }
}

if ($py) {
  if (Want 'T7') {
  Say "T7 テストと検証"
  $script:IsolatePy = $true
  $t = Py @('-m', 'unittest', '-q'); $tOk = $LASTEXITCODE -eq 0
  $v = Py @('-m', 'kimeru', 'validate'); $vOk = $LASTEXITCODE -eq 0
  # on a failure, the names of the failing tests (not the end of the output, which is the validate listing)
  $failed = @(($t -split "`n") | Where-Object { $_ -match '^(FAIL|ERROR): ' } | ForEach-Object { ($_ -replace '^(FAIL|ERROR): (\S+) \(([^)]*)\).*$', '$3') -replace '^tests\.', '' } | Select-Object -Unique)
  $ran = (($t -split "`n") | Where-Object { $_ -match '^(Ran \d|FAILED \(|OK\s*$|OK \()' }) -join ' '
  Rec 'T7' $(if ($tOk -and $vOk) { "OK " + (Short (($t -split "`n") | Where-Object { $_ -match '^Ran ' })) }
             elseif (-not $tOk) { "NG テスト: " + (Short $ran) + " 失敗: " + (($failed | Select-Object -First 5) -join ', ') }
             else { "NG validate: " + (Short (($v -split "`n" | Where-Object { $_ -notmatch '^ok ' }) -join ' ')) })

  }

  Say "T8 サンプルで判断と朝のまとめ"
  $script:IsolatePy = $true
  $out = Join-Path $tmp 'out'
  $ex = @('examples\teams_chat.json', 'examples\monitor_alert.json', 'examples\ado_workitem.json', 'examples\meeting_minutes.json')
  $o = Py (@('-m', 'kimeru', '--out', $out, 'run') + $ex); $rOk = $LASTEXITCODE -eq 0
  $b = Py @('-m', 'kimeru', '--out', $out, 'brief')
  $nDec = ([regex]::Matches($o, '(?m)^\[')).Count
  $script:IsolatePy = $false
  Rec 'T8' $(if ($rOk -and $b -match 'kimeru brief') { "OK 判断=$nDec plan=$(([regex]::Matches($o, 'plan:')).Count)" } else { "NG " + (Short $o) })

  if (Want 'T9') { Say "T9 承認の流れ（iPhone から OK / NG）" }
  if (-not (Want 'T9')) { }
  elseif ($selfOk -and (Consent "   確認待ち2件を自分とのチャットに送信します（宛先は自分だけ）。よろしいですか")) {
    $base = Get-Random -Minimum 100 -Maximum 899
    [IO.File]::WriteAllText((Join-Path $out 'approvals.json'), "{`"next`": $base, `"items`": {}}")
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out, 'notify', '--send')
    Rec 'T9-notify' $(if (Posted $nt) { 'OK ' + (Short (($nt -split "`n") | Where-Object { $_ -match 'posted:' })) } elseif ($nt -match 'another approvals run is in progress') { 'NG 鍵が取れず、何も投稿していません（他の処理が動いています。少し待って、やり直してください）' } else { 'NG ' + (Fails $nt) })
    if ($SelfReply) {
      if (Posted $nt) { [void](Auto-Reply "OK $base" "$base,$($base + 1)"); Start-Sleep -Seconds 2; [void](Auto-Reply "NG $($base + 1)" "$base,$($base + 1)") }
    } else {
    Write-Host ("   iPhone から「OK {0}」と「NG {1}」を別々に返信してください（最大 {2} 秒）" -f $base, ($base + 1), $WaitSec) -ForegroundColor Green
    }
    $acc = ''; $deadline = (Get-Date).AddSeconds($replyWait)
    while ((Get-Date) -lt $deadline -and -not ($acc -match 'approved' -and $acc -match 'rejected')) {
      Start-Sleep -Seconds 10
      $acc += Py @('-m', 'kimeru', '--out', $out, 'approvals')
    }
    $again = Py @('-m', 'kimeru', '--out', $out, 'approvals')
    $ok9 = $acc -match 'approved' -and $acc -match 'rejected'
    $shown = if ($acc -match 'failed: |Traceback|Error') { Fails $acc } else { Short $acc }
    Rec 'T9' ("{0} approvals={1} 2回目={2}" -f $(if ($ok9) { 'OK' } else { 'NG' }), $shown, $(if (-not $again.Trim()) { '（なし＝二重処理なし）' } elseif ($again -match 'failed: |Traceback') { Fails $again } else { Short $again }))
    AutoTag 'T9'
  } else { Rec 'T9' 'SKIP' }
}

# ---- T23: how long is the preview Teams gives? (read only; lengths only, never text) ----
# N5: the list is only read, so the automatic check runs T23 whether or not the self chat is open (T3 opens nothing there)
if ($uia -and ($selfOk -or $Auto) -and (Want 'T23')) {
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
    # the input-box check of this PC: after the read, wait for you to click a Teams compose box, then take `diag` (read only, it types nothing).
    # focus= holds the control type, the ProcessId of the focused element, the window that holds it (topPid), the window in front now
    # (fgPid) and the processes of Teams (teamsPids); the sheet gets the type, the two ids, whether they are Teams' own, and teamsInUse
    $focusSec = 8
    if ($env:KIMERU_T24_FOCUS_SEC -match '^\d+$') { $focusSec = [Math]::Min(60, [Math]::Max(3, [int]$env:KIMERU_T24_FOCUS_SEC)) }
    Write-Host ("   {0} 秒以内に Teams の入力欄（メッセージを書く欄）をクリックしてください。ポップアウトした窓で確かめるときは、その窓を前面にして、その入力欄をクリックします" -f $focusSec) -ForegroundColor Green
    Start-Sleep -Seconds $focusSec
    $dg = Self 'diag'
    Rec 'T24-diag' $(if ($dg.ok) { Focus-Numbers $dg } else { 'NG ' + (Short $dg.error) })
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
    $tgt = (Py @('-c', 'import json; from kimeru import config, pull; config.apply([]); o, p = pull.ado_names(config.value(''ado_org''), config.value(''ado_project'')); print(json.dumps({''org'': o, ''project'': p}))')).Trim()
    $origin = try { $tgt | ConvertFrom-Json } catch { $null }
    $t22text = "kimeru の試験です。このコメントは、承認のあとに 1 回だけ書かれます。`n件数 < 3 のとき <b> を使う、という文を含みます。"
    $rec = [ordered]@{ graph = 'check'; event_kind = 'ado.workitem.created'; event_id = "t22-$wi"; node = 'request_info'; outcome = 'decide'; needs_human = $true; advice = ''
      event = @{ origin = @{ org = [string]$origin.org; project = [string]$origin.project } }
      actions = @(@{ type = 'ado.comment'; id = $wi; text = $t22text }) }
    [IO.File]::WriteAllText((Join-Path $out22 'queue.jsonl'), (($rec | ConvertTo-Json -Compress -Depth 8) + "`n"), (New-Object Text.UTF8Encoding $false))
    $env:KIMERU_EXECUTE = 'ado.comment'
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out22, 'notify', '--send')
    if (-not (Posted $nt)) { Rec 'T22' ('NG 投稿できません ' + $(if ($nt -match 'another approvals run is in progress') { '（鍵が取れず、何も投稿していません）' } else { Fails $nt })) }
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
        # read back what ADO stored (read only; counts and true/false only): did it wrap the comment in tags, did it keep `<` as `&lt;`,
        # and did the same-text check of kimeru find it (a redo must not write it again)
        # the text has line breaks: it goes through a file, never through the command line (a .bat shim cuts an argument at a line break)
        $t22file = Join-Path $out22 't22text.txt'
        [IO.File]::WriteAllText($t22file, $t22text, (New-Object Text.UTF8Encoding $false))
        $rb = try { (Py @('-c', 'import json, sys; from kimeru import config, execute; config.apply([]); print(json.dumps(execute.inspect_comment(sys.argv[1], sys.argv[2], execute.comment_text(dict(text=open(sys.argv[3], encoding=''utf-8'').read())))))', $wi, 'kimeru の試験です', $t22file)) -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1 | ConvertFrom-Json } catch { $null }
        $shape = if ($rb -and $rb.PSObject.Properties['found']) { "読み返した件数=$($rb.found) タグで包まれた=$($rb.wrapped_in_tags) エスケープされた=$($rb.escaped) 同じ文面の確認が一致=$($rb.same_text_check)" } else { '読み返せず（az のサインインか、通信を確かめてください）' }
        $seen = YesNo "   ADO の作業項目 $wi に、kimeru のコメントが **1 件だけ** ありますか"
        $good = $seen -and $rb -and $rb.found -eq 1 -and $rb.same_text_check
        Rec 'T22' $(if ($good) { "OK 書かれた回数=1、ADO でも 1 件 $shape" } elseif ($seen) { "NG ADO には 1 件あるが、同じ文面の確認が一致しない、または読み返せない $shape" } else { "NG kimeru は 1 回書いたと記録したが、ADO で 1 件と確認できない $shape" })
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
  if (-not (Consent "   架空の確認待ち2件を自分とのチャットに送信します（宛先は自分だけ）。よろしいですか")) { Rec 'T20' 'SKIP' }
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
    if (-not (Posted $nt)) { Rec 'T20' ('NG 投稿できません ' + $(if ($nt -match 'another approvals run is in progress') { '（鍵が取れず、何も投稿していません）' } else { Fails $nt })) }
    else {
      if ($SelfReply) { [void](Auto-Reply "聞き返し $base" "$base,$($base + 1)") }   # 下書き carries a long text: it is not on the whitelist, so it is not typed
      else {
      Write-Host ("   Teams の自分とのチャットで、次の 2 つを別々に返信してください（最大 {0} 秒）" -f $WaitSec) -ForegroundColor Green
      Write-Host ("     聞き返し {0}" -f $base) -ForegroundColor Green
      Write-Host ("     下書き {0} ご連絡ありがとうございます。仕様を確認して、改めてご連絡します。" -f ($base + 1)) -ForegroundColor Green
      }
      $deadline = (Get-Date).AddSeconds($replyWait); $okA = $false; $okB = $false
      while ((Get-Date) -lt $deadline -and -not ($okA -and ($okB -or $SelfReply))) {
        Start-Sleep -Seconds 10
        [void](Py @('-m', 'kimeru', '--out', $out20, 'approvals'))
        try {
          $ap = Get-Content -Raw -Encoding UTF8 (Join-Path $out20 'approvals.json') | ConvertFrom-Json
          $ia = $ap.items.PSObject.Properties[[string]$base].Value; $ib = $ap.items.PSObject.Properties[[string]($base + 1)].Value
          $okA = ($ia.record.actions[0].variant -eq 'ask_back')
          $okB = ($ib.record.actions[0].drafted_by -like 'm365*')
        } catch {}
      }
      if ($SelfReply) { Rec 'T20' ("{0} 聞き返し={1} 下書き=未確認（自動の返信では打たない）" -f $(if ($okA) { '部分OK' } else { 'NG' }), $(if ($okA) { '読めた' } else { '読めない' })); AutoTag 'T20' }
      else { Rec 'T20' ("{0} 聞き返し={1} 下書き={2}" -f $(if ($okA -and $okB) { 'OK' } else { 'NG' }), $(if ($okA) { '読めた' } else { '読めない' }), $(if ($okB) { '読めた' } else { '読めない' })) }
    }
  }
}

# ---- T25 停止・再開の語（自分とのチャットの自分の行を読むだけ。打つのは人）----
# N4: my own line of the list is judged by control.line_of (the daily cycle's own judgment); when the list has no self row, the self chat's own
# timeline is read and judged by control.line_of_timeline, as kimeru does. Nothing is sent by the script.
if (Want 'T25') {
  Say "T25 停止・再開の語（一覧の自分の行を読み、一覧に無ければ自分とのチャットの中を読みます）"
  if ($Auto) { Rec 'T25' 'SKIP（自動: 自分とのチャットに人が打つ必要がある。run-check.cmd T25 で行う）' }
  elseif (-not $uia) { Rec 'T25' 'SKIP（組織ポリシーで画面操作不可）' }
  elseif (-not $py) { Rec 'T25' 'SKIP（Python なし）' }
  elseif ($SelfReply -and -not $selfOk) { Rec 'T25' 'SKIP（自分とのチャットが開けていないため。先に T3 を通してください）' }
  elseif (-not (Consent "   自分とのチャットに「停止」と「再開」を、あなたが 1 行ずつ打ちます（宛先は自分だけ。スクリプトは送りません）。よろしいですか")) { Rec 'T25' 'SKIP' }
  else {
    $ctlWait = 30; $found = @{}; $waited = @{}; $via = @{}; $row = 'なし'; $readErr = $null
    $n25 = Get-Random -Minimum 100 -Maximum 999
    if ($SelfReply) {   # the replies go under a test post of this run; 停止 is always followed by 再開
      $p25 = Self 'post' @('-Text', "[kimeru 試験 #$n25] 停止・再開の試験です")
      $s25 = if ($p25.ok -and $p25.typed) { Self 'send' } else { $p25 }
      if (-not $s25.ok) { $readErr = [string]$s25.error }
    }
    foreach ($word in @('停止', '再開')) {
      if ($SelfReply) { if (-not $readErr -and -not (Auto-Reply $word "$n25")) { $readErr = $script:ReplyErr } }
      else {
      Write-Host ("   自分とのチャットに「{0}」とだけ打って Teams で送信し、この画面で Enter を押してください（最大 {1} 秒、一覧を読みます）" -f $word, $ctlWait) -ForegroundColor Green
      [void](Read-Host '   ')
      }
      $began = Get-Date; $hit = $false
      while (-not $hit -and -not $readErr -and ((Get-Date) - $began).TotalSeconds -lt $ctlWait) {
        $c = Self 'chats'
        if (-not $c.ok) { $readErr = [string]$c.error; break }
        $selfRows = @($c.chats | Where-Object { $_.kind -eq 'self' } | ForEach-Object { [ordered]@{ kind = 'self'; preview = [string]$_.preview } })
        $row = if ($selfRows.Count) { 'あり' } else { 'なし' }
        $hit = (Control-Word $selfRows).word -eq $word
        if ($hit) { $via[$word] = '一覧' }
        elseif (-not $selfRows.Count) {   # no self row in the list: the self chat's own timeline
          $t25 = Self 'read'
          if ($t25.ok) { $hit = (Control-Word $null @($t25.timeline)).word -eq $word; if ($hit) { $via[$word] = '自分とのチャット' } }
        }
        if (-not $hit) { Start-Sleep -Seconds 5 }
      }
      $found[$word] = $hit
      $waited[$word] = [int]((Get-Date) - $began).TotalSeconds
    }
    if ($readErr) { Rec 'T25' ('NG ' + $readErr) }
    else { $ok = [bool]($found['停止'] -and $found['再開']); Rec 'T25' ("{0} 停止={1}（待った{2}秒） 再開={3}（待った{4}秒） 自分の行={5} 読んだ所={6}" -f $(if ($ok) { 'OK' } else { 'NG' }), $(if ($found['停止']) { '認識' } else { '未認識' }), $waited['停止'], $(if ($found['再開']) { '認識' } else { '未認識' }), $waited['再開'], $row, $(if ($via['停止']) { $via['停止'] } elseif ($via['再開']) { $via['再開'] } else { 'なし' })); AutoTag 'T25' }
  }
}

# ---- T26 送った文が一覧の自分の行に出るか（相手のチャットは開きません。一覧の「あなた:」の行だけを読みます）----
# N4: my rows in other chats are classified by sent.classify (as_is, edited, other). A row's text is never printed or kept.
if (Want 'T26') {
  Say "T26 送った文が一覧の自分の行に出るか（相手のチャットは開きません。一覧の「あなた:」の行だけを読みます）"
  if ($Auto) { Rec 'T26' 'SKIP（自動: 1 対 1 チャットへの送信は人が行う。run-check.cmd T26 で行う）' }
  elseif (-not $uia) { Rec 'T26' 'SKIP（組織ポリシーで画面操作不可）' }
  elseif (-not $py) { Rec 'T26' 'SKIP（Python なし）' }
  else {
    $n = Get-Random -Minimum 100 -Maximum 999
    $sendText = "kimeru 試験 $n の送信です"
    Write-Host "   どれかの 1 対 1 チャットに、次の文を 1 通だけ、そのまま送ってください（相手に届く文です。送るのはあなたです）:" -ForegroundColor Green
    Write-Host ("     {0}" -f $sendText) -ForegroundColor Green
    if (-not (YesNo "   送り終えましたか？（N なら SKIP。このあとは一覧を読むだけです）")) { Rec 'T26' 'SKIP' }
    else {
      $began = Get-Date; $readErr = $null; $cls = $null; $ok = $false
      Start-Sleep -Seconds 5
      while (-not $ok -and -not $readErr -and ((Get-Date) - $began).TotalSeconds -lt 60) {
        $c = Self 'chats'
        if (-not $c.ok) { $readErr = [string]$c.error; break }
        $shown = @($c.chats | Where-Object { $_.kind -ne 'self' -and [string]$_.preview -match '^\s*(あなた|You)\s*:' } | ForEach-Object { [string]$_.preview -replace '^\s*(あなた|You)\s*:\s*', '' })
        $cls = Sent-Classes $sendText $shown
        $ok = [bool]($cls -and [int]$cls.as_is -ge 1)
        if (-not $ok) { Start-Sleep -Seconds 10 }
      }
      $waited = [int]((Get-Date) - $began).TotalSeconds
      if ($readErr) { Rec 'T26' ('NG ' + $readErr) }
      elseif (-not $cls) { Rec 'T26' 'NG 分類できず（Python の出力を読めない）' }
      else { $tot = [int]$cls.n; $asIs = [int]$cls.as_is; $edited = [int]$cls.edited; $other = [int]$cls.other; Rec 'T26' ("{0} 件数={1} as_is={2} edited={3} other={4} 待った={5}秒" -f $(if ($ok) { 'OK' } else { 'NG' }), $tot, $asIs, $edited, $other, $waited) }
    }
  }
}

# ---- T27 返信の語を読み手が読めるか（自分とのチャットに試験の投稿 1 通。承認の状態は変えません）----
# N4: the post starts with the test mark, so the real reader never takes a reply below it for an approval. Only the reader (read) is used.
if (Want 'T27') {
  Say "T27 返信の語を読み手が読めるか（試験の投稿の下に打つ語。承認の状態は変えません）"
  if ($Auto) { Rec 'T27' 'SKIP（自動: 自分とのチャットに投稿し、人が打つ必要がある。run-check.cmd T27 で行う）' }
  elseif (-not $uia -or -not $selfOk) { Rec 'T27' 'SKIP（自分とのチャットが開けていないため。先に T3 を通してください）' }
  elseif (-not (Consent "   自分とのチャットに試験の投稿を 1 通送信します（宛先は自分だけ。承認の投稿ではありません）。よろしいですか")) { Rec 'T27' 'SKIP' }
  else {
    $n = Get-Random -Minimum 100 -Maximum 999
    $p27 = Self 'post' @('-Text', "[kimeru 試験 #$n] 返信の試験です。次の 7 行を、自分とのチャットに 1 行ずつ打ってください")
    $s27 = if ($p27.ok -and $p27.typed) { Self 'send' } else { $p27 }
    if (-not $s27.ok) { $readErr = [string]$s27.error; Rec 'T27' ('NG ' + $readErr) }
    else {
      $want = @("R:違う $n", "R:保留 $n 明日", "R:不要 $n", "R:着手 $n", "R:完了 $n 1", "R:延期 $n 明日", "R:送った $n")
      $words = @('違う', '保留', '不要', '着手', '完了', '延期', '送った')
      if ($SelfReply) {
        foreach ($line in @($want | ForEach-Object { $_.Substring(2) })) { [void](Auto-Reply $line "$n"); Start-Sleep -Seconds 2 }
      } else {
      Write-Host "   上の投稿の下に、次の 7 行を、自分とのチャットに 1 行ずつ打って送ってください（最大 $WaitSec 秒。読めたものから数えます）:" -ForegroundColor Green
      foreach ($line in @($want | ForEach-Object { $_.Substring(2) })) { Write-Host ("     {0}" -f $line) -ForegroundColor Green }
      }
      $began = Get-Date; $tl = @(); $at = -1; $readErr = $null
      while ((Get-Date) -lt $began.AddSeconds($replyWait)) {
        $r = Self 'read'
        if (-not $r.ok) { $readErr = [string]$r.error; break }
        if ($r.timeline) {
          $tl = @($r.timeline)
          $at = [array]::IndexOf($tl, "T:$n")
          if ($at -ge 0 -and @($want | Where-Object { [array]::IndexOf($tl, $_) -gt $at }).Count -eq $want.Count) { break }
        }
        Start-Sleep -Seconds 10
      }
      $hits = 0; $missing = @()
      for ($i = 0; $i -lt $want.Count; $i++) {
        if ($at -ge 0 -and [array]::IndexOf($tl, $want[$i]) -gt $at) { $hits++ } else { $missing += $words[$i] }
      }
      $waited = [int]((Get-Date) - $began).TotalSeconds
      $posted = if ($at -ge 0) { 'あり' } else { 'なし' }
      $missTxt = if ($missing.Count) { $missing -join '・' } else { 'なし' }
      if ($readErr) { Rec 'T27' ('NG ' + $readErr) }
      else { Rec 'T27' ("{0} 投稿={1} 分類={2}/{3} 分類されなかった語={4} 待った={5}秒" -f $(if ($hits -eq $want.Count) { 'OK' } else { 'NG' }), $posted, $hits, $want.Count, $missTxt, $waited); AutoTag 'T27' }
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
  if ($LASTEXITCODE -ne 0) { Rec 'T10' 'NG az login できず（組織の設定で拒否された可能性）' }
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
elseif (-not $env:TYPESAFE_API_KEY) { Rec 'T11' 'SKIP（TYPESAFE_API_KEY 未設定）' }
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
    Rec 'T12' ("{5} chats={0} [{1}] title+preview={2} unread={3} mention={4}" -f $cs.Count, $kinds, $withText,
      @($cs | Where-Object unread).Count, @($cs | Where-Object mention).Count, $(if ($cs.Count) { 'OK' } else { 'NG' }))
    if (-not $cs.Count) {   # an empty list is not a success: keep the content-free layout to see why (Teams on the Chat tab?)
      $d = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action diag 2>&1 | Out-String
      Rec 'T12-diag' (Safe-Diag $d)
    }
  } catch { Rec 'T12' ('NG ' + (Safe-Error $c)) }
}

# ---- T13: can Kev (local, Jev-compatible model) run here? (no downloads) ----
if (Want 'T13') {
  Say "T13 ローカル判断モデル（kev）の事前チェック（ダウンロードなし）"
  $cs = Get-CimInstance Win32_ComputerSystem; $cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1)
  $free = [math]::Round((Get-PSDrive C).Free / 1GB)
  $vc = Test-Path "$env:SystemRoot\System32\vcruntime140.dll"
  # the reachability probe is the only part of T13 that leaves the PC: ask first (No keeps the rest of the line, net: SKIP)
  $net = if (YesNo "   外部の 3 サイト（pypi.org・github.com・huggingface.co）へ到達確認（HEAD。本文・名前・キーは送りません）を送ります。よろしいですか") {
    foreach ($u in 'https://pypi.org/simple/', 'https://github.com', 'https://huggingface.co') {
      try { $null = Invoke-WebRequest $u -Method Head -UseBasicParsing -TimeoutSec 8; ($u -replace 'https://|/.*$', '') + '=OK' }
      catch { ($u -replace 'https://|/.*$', '') + '=NG' }
    }
  } else { 'SKIP' }
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
    $script:IsolatePy = $true
    $k = Py @('-m', 'kimeru', '--backend', 'kev', '--out', (Join-Path $tmp 'kev'), 'run', 'examples\teams_chat.json')
    $script:IsolatePy = $false
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
    $c = ((Py @('-c', 'from kimeru import writer; print(writer.find_exe(''copilot'', ''KIMERU_COPILOT_EXE'') or '''')')) -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
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
$noRestPrev = $env:KIMERU_M365_NO_REST   # T18 turns the REST route off; the value is put back when T18 ends (T16 and T17 must not see it)
try {
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
} finally {
  if ($null -eq $noRestPrev) { Remove-Item Env:KIMERU_M365_NO_REST -ErrorAction SilentlyContinue } else { $env:KIMERU_M365_NO_REST = $noRestPrev }
}

# ---- T16: Windows notification on this PC (self-chat posts never notify your own devices) ----
if (Want 'T16') {
  Say "T16 PC への通知（Windows の通知を 1 件出します。クリックすると Teams の自分とのチャットが開きます）"
  $o = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'toast.ps1') -Title 'kimeru: テスト通知' -Body '確認待ちができるとこの通知が出ます' 2>&1 | Out-String
  $tj = $null; try { $tj = $o.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json } catch {}
  if ($o -notmatch '"ok":true') { Rec 'T16' ('NG ' + (Safe-Error $o)) }
  else {
    $why = if ($tj) { "設定=$($tj.setting) 通知センター内=$($tj.inCenter)件" } else { '' }
    if ($Auto) { Rec 'T16' "OK 通知を出した（表示は自動では確かめない） $why" }
    else {
    $seen = YesNo "   画面右下に「kimeru: テスト通知」が出ましたか（集中モード中は通知センターに入ります）"
    Rec 'T16' $(if ($seen) { "OK 通知が表示された $why" } else { "NG 通知が見えない $why（DisabledForUser=設定または集中モード / DisabledByGroupPolicy=組織の設定。通知センターに 1 件以上あれば、届いているが画面には出ていない）" })
    }
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

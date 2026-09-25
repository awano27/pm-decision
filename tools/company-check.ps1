<#
.SYNOPSIS
  One-shot company PC check for kimeru (run via run-company-check.cmd).
  Runs every level of docs/company-pc-test.md automatically, asks only when a human
  is required (sending to your own self chat, iPhone replies, opening a screen, az / Jev),
  and writes a result sheet (no message text, names or tokens) to the clipboard and
  kimeru-check-result.txt.
#>
param([int]$WaitSec = 180)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$Results = [ordered]@{}   # not $R: PowerShell names are case-insensitive ($r is used below)
$tmp = Join-Path $env:TEMP ("kimeru-check-" + (Get-Random -Minimum 10000 -Maximum 99999))
New-Item -ItemType Directory -Force $tmp | Out-Null

function Say($t) { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Rec($k, $v) { $Results[$k] = $v; Write-Host ("   {0}: {1}" -f $k, $v) -ForegroundColor Yellow }
function Short($s) { $x = ([string]$s -replace '\s+', ' ').Trim(); if ($x.Length -gt 160) { $x.Substring(0, 160) } else { $x } }
function YesNo($q) { (Read-Host "$q [y/N]") -match '^\s*([yYｙＹ]|はい)' }   # tolerate stray keys after y ("y[")
function Self($action, [string[]]$extra = @()) {
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action $action @extra 2>&1 | Out-String
  try { return ($out.Trim().TrimStart([char]0xFEFF) | ConvertFrom-Json) } catch { return [pscustomobject]@{ ok = $false; error = (Short $out) } }
}
function Probe($label) {
  $o = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'probe-teams.ps1') -Label $label 2>&1 | Out-String
  $f = Join-Path $root "probe-$label.txt"
  if (-not (Test-Path $f)) { return @{ ok = $false; error = (Short $o) } }
  $lines = Get-Content $f -Encoding UTF8
  $n = if ($lines[0] -match 'elements=(\d+)') { [int]$Matches[1] } else { 0 }
  $i = [array]::IndexOf($lines, 'copilot/recap-like UI labels:')
  $labels = 0
  if ($i -ge 0) { for ($j = $i + 1; $j -lt $lines.Count -and $lines[$j].StartsWith('  '); $j++) { $labels++ } }
  $types = ($lines | Where-Object { $_ -like 'types:*' } | Select-Object -First 1)
  Remove-Item $f -ErrorAction SilentlyContinue   # may contain names; only counts are kept
  return @{ ok = ($n -ge 100); elements = $n; labels = $labels; types = (Short $types) }
}
function WaitReply($id, $word) {
  $deadline = (Get-Date).AddSeconds($WaitSec)
  while ((Get-Date) -lt $deadline) {
    $r = Self 'read'
    if ($r.ok -and $r.timeline) {
      $tl = @($r.timeline); $last = -1
      for ($i = 0; $i -lt $tl.Count; $i++) { if ($tl[$i] -eq "P:$id") { $last = $i } }
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

  Say "T1 チャット画面の読み取り"
  $p = Probe 'chat'
  Rec 'T1' $(if ($p.ok) { "OK elements=$($p.elements) $($p.types)" } else { "NG elements=$($p.elements) $($p.error)" })

  Say "T2 Copilot 会議まとめ画面の読み取り"
  Write-Host "   Teams で Copilot のまとめ（要約）タブを表示してから Enter。無ければ s + Enter でスキップ"
  if ((Read-Host) -ne 's') {
    $p = Probe 'recap'
    Rec 'T2' $(if ($p.ok) { "OK elements=$($p.elements) recap系ラベル=$($p.labels)" } else { "NG elements=$($p.elements) $($p.error)" })
  } else { Rec 'T2' 'SKIP' }

  Say "T3 自分とのチャット"
  $s = Self 'open'; $r = Self 'read'
  $selfOk = [bool]($s.ok -and $r.ok)
  Rec 'T3' $(if ($selfOk) { "OK open/read（timeline=$(@($r.timeline).Count)件）" } else { "NG $($s.error) $($r.error)" })
  if (-not $selfOk) {
    # names are masked by diag; this tells us which marker the work account uses
    $d = & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'teams-self.ps1') -Action diag 2>&1 | Out-String
    Rec 'T3-diag' (Short $d)
  }

  if ($selfOk) {
    Say "T4/T5 貼り付け・送信・iPhone 返信"
    if (YesNo "   自分とのチャットにテストメッセージを1通送信します（宛先は自分だけ）。よろしいですか") {
      $n = Get-Random -Minimum 100 -Maximum 999
      Write-Host "   数秒間マウス・キーボードに触らないでください"
      $p1 = Self 'post' @('-Text', "[kimeru #$n] 会社PCテスト\n返信: OK $n")
      $s1 = if ($p1.ok -and $p1.typed) { Self 'send' } else { $p1 }
      if ($s1.ok) {
        Write-Host "   会社の iPhone の Teams で、自分とのチャットに「OK $n」と返信してください（最大 $WaitSec 秒待ちます）" -ForegroundColor Green
        $got = WaitReply $n 'OK'
        $push = Read-Host "   iPhone に通知は届きましたか？ [y/n]"
        Rec 'T4' 'OK'
        Rec 'T5' ("送信=OK iPhone通知={0} 返信読取={1}" -f $(if ($push -match '^y') { 'あり' } else { 'なし' }), $(if ($got) { 'OK' } else { 'NG(タイムアウト)' }))
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
  Say "T7 テストと検証"
  $t = Py @('-m', 'unittest', '-q'); $tOk = $LASTEXITCODE -eq 0
  $v = Py @('-m', 'kimeru', 'validate'); $vOk = $LASTEXITCODE -eq 0
  Rec 'T7' $(if ($tOk -and $vOk) { "OK " + (Short (($t -split "`n") | Where-Object { $_ -match '^Ran ' })) } else { "NG " + (Short (($t + $v) -split "`n" | Select-Object -Last 8)) })

  Say "T8 サンプルで判断と朝のまとめ"
  $out = Join-Path $tmp 'out'
  $ex = @('examples\teams_chat.json', 'examples\monitor_alert.json', 'examples\ado_workitem.json', 'examples\meeting_minutes.json')
  $o = Py (@('-m', 'kimeru', '--out', $out, 'run') + $ex); $rOk = $LASTEXITCODE -eq 0
  $b = Py @('-m', 'kimeru', '--out', $out, 'brief')
  $nDec = ([regex]::Matches($o, '(?m)^\[')).Count
  Rec 'T8' $(if ($rOk -and $b -match 'kimeru brief') { "OK 判断=$nDec plan=$(([regex]::Matches($o, 'plan:')).Count)" } else { "NG " + (Short $o) })

  Say "T9 承認の流れ（iPhone から OK / NG）"
  if ($selfOk -and (YesNo "   確認待ち2件を自分とのチャットに送信します（宛先は自分だけ）。よろしいですか")) {
    $base = Get-Random -Minimum 100 -Maximum 899
    [IO.File]::WriteAllText((Join-Path $out 'approvals.json'), "{`"next`": $base, `"items`": {}}")
    Write-Host "   数秒間マウス・キーボードに触らないでください"
    $nt = Py @('-m', 'kimeru', '--out', $out, 'notify', '--send')
    Write-Host ("   iPhone から「OK {0}」と「NG {1}」を別々に返信してください（最大 {2} 秒）" -f $base, ($base + 1), $WaitSec) -ForegroundColor Green
    $acc = ''; $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline -and -not ($acc -match 'approved' -and $acc -match 'rejected')) {
      Start-Sleep -Seconds 10
      $acc += Py @('-m', 'kimeru', '--out', $out, 'approvals')
    }
    $again = Py @('-m', 'kimeru', '--out', $out, 'approvals')
    Rec 'T9' ("{0} approvals={1} 2回目={2}" -f $(if ($acc -match 'approved' -and $acc -match 'rejected') { 'OK' } else { 'NG' }), (Short $acc), $(if ($again.Trim()) { Short $again } else { '（なし＝二重処理なし）' }))
  } else { Rec 'T9' 'SKIP' }
}

# ---- Level 2: Azure CLI ----
Say "T10 Azure CLI で取り込み"
$az = Get-Command az -ErrorAction SilentlyContinue
if (-not $az -or -not $py) { Rec 'T10' $(if (-not $az) { 'SKIP az なし' } else { 'SKIP Python なし' }) }
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
Say "T11 Jev で判断（架空のサンプルのみ送信）"
if ($py -and (YesNo "   Jev（社外クラウド）に架空のサンプル1件を送信して判断させますか")) {
  $sec = Read-Host "   TypeSafe API キー（入力は表示されません。Enter でスキップ）" -AsSecureString
  $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  if ($key) {
    $env:TYPESAFE_API_KEY = $key
    $j = Py @('-m', 'kimeru', '--backend', 'jev', '--out', (Join-Path $tmp 'jev'), 'run', 'examples\teams_chat.json')
    Remove-Item Env:TYPESAFE_API_KEY; $key = $null
    Rec 'T11' $(if ($j -match 'plan:') { 'OK ' + (Short (($j -split "`n") | Where-Object { $_ -match 'path:' } | Select-Object -First 1)) } else { 'NG ' + (Short ($j -replace '[A-Za-z0-9_\-]{24,}', '<redacted>')) })
  } else { Rec 'T11' 'SKIP' }
} else { Rec 'T11' 'SKIP' }

# ---- Result ----
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
$sheet = @("kimeru 会社PCチェック結果 $(Get-Date -Format 'yyyy-MM-dd HH:mm')") + ($Results.GetEnumerator() | ForEach-Object { "{0,-6} {1}" -f $_.Key, $_.Value })
$sheet | Out-File -Encoding utf8 (Join-Path $root 'kimeru-check-result.txt')
try { $sheet -join "`r`n" | Set-Clipboard; $clip = 'クリップボードにコピーしました' } catch { $clip = 'kimeru-check-result.txt を開いてコピーしてください' }
Say "結果（$clip）"
$sheet | ForEach-Object { Write-Host $_ }

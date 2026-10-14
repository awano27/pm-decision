<#
  Test double for tools/teams-self.ps1: same parameters and JSON output, but the
  "self chat" is a JSON file ($env:KIMERU_FAKE_CHAT) instead of the Teams window.
#>
param(
  [Parameter(Mandatory = $true)][string]$Action,
  [string]$Text = '',
  [string]$ChatId = '',
  [string]$Preview = '',
  [int]$Count = 5,
  [switch]$Send
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$path = $env:KIMERU_FAKE_CHAT
$chat = if (Test-Path $path) { Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json } else { [pscustomobject]@{ messages = @() } }
function Out-Json($o) { $o | ConvertTo-Json -Compress -Depth 5 }
function Save { $chat | ConvertTo-Json -Depth 5 | Set-Content -Path $path -Encoding UTF8 }

switch ($Action) {
  'post' {
    if (-not $Text.StartsWith('[kimeru')) { Out-Json @{ ok = $false; error = 'refusing' }; exit 2 }
    $Text = $Text -replace '\\n', "`r`n"
    $messageId = ''
    if ($Send) {
      $chat.messages = @($chat.messages) + $Text
      $messageId = 'fake-' + $chat.messages.Count
      Save
    }
    Out-Json @{ ok = $true; typed = $true; sent = [bool]$Send
                readback = @{ matched = [bool]$Send; message_id = $messageId } }
  }
  'read' {
    # like the real screen: only the last KIMERU_FAKE_TAIL messages are visible, and consecutive identical lines are folded into one
    $msgs = @($chat.messages)
    $tail = [int]$env:KIMERU_FAKE_TAIL
    if ($tail -gt 0 -and $msgs.Count -gt $tail) { $msgs = @($msgs | Select-Object -Last $tail) }
    $tl = New-Object System.Collections.Generic.List[string]
    foreach ($m in $msgs) {
      $first = ($m -split "`r?`n")[0].Trim()
      $c = $null
      if ($first -match '^\[kimeru #(\d+)\]') { $c = "P:" + $Matches[1] }
      elseif ($first -match '^\[kimeru 詳細 #(\d+)\]') { $c = "D:" + $Matches[1] }
      elseif ($first -match '^\[kimeru 未送信((?:\s+#\d+)+)\]') { $nums = @([regex]::Matches($Matches[1], '\d+') | ForEach-Object { $_.Value }); $c = 'U:' + ($nums -join ',') }
      elseif ($first -match '^\[kimeru 期限((?:\s+#\d+)+)\]') { $nums = @([regex]::Matches($Matches[1], '\d+') | ForEach-Object { $_.Value }); $c = 'K:' + ($nums -join ',') }
      elseif ($first -match '^\[kimeru 試験 #(\d+)\]') { $c = "T:" + $Matches[1] }
      elseif ($first -match '^\[kimeru 試験 実行 #(\d+)(?:\s+(\d+))?\]') { $c = 'TX:{0}:{1}' -f $Matches[1], $(if ($Matches[2]) { [int]$Matches[2] } else { 0 }) }
      elseif ($first -match '^\[kimeru 確認 S(\d+)\]') { $c = 'S:' + $Matches[1] }
      elseif ($first -match '^\[kimeru 実行 #(\d+)(?:\s+(\d+))?\]') { $c = 'X:{0}:{1}' -f $Matches[1], $(if ($Matches[2]) { [int]$Matches[2] } else { 0 }) }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^再実行\s*#?(\d+)\s*[-ー−‐‑‒–—―─ｰ]\s*(\d+)\s*[.。!！]*$') { $c = 'R:再実行 {0}-{1}' -f $Matches[1], $Matches[2] }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^再実行\s*#?(\d+)(\s*[^\d\s.。!！]{1,6}\s*|\s+)(\d*)\s*[.。!！]*$') { $c = 'R:再実行形式 {0} {1}' -f $Matches[1], (($Matches[2] + $Matches[3]).Trim()) }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^(?i)(OK|NG|保留|聞き返し|詳細|再実行|済|不要)\s*#?(\d+)$') { $c = 'R:{0} {1}' -f $Matches[1].ToUpper(), $Matches[2] }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^(?i)保留\s*#?(\d+)\s+(\S{1,10})$') { $c = 'R:保留 {0} {1}' -f $Matches[1], $Matches[2] }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^(着手|完了)\s*#?(\d+)(?:\s+(\d+))?\s*[.。!！]*$') { $c = if ($Matches[3]) { 'R:{0} {1} {2}' -f $Matches[1], $Matches[2], $Matches[3] } else { 'R:{0} {1}' -f $Matches[1], $Matches[2] } }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^延期\s*#?(\d+)\s+(\S{1,10})\s*[.。!！]*$') { $c = 'R:延期 {0} {1}' -f $Matches[1], $Matches[2].TrimEnd('.', '。', '!', '！') }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^違う\s*#?(\d+)(?:\s+(\d+))?$') { $c = if ($Matches[2]) { 'R:違う {0} {1}' -f $Matches[1], $Matches[2] } else { 'R:違う {0}' -f $Matches[1] } }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^(正しい|違う)\s*S(\d+)$') { $c = 'R:{0} S{1}' -f $Matches[1], $Matches[2] }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^送った\s*#?(\d+)\s*[.。!！]*$') { $c = 'R:送った {0}' -f $Matches[1] }
      if ($c -and ($tl.Count -eq 0 -or $tl[$tl.Count - 1] -ne $c)) { $tl.Add($c) }
    }
    Out-Json @{ ok = $true; posts = @(); replies = @(); timeline = @($tl) }
  }
  'status' {
    # N2: KIMERU_FAKE_TEAMS = window (the default) | tray (the process runs, no window on screen) | none (not running)
    $st = if ($env:KIMERU_FAKE_TEAMS) { $env:KIMERU_FAKE_TEAMS } else { 'window' }
    $win = [bool]($st -eq 'window')
    Out-Json @{ ok = $true; teams = $win; teams_process = [bool]($st -ne 'none'); teams_window = $win; selfChatOpen = $win }
  }
  'chats' {
    if ($env:KIMERU_FAKE_TEAMS -in 'tray', 'none') { Out-Json @{ ok = $false; error = 'Teams window not found' }; exit 2 }
    $f = $env:KIMERU_FAKE_CHATS
    $rows = if ($f -and (Test-Path $f)) { @((Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json).chats) } else { @() }
    Out-Json @{ ok = $true; chats = @($rows | ForEach-Object { @{ id = $_.id; kind = $_.kind; title = $_.title; preview = $_.preview; time = $_.time; unread = [bool]$_.unread; mention = [bool]$_.mention } }) }
  }
  'readchat' {
    $f = $env:KIMERU_FAKE_CHATS
    $c = if ($f -and (Test-Path $f)) { @((Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json).chats) | Where-Object { $_.id -eq $ChatId } | Select-Object -First 1 } else { $null }
    if (-not $c) { Out-Json @{ ok = $false; error = 'the chat is not in the list on screen; nothing was opened' }; exit 2 }
    $log = $env:KIMERU_FAKE_OPENLOG
    if ($log) { Add-Content -Path $log -Value $ChatId -Encoding UTF8 }
    # a chat entry may say how putting the original chat back went ("restore": restored | self | failed) or that reading fails ("error")
    $rs = if ($c.restore) { [string]$c.restore } else { 'restored' }
    $ret = [bool]($rs -eq 'restored')
    if ($c.error) { Out-Json @{ ok = $false; error = [string]$c.error; restore = $rs; returned = $ret; hadOriginal = $true }; exit 2 }
    Out-Json @{ ok = $true; opened = $true; how = 'fake'; messages = @(@($c.messages) | Select-Object -Last $Count | ForEach-Object { @{ text = $_; sender = ''; time = '' } }); returned = $ret; restore = $rs; hadOriginal = $true; preview = $Preview }
  }
  default { Out-Json @{ ok = $false; error = "unsupported $Action" }; exit 2 }
}

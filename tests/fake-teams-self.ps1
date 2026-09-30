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
    if ($Send) { $chat.messages = @($chat.messages) + $Text; Save }
    Out-Json @{ ok = $true; typed = $true; sent = [bool]$Send }
  }
  'read' {
    $tl = New-Object System.Collections.Generic.List[string]
    foreach ($m in @($chat.messages)) {
      $first = ($m -split "`r?`n")[0].Trim()
      if ($first -match '^\[kimeru #(\d+)\]') { $tl.Add("P:" + $Matches[1]) }
      elseif ($first.Normalize([Text.NormalizationForm]::FormKC) -match '^(?i)(OK|NG|保留)\s*#?(\d+)$') { $c = 'R:{0} {1}' -f $Matches[1].ToUpper(), $Matches[2]; $tl.Add($c) }
    }
    Out-Json @{ ok = $true; posts = @(); replies = @(); timeline = @($tl) }
  }
  'chats' {
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

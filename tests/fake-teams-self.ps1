<#
  Test double for tools/teams-self.ps1: same parameters and JSON output, but the
  "self chat" is a JSON file ($env:KIMERU_FAKE_CHAT) instead of the Teams window.
#>
param(
  [Parameter(Mandatory = $true)][string]$Action,
  [string]$Text = '',
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
  default { Out-Json @{ ok = $false; error = "unsupported $Action" }; exit 2 }
}

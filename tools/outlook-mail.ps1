<#
.SYNOPSIS
  Send a short mail to yourself through the desktop Outlook (COM). Used by `kimeru` push route "outlook".
  The recipient is NOT a parameter: it is always the address Outlook is signed in with (Session.CurrentUser),
  so no setting can send this mail to anyone else. The recipient is added by that SMTP ADDRESS (never by display name: a
  contact with the same name could win), and the address it resolved to must equal the signed-in user's own or nothing is sent. It sends exactly the -Body it is given (a line with counts and
  numbers; never message text). Needs the desktop Outlook (classic) signed in; nothing else is installed or changed.

  Usage: outlook-mail.ps1 -Subject <text> -Body <text>
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [Parameter(Mandatory = $true)][string]$Subject,
  [Parameter(Mandatory = $true)][string]$Body
)
$ErrorActionPreference = 'Stop'
try {
  $ol = New-Object -ComObject Outlook.Application
  $me = $ol.Session.CurrentUser
  if (-not $me) { throw 'no signed-in user' }
  function Get-Smtp($entry) {
    if (-not $entry) { return $null }
    if ($entry.Type -eq 'EX') { $ex = $entry.GetExchangeUser(); if ($ex) { return [string]$ex.PrimarySmtpAddress }; return $null }
    return [string]$entry.Address
  }
  $mine = Get-Smtp $me.AddressEntry
  if (-not $mine -or $mine -notmatch '^[^@\s]+@[^@\s]+$') { throw 'own address unknown' }
  $mail = $ol.CreateItem(0)
  $r = $mail.Recipients.Add($mine)           # by address, not by name
  $r.Resolve() | Out-Null
  if (-not $r.Resolved) { throw 'own address not resolved' }
  if ($mail.Recipients.Count -ne 1) { throw 'unexpected recipients' }
  $got = Get-Smtp $r.AddressEntry
  if (-not $got -or ($got -ne $mine)) { throw 'resolved address is not your own' }   # -ne ignores case
  $mail.Subject = $Subject
  $mail.Body = $Body
  $mail.Send()
  '{"ok":true,"verified":true}'
} catch {
  '{"ok":false}'
  exit 2
}

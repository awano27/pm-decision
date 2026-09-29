<#
.SYNOPSIS
  Send a short mail to yourself through the desktop Outlook (COM). Used by `kimeru` push route "outlook".
  It sends exactly the -Body it is given (a line with counts and numbers; never message text).
  Needs the desktop Outlook (classic) signed in; nothing else is installed or changed.

  Usage: outlook-mail.ps1 -To <address> -Subject <text> -Body <text>
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [Parameter(Mandatory = $true)][string]$To,
  [Parameter(Mandatory = $true)][string]$Subject,
  [Parameter(Mandatory = $true)][string]$Body
)
$ErrorActionPreference = 'Stop'
try {
  $ol = New-Object -ComObject Outlook.Application
  $mail = $ol.CreateItem(0)
  $mail.To = $To
  $mail.Subject = $Subject
  $mail.Body = $Body
  $mail.Send()
  '{"ok":true}'
} catch {
  '{"ok":false}'
  exit 2
}

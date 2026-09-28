<#
.SYNOPSIS
  Show a Windows notification (toast) on this PC. Built-in WinRT only: no module, no admin rights,
  nothing leaves the PC. Clicking it opens the Teams chat with yourself (msteams: deep link).

  Usage: toast.ps1 -Title <text> -Body <text> [-Link <uri>]
#>
[CmdletBinding(PositionalBinding = $false)]
param(
  [Parameter(Mandatory = $true)][string]$Title,
  [string]$Body = '',
  [string]$Link = 'msteams:/l/chat/48:notes/conversations'
)
$ErrorActionPreference = 'Stop'
function Esc($s) { [Security.SecurityElement]::Escape([string]$s) }
try {
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
  $xml.LoadXml(@"
<toast activationType="protocol" launch="$(Esc $Link)" scenario="reminder">
  <visual><binding template="ToastGeneric"><text>$(Esc $Title)</text><text>$(Esc $Body)</text></binding></visual>
  <actions><action content="Teams で開く" activationType="protocol" arguments="$(Esc $Link)"/><action content="閉じる" activationType="system" arguments="dismiss"/></actions>
</toast>
"@)
  # Windows PowerShell's own app id: a registered AUMID, so no shortcut or registration is needed
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show((New-Object Windows.UI.Notifications.ToastNotification $xml))
  '{"ok":true}'
} catch {
  '{"ok":false,"error":' + (ConvertTo-Json ([string]$_.Exception.Message)) + '}'
  exit 2
}

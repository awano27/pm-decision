# Dot-sourced by the scripts that check whether the Kev on this PC answers. start-kev.cmd makes a key once per Windows
# user (%LOCALAPPDATA%\kev\api-key) and Kev then refuses a request without it, so the check sends it too
# (KEV_API_KEY first, as kimeru does; the file named by KIMERU_KEV_KEY_FILE replaces the default file).
function Get-KevHeaders {
  $key = $env:KEV_API_KEY
  if (-not $key) {
    $file = if ($env:KIMERU_KEV_KEY_FILE) { $env:KIMERU_KEV_KEY_FILE } elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'kev\api-key' } else { '' }
    if ($file -and (Test-Path -LiteralPath $file)) { $key = ([string](Get-Content -LiteralPath $file -TotalCount 1)).Trim() }
  }
  if ($key) { @{ Authorization = "Bearer $key" } } else { @{} }
}
function Test-KevUp([int]$TimeoutSec = 5) {
  try { $null = Invoke-WebRequest 'http://127.0.0.1:8009/v1/models' -Headers (Get-KevHeaders) -UseBasicParsing -TimeoutSec $TimeoutSec; $true } catch { $false }
}

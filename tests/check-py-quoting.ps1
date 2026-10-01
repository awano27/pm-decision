# Runs the `-c` programs of tools/check.ps1 through a copy of its `Py` function, so that Windows PowerShell 5.1 (run-check.cmd) and
# PowerShell 7 can both be tried without touching Teams, ADO or the network. The ADO read-back runs the REAL execute.inspect_comment,
# with a made-up organization and project and work item 0, and with the network cut: tests/check_readback_stub.py answers the one
# GET with a stored comment and makes any other call to the network fail. Prints one line per program: OK or NG with the output.
param([Parameter(Mandatory)][string]$Check, [Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$TextFile)
$src = Get-Content -Raw -Encoding UTF8 $Check
$line = ($src -split "`n" | Where-Object { $_ -match '^function Py\(' } | Select-Object -First 1)
Invoke-Expression $line          # the function exactly as check.ps1 defines it
$py = @($Python)
$progs = [regex]::Matches($src, "Py @\('-c', '((?:[^']|'')*)'") | ForEach-Object { $_.Groups[1].Value.Replace("''", "'") }
if (@($progs).Count -ne 3) { 'NG expected 3 -c programs, found ' + @($progs).Count; exit 1 }
$bad = 0
$env:KIMERU_ADO_ORG = 'fake-org'; $env:KIMERU_ADO_PROJECT = 'fake-project'    # a made-up target; no real organization is named
foreach ($p in $progs) {
  if ($p.Contains('"')) { 'NG a double quote in the program'; $bad++; continue }
  $code = if ($p -match 'inspect_comment') { 'import tests.check_readback_stub; ' + $p } else { $p }
  $args2 = @('-c', $code)
  if ($p -match 'sys\.argv') { $args2 += @('0', ('kimeru ' + [char]0x8a66 + [char]0x9a13), $TextFile) }
  $o = Py $args2
  if ($LASTEXITCODE -ne 0 -or $o -match 'Traceback|Error') { 'NG ' + $o.Trim(); $bad++ } else { 'OK ' + $o.Trim() }
}
Remove-Item Env:KIMERU_ADO_ORG, Env:KIMERU_ADO_PROJECT -ErrorAction SilentlyContinue
exit $(if ($bad) { 1 } else { 0 })

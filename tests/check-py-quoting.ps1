# Runs the `-c` programs of tools/check.ps1 through a copy of its `Py` function, so that Windows PowerShell 5.1 (run-check.cmd) and
# PowerShell 7 can both be tried without touching Teams, ADO or the network. The ADO read-back is replaced by a stub that only
# returns its arguments. Prints one line per program: OK or NG with the output.
param([Parameter(Mandatory)][string]$Check, [Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$TextFile)
$src = Get-Content -Raw -Encoding UTF8 $Check
$line = ($src -split "`n" | Where-Object { $_ -match '^function Py\(' } | Select-Object -First 1)
Invoke-Expression $line          # the function exactly as check.ps1 defines it
$py = @($Python)
$progs = [regex]::Matches($src, "Py @\('-c', '((?:[^']|'')*)'") | ForEach-Object { $_.Groups[1].Value.Replace("''", "'") }
if (@($progs).Count -ne 3) { 'NG expected 3 -c programs, found ' + @($progs).Count; exit 1 }
$bad = 0
foreach ($p in $progs) {
  if ($p.Contains('"')) { 'NG a double quote in the program'; $bad++; continue }
  $args2 = @('-c', $p.Replace('execute.inspect_comment', '(lambda w, m, t: {''w'': w, ''m'': m, ''t'': t})'))
  if ($p -match 'sys\.argv') { $args2 += @('123', ('kimeru ' + [char]0x8a66 + [char]0x9a13), $TextFile) }
  $o = Py $args2
  if ($LASTEXITCODE -ne 0 -or $o -match 'Traceback|Error') { 'NG ' + $o.Trim(); $bad++ } else { 'OK ' + $o.Trim() }
}
exit $(if ($bad) { 1 } else { 0 })

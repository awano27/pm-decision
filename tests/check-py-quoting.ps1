# Runs the `-c` programs of tools/check.ps1 through a copy of its `Py` function, so that Windows PowerShell 5.1 (run-check.cmd) and
# PowerShell 7 can both be tried without touching Teams, ADO or the network. The ADO read-back runs the REAL execute.inspect_comment,
# with a made-up organization and project and work item 0, and with the network cut: tests/check_readback_stub.py answers the one
# GET with a stored comment and makes any other call to the network fail. Prints one line per program: OK or NG with the output.
param([Parameter(Mandatory)][string]$Check, [Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$TextFile)
$src = Get-Content -Raw -Encoding UTF8 $Check
$def = [regex]::Match($src, '(?ms)^function Py\(.*?^\}').Value   # the whole function (it spans several lines)
if (-not $def) { 'NG function Py not found in check.ps1'; exit 1 }
$Auto = $false; $script:IsolatePy = $false; $tmp = [IO.Path]::GetTempPath()   # what Py reads from check.ps1's scope
Invoke-Expression $def          # the function exactly as check.ps1 defines it
$py = @($Python)
$progs = [regex]::Matches($src, "Py @\('-c', '((?:[^']|'')*)'") | ForEach-Object { $_.Groups[1].Value.Replace("''", "'") }
if (@($progs).Count -ne 5) { 'NG expected 5 -c programs, found ' + @($progs).Count; exit 1 }
$bad = 0
$env:KIMERU_ADO_ORG = 'fake-org'; $env:KIMERU_ADO_PROJECT = 'fake-project'    # a made-up target; no real organization is named
foreach ($p in $progs) {
  if ($p.Contains('"')) { 'NG a double quote in the program'; $bad++; continue }
  $code = if ($p -match 'inspect_comment') { 'import tests.check_readback_stub; ' + $p } else { $p }
  $args2 = @('-c', $code); $jf = $null
  if ($p -match 'json\.load') {
    # T25 (control.line_of) and T26 (sent.classify) read a JSON file that check.ps1 writes; here a made-up self row and a made-up sent text
    $jf = Join-Path ([IO.Path]::GetTempPath()) ('kimeru-n4-' + [guid]::NewGuid().ToString('N') + '.json')
    if ($p -match 'control\.line_of') { [IO.File]::WriteAllText($jf, '[{"kind":"self","preview":"あなた: 停止"}]', (New-Object Text.UTF8Encoding $false)) }
    else { [IO.File]::WriteAllText($jf, '{"sent":"kimeru 試験 1 の送信です","shown":["kimeru 試験 1 の送信です","別の人の文"]}', (New-Object Text.UTF8Encoding $false)) }
    $args2 += @($jf)
  }
  elseif ($p -match 'sys\.argv') { $args2 += @('0', ('kimeru ' + [char]0x8a66 + [char]0x9a13), $TextFile) }
  $o = Py $args2
  if ($jf) { Remove-Item $jf -ErrorAction SilentlyContinue }
  if ($LASTEXITCODE -ne 0 -or $o -match 'Traceback|Error') { 'NG ' + $o.Trim(); $bad++ } else { 'OK ' + $o.Trim() }
}
Remove-Item Env:KIMERU_ADO_ORG, Env:KIMERU_ADO_PROJECT -ErrorAction SilentlyContinue
exit $(if ($bad) { 1 } else { 0 })

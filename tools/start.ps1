# Called by START.cmd: prep, then the short Monday check. No admin rights.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$here = $PSScriptRoot
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'prepare-company.ps1')
Write-Host ''
Write-Host '===== 続けて動作確認をします（10 分ほど） =====' -ForegroundColor Cyan
Write-Host ''
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'company-check.ps1') monday
Write-Host ''
Write-Host '終わりました。結果はクリップボードにコピー済みです。チャットに貼り付けてください。' -ForegroundColor Green

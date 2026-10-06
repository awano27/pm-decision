# Called by START.cmd: prep, then the short bring-in check. No admin rights.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$here = $PSScriptRoot
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'prepare-bundle.ps1')
Write-Host ''
Write-Host '===== 続けて動作確認をします（10 分ほど） =====' -ForegroundColor Cyan
Write-Host ''
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'check.ps1') managed
Write-Host ''
Write-Host '終わりました。結果はクリップボードにコピー済みです。共有する前に中身を確認してください。' -ForegroundColor Green

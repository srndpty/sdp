# 自動修正スクリプト。Ruff の lint 自動修正とフォーマットを実行する。
#
#   pwsh -File scripts/fix.ps1
#
# 検査のみを行いたい場合は scripts/check.ps1 を使う。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Write-Host '=== Ruff lint (--fix) ===' -ForegroundColor Cyan
    uv run ruff check --fix .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host '=== Ruff format ===' -ForegroundColor Cyan
    uv run ruff format .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host '自動修正が完了しました。' -ForegroundColor Green
}
finally {
    Pop-Location
}

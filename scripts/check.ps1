# 品質チェックスクリプト。CI（.github/workflows/ci.yml）からも同じものを呼び出す。
#
#   pwsh -File scripts/check.ps1
#
# 実行順:
#   1. lockfile の整合性確認
#   2. Ruff format check
#   3. Ruff lint
#   4. Pyright
#   5. pytest（audio マーカーを除外、coverage 計測と XML 出力）
#   6. coverage 下限確認
#
# いずれかが失敗した時点で非ゼロ終了する。

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host "失敗: $Name (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Invoke-Step '1. lockfile 整合性確認' { uv lock --check }
    Invoke-Step '2. Ruff format check' { uv run ruff format --check . }
    Invoke-Step '3. Ruff lint' { uv run ruff check . }
    Invoke-Step '4. Pyright' { uv run pyright }
    Invoke-Step '5. pytest (audio 除外)' {
        uv run pytest -m 'not audio' --cov --cov-report=xml --cov-fail-under=0
    }
    Invoke-Step '6. coverage 下限確認' { uv run coverage report }

    Write-Host 'すべてのチェックに成功しました。' -ForegroundColor Green
}
finally {
    Pop-Location
}

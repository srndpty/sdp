# リポジトリの正式な開発手順を呼び出す薄い入口。
param([string]$Command = 'help')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = $PSScriptRoot
$forwardedArguments = @($args)
$exitCode = 0

if ($Command -notin @('run', 'gui', 'test', 'install') -and $forwardedArguments.Count -gt 0) {
    Write-Host "引数を追加できるのは run / gui / test / install だけです。" -ForegroundColor Red
    exit 2
}

# 起動時の相対音源パスは呼び出し元基準。uv の project だけを指定する。
if ($Command -in @('run', 'gui')) {
    & uv run --project $repoRoot python -m sdp @forwardedArguments
    exit $LASTEXITCODE
}

Push-Location $repoRoot
try {
    switch ($Command) {
        'help' {
            Write-Host @'
使い方: .\dev.ps1 <command> [引数]
  build  Windows onedir 配布物をビルド（scripts/build-package.ps1）
  run    アプリ起動（音源パス、--selftest、--codec-test を指定可能）
  gui    GUI アプリ起動（run と同じ入口）
  test   通常テスト（pytest の引数を追加可能。実音テストも含む）
  lint   Ruff format check / lint と Pyright（修正なし）
  check  CI と共通の正式な品質ゲート（scripts/check.ps1）
  fix    Ruff 自動修正とフォーマット（scripts/fix.ps1）
  install ビルドとローカルインストール（-SkipBuild / -InnoSetupCompiler を指定可能）
  help   この説明を表示（コマンド省略時も表示）
clean は未提供。配布・インストールの詳細手順は README を参照。
'@
        }
        'build' {
            & pwsh -NoProfile -File (Join-Path $repoRoot 'scripts/build-package.ps1')
            $exitCode = $LASTEXITCODE
        }
        'install' {
            & pwsh -NoProfile -File (Join-Path $repoRoot 'scripts/install-local.ps1') @forwardedArguments
            $exitCode = $LASTEXITCODE
        }
        'check' {
            & pwsh -NoProfile -File (Join-Path $repoRoot 'scripts/check.ps1')
            $exitCode = $LASTEXITCODE
        }
        'fix' {
            & pwsh -NoProfile -File (Join-Path $repoRoot 'scripts/fix.ps1')
            $exitCode = $LASTEXITCODE
        }
        'test' {
            & uv run pytest @forwardedArguments
            $exitCode = $LASTEXITCODE
        }
        'lint' {
            & uv run ruff format --check .
            $exitCode = $LASTEXITCODE
            if ($exitCode -eq 0) {
                & uv run ruff check .
                $exitCode = $LASTEXITCODE
            }
            if ($exitCode -eq 0) {
                & uv run pyright
                $exitCode = $LASTEXITCODE
            }
        }
        default {
            Write-Host "不明なコマンドです: $Command。dev help を参照してください。" -ForegroundColor Red
            $exitCode = 2
        }
    }
}
finally {
    Pop-Location
}
exit $exitCode

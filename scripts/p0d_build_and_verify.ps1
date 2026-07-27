# P0-D: PyInstaller onedir パッケージ版のクリーンビルドと検証。
#
#   pwsh -File scripts/p0d_build_and_verify.ps1
#   pwsh -File scripts/p0d_build_and_verify.ps1 -Runs 1
#
# 各ラウンドで次を行う。
#   1. build/ dist/ を削除（クリーンビルド）
#   2. PyInstaller を --clean --noconfirm で実行し、ビルドログと warn ファイルを保存
#   3. onedir 一式を ASCII パスと「日本語 空白」入りパスへコピー
#   4. PATH を OS 必須分だけに制限し、リポジトリ外をカレントディレクトリにして exe を実行
#
# PATH 制限により、開発用の python / uv / ffmpeg CLI に依存していないことを確認する。
# 開発機の FFmpeg CLI をリネームまたは削除するような破壊的操作は一切行わない。

[CmdletBinding()]
param(
    [int]$Runs = 2
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$workRoot = Join-Path $repoRoot '.sdp-local/p0d'
$audioDir = Join-Path $repoRoot 'assets/test_audio'

# OS の動作に必要な最小限の PATH だけを残す。
# uv / Python / C:\tools（開発用 FFmpeg CLI）は意図的に含めない。
$restrictedPath = @(
    "$env:SystemRoot\system32"
    "$env:SystemRoot"
    "$env:SystemRoot\System32\Wbem"
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0"
) -join ';'

function Assert-CommandMissing {
    param([Parameter(Mandatory)][string]$Name)

    $found = & "$env:SystemRoot\system32\where.exe" $Name 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  where.exe $Name : 見つかった → $found" -ForegroundColor Red
        return $false
    }
    Write-Host "  where.exe $Name : 見つからない（期待どおり）" -ForegroundColor Green
    return $true
}

function Invoke-PackagedProbe {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$PackageDir,
        [Parameter(Mandatory)][string]$LogPath
    )

    $exePath = Join-Path $PackageDir 'p0d_probe.exe'
    if (-not (Test-Path $exePath)) {
        Write-Host "exe が見つかりません: $exePath" -ForegroundColor Red
        return $false
    }

    Write-Host "--- 実行: $Label ---" -ForegroundColor Cyan
    Write-Host "  exe : $exePath"

    $savedPath = $env:PATH
    # リポジトリ外をカレントディレクトリにして、ソースツリーへの暗黙依存を検出する。
    Push-Location $env:TEMP
    try {
        $env:PATH = $restrictedPath
        Write-Host "  PATH を OS 必須分へ制限した状態で外部コマンドの不在を確認:"
        $missingOk = $true
        foreach ($name in @('ffmpeg', 'ffprobe', 'python', 'uv')) {
            $missingOk = (Assert-CommandMissing -Name $name) -and $missingOk
        }
        if (-not $missingOk) {
            Write-Host '  PATH の制限が不十分です。検証をやり直してください。' -ForegroundColor Red
            return $false
        }

        Write-Host "  cwd : $(Get-Location)"
        & $exePath --audio-dir $audioDir *>&1 | Tee-Object -FilePath $LogPath
        $exitCode = $LASTEXITCODE
        Write-Host "  終了コード: $exitCode"
        return ($exitCode -eq 0)
    }
    finally {
        $env:PATH = $savedPath
        Pop-Location
    }
}

Push-Location $repoRoot
try {
    New-Item -ItemType Directory -Force -Path $workRoot | Out-Null
    $overallOk = $true

    for ($run = 1; $run -le $Runs; $run++) {
        Write-Host ''
        Write-Host "========== クリーンビルド $run 回目 ==========" -ForegroundColor Yellow

        foreach ($dir in @('build', 'dist')) {
            $target = Join-Path $repoRoot $dir
            if (Test-Path $target) {
                Remove-Item -Recurse -Force $target
                Write-Host "  削除: $dir"
            }
        }

        $buildLog = Join-Path $workRoot "build-run$run.log"
        Write-Host '  PyInstaller を実行中...'
        uv run pyinstaller --clean --noconfirm packaging/p0d_probe.spec *>&1 |
            Tee-Object -FilePath $buildLog | Select-Object -Last 3
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ビルド失敗 (exit $LASTEXITCODE)。ログ: $buildLog" -ForegroundColor Red
            exit $LASTEXITCODE
        }

        $warnFile = Join-Path $repoRoot 'build/p0d_probe/warn-p0d_probe.txt'
        if (Test-Path $warnFile) {
            Copy-Item $warnFile (Join-Path $workRoot "warn-run$run.txt") -Force
            $warnCount = (Get-Content $warnFile | Where-Object { $_ -match '^missing module' }).Count
            Write-Host "  warn ファイルの missing module 行数: $warnCount"
        }

        $distDir = Join-Path $repoRoot 'dist/p0d_probe'
        $sizeMb = [math]::Round(
            ((Get-ChildItem $distDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
        $fileCount = (Get-ChildItem $distDir -Recurse -File | Measure-Object).Count
        Write-Host "  成果物: $distDir （$sizeMb MB / $fileCount ファイル）"

        # ビルド元とは別の場所へコピーして実行する。
        $asciiDir = Join-Path $workRoot "run-ascii"
        $japaneseDir = Join-Path $workRoot "日本語 パッケージ"
        foreach ($dir in @($asciiDir, $japaneseDir)) {
            if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
            Copy-Item $distDir $dir -Recurse -Force
        }

        $asciiOk = Invoke-PackagedProbe -Label "ASCII パス (run $run)" -PackageDir $asciiDir `
            -LogPath (Join-Path $workRoot "probe-ascii-run$run.log")
        $japaneseOk = Invoke-PackagedProbe -Label "日本語・空白パス (run $run)" -PackageDir $japaneseDir `
            -LogPath (Join-Path $workRoot "probe-japanese-run$run.log")

        if ($asciiOk -and $japaneseOk) {
            Write-Host "  ラウンド $run : 合格" -ForegroundColor Green
        }
        else {
            Write-Host "  ラウンド $run : 不合格" -ForegroundColor Red
            $overallOk = $false
        }
    }

    Write-Host ''
    if ($overallOk) {
        Write-Host "全 $Runs ラウンドが合格しました。" -ForegroundColor Green
        exit 0
    }
    Write-Host '不合格のラウンドがあります。' -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}

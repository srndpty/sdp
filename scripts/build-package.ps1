# sdpのWindows onedir配布物を再現可能な手順で構築する。

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$specPath = Join-Path $repoRoot 'packaging\sdp.spec'
$buildPath = [IO.Path]::GetFullPath((Join-Path $repoRoot 'build'))
$distPath = [IO.Path]::GetFullPath((Join-Path $repoRoot 'dist'))

function Assert-SafeBuildTarget {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Leaf)

    $expected = [IO.Path]::GetFullPath((Join-Path $repoRoot $Leaf))
    if ($Path -ne $expected -or -not $Path.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "安全でない削除対象です: $Path"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -PathType Leaf)) {
    throw "リポジトリルートを確認できません: $repoRoot"
}
if (-not (Test-Path -LiteralPath $specPath -PathType Leaf)) {
    throw "PyInstaller specがありません: $specPath"
}

Assert-SafeBuildTarget -Path $buildPath -Leaf 'build'
Assert-SafeBuildTarget -Path $distPath -Leaf 'dist'

foreach ($target in @($buildPath, $distPath)) {
    if (Test-Path -LiteralPath $target) {
        try {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
        catch {
            throw "以前の配布物を削除できません。起動中のsdp.exeやExplorerの参照を閉じてください: $target"
        }
    }
}

$timer = [Diagnostics.Stopwatch]::StartNew()
Push-Location $repoRoot
try {
    uv run pyinstaller $specPath --clean --noconfirm
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
    $timer.Stop()
}

$packagePath = Join-Path $distPath 'sdp'
$size = (Get-ChildItem -LiteralPath $packagePath -File -Recurse | Measure-Object Length -Sum).Sum
Write-Host ("ビルド完了: {0}" -f $packagePath) -ForegroundColor Green
Write-Host ("所要時間: {0:N1}秒 / サイズ: {1:N1} MiB" -f $timer.Elapsed.TotalSeconds, ($size / 1MB))

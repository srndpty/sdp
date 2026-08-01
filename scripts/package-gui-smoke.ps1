# build済みWindows配布版をpywinautoで実操作する。対話desktopでのみ実行する。

[CmdletBinding()]
param(
    [string]$PackageDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ([string]::IsNullOrWhiteSpace($PackageDirectory)) {
    $PackageDirectory = Join-Path $repoRoot 'dist\sdp'
}
$packagePath = [IO.Path]::GetFullPath($PackageDirectory)
if (-not (Test-Path -LiteralPath (Join-Path $packagePath 'sdp.exe') -PathType Leaf)) {
    throw "配布版sdp.exeがありません: $packagePath"
}

$originalPackage = $env:SDP_PACKAGE_DIRECTORY
try {
    $env:SDP_PACKAGE_DIRECTORY = $packagePath
    Push-Location $repoRoot
    try {
        uv run pytest tests/e2e_windows/test_packaged_gui.py -m packaged_gui -q -p no:cacheprovider
        if ($LASTEXITCODE -ne 0) {
            throw "配布版GUI smokeに失敗しました（exit code $LASTEXITCODE）"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:SDP_PACKAGE_DIRECTORY = $originalPackage
}

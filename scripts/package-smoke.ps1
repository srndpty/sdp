# 開発用Pythonへ依存せず、リポジトリ外でonedir配布物のselftestを実行する。

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
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$smokeRoot = [IO.Path]::GetFullPath(
    (Join-Path $tempRoot ("sdp-package-smoke-{0}" -f [Guid]::NewGuid()))
)
if (-not $smokeRoot.StartsWith($tempRoot) -or
    [IO.Path]::GetFileName($smokeRoot) -notlike 'sdp-package-smoke-*') {
    throw "安全でない一時directoryです: $smokeRoot"
}
$copiedPackage = Join-Path $smokeRoot 'sdp'
$localData = Join-Path $smokeRoot 'local-app-data'
$originalPath = $env:PATH
$originalLocalAppData = $env:LOCALAPPDATA
$originalPythonUtf8 = $env:PYTHONUTF8

function Invoke-PackagedSelftest {
    param(
        [Parameter(Mandatory)][int]$ExpectedExitCode,
        [Parameter(Mandatory)][string]$Label
    )

    $process = Start-Process `
        -FilePath (Join-Path $copiedPackage 'sdp.exe') `
        -ArgumentList '--selftest' `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($process.ExitCode -ne $ExpectedExitCode) {
        throw "$Label の終了コードが不正です（期待 $ExpectedExitCode、実際 $($process.ExitCode)）"
    }
}

function Assert-MissingDependencyFails {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Label
    )

    $disabledPath = "$Path.sdp-disabled"
    Move-Item -LiteralPath $Path -Destination $disabledPath
    try {
        Invoke-PackagedSelftest -ExpectedExitCode 1 -Label "$Label 欠落selftest"
    }
    finally {
        Move-Item -LiteralPath $disabledPath -Destination $Path
    }
}

try {
    $env:PYTHONUTF8 = '1'
    Push-Location $repoRoot
    try {
        uv run python tools/package_layout.py $packagePath
        if ($LASTEXITCODE -ne 0) {
            throw "配布物の構造検査に失敗しました（exit code $LASTEXITCODE）"
        }
    }
    finally {
        Pop-Location
    }

    New-Item -ItemType Directory -Path $smokeRoot | Out-Null
    Copy-Item -LiteralPath $packagePath -Destination $copiedPackage -Recurse
    New-Item -ItemType Directory -Path $localData | Out-Null

    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:LOCALAPPDATA = $localData
    Push-Location $smokeRoot
    try {
        Invoke-PackagedSelftest -ExpectedExitCode 0 -Label '通常selftest'

        $mediaPlugin = @(
            Get-ChildItem -LiteralPath $copiedPackage -Recurse -File `
                -Filter 'ffmpegmediaplugin.dll'
        )
        $avcodec = @(
            Get-ChildItem -LiteralPath $copiedPackage -Recurse -File -Filter 'avcodec-*.dll'
        )
        if ($mediaPlugin.Count -ne 1 -or $avcodec.Count -ne 1) {
            throw "欠落test対象を一意に検出できません（plugin=$($mediaPlugin.Count), avcodec=$($avcodec.Count)）"
        }
        Assert-MissingDependencyFails -Path $mediaPlugin[0].FullName -Label 'FFmpeg media plugin'
        Assert-MissingDependencyFails -Path $avcodec[0].FullName -Label 'FFmpeg avcodec'
    }
    finally {
        Pop-Location
    }

    $unexpected = Get-ChildItem -LiteralPath $localData -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('playlist.json', 'settings.json', 'ui-state.json') }
    if ($unexpected) {
        throw "selftestが永続設定ファイルを作成しました: $($unexpected.FullName -join ', ')"
    }

    Write-Host 'リポジトリ外・制限PATHでの配布版selftestと欠落検出に成功しました。' `
        -ForegroundColor Green
}
finally {
    $env:PATH = $originalPath
    $env:LOCALAPPDATA = $originalLocalAppData
    $env:PYTHONUTF8 = $originalPythonUtf8
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}

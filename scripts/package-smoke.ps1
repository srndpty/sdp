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
$smokeRoot = Join-Path ([IO.Path]::GetTempPath()) ("sdp-package-smoke-{0}" -f [Guid]::NewGuid())
$copiedPackage = Join-Path $smokeRoot 'sdp'
$localData = Join-Path $smokeRoot 'local-app-data'
$originalPath = $env:PATH
$originalLocalAppData = $env:LOCALAPPDATA
$originalPythonUtf8 = $env:PYTHONUTF8

Push-Location $repoRoot
try {
    $env:PYTHONUTF8 = '1'
    uv run python tools/package_layout.py $packagePath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

New-Item -ItemType Directory -Path $smokeRoot | Out-Null
Copy-Item -LiteralPath $packagePath -Destination $copiedPackage -Recurse
New-Item -ItemType Directory -Path $localData | Out-Null

try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:LOCALAPPDATA = $localData
    Push-Location $smokeRoot
    try {
        $process = Start-Process `
            -FilePath (Join-Path $copiedPackage 'sdp.exe') `
            -ArgumentList '--selftest' `
            -PassThru `
            -Wait `
            -WindowStyle Hidden
        if ($process.ExitCode -ne 0) {
            throw "配布版selftestに失敗しました（exit code $($process.ExitCode)）"
        }
    }
    finally {
        Pop-Location
    }

    $unexpected = Get-ChildItem -LiteralPath $localData -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('playlist.json', 'settings.json', 'ui-state.json') }
    if ($unexpected) {
        throw "selftestが永続設定ファイルを作成しました: $($unexpected.FullName -join ', ')"
    }

    Write-Host 'リポジトリ外・制限PATHでの配布版selftestに成功しました。' -ForegroundColor Green
}
finally {
    $env:PATH = $originalPath
    $env:LOCALAPPDATA = $originalLocalAppData
    $env:PYTHONUTF8 = $originalPythonUtf8
    if (Test-Path -LiteralPath $smokeRoot) {
        Remove-Item -LiteralPath $smokeRoot -Recurse -Force
    }
}

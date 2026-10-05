# 最新の作業ツリーから installer を作り、ローカルマシンへ上書き導入する。
[CmdletBinding()]
param([switch]$SkipBuild, [string]$InnoSetupCompiler)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot

if (Get-Process -Name sdp -ErrorAction SilentlyContinue) {
    throw 'sdp が起動中です。終了してから dev install を実行してください。'
}

Push-Location $repoRoot
try {
    $buildArguments = @()
    if ($SkipBuild) { $buildArguments += '-SkipBuild' }
    if ($InnoSetupCompiler) { $buildArguments += @('-InnoSetupCompiler', $InnoSetupCompiler) }
    & pwsh -NoProfile -File (Join-Path $PSScriptRoot 'build-installer.ps1') @buildArguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    # build と同じ命名規則を使い、古い別 version の installer を選ばない。
    $installerName = & uv run python -c @"
from sdp import __version__
from sdp.installer_manifest import installer_name
from sdp.release_manifest import normalized_architecture
print(installer_name(__version__, normalized_architecture()))
"@
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $setupPath = (Resolve-Path -LiteralPath (Join-Path $repoRoot "release/$($installerName.Trim())")).Path
    Write-Host "ローカルインストールを開始します（UAC 昇格）: $setupPath"
    try {
        $install = Start-Process -FilePath $setupPath -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
    }
    catch [System.ComponentModel.Win32Exception] {
        if ($_.Exception.NativeErrorCode -eq 1223) {
            Write-Host 'UAC の承認がキャンセルされました。' -ForegroundColor Red
            exit 1223
        }
        throw
    }
    if ($install.ExitCode -ne 0) {
        Write-Host "インストールに失敗しました（exit code $($install.ExitCode)）。" -ForegroundColor Red
        if ($install.ExitCode -eq 7) {
            Write-Host '旧 per-user 版を「アプリと機能」から削除してから再実行してください。'
        }
        exit $install.ExitCode
    }

    # installer が登録した実際の導入先を使う（カスタム導入先にも対応）。
    $manifestPath = $setupPath.Replace('-setup.exe', '-installer.manifest.json')
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $uninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($manifest.app_id)_is1"
    $uninstall = Get-ItemProperty -LiteralPath $uninstallKey
    $installedExecutable = Join-Path $uninstall.'Inno Setup: App Path' 'sdp.exe'
    $selftest = Start-Process -FilePath $installedExecutable -ArgumentList '--selftest' `
        -Wait -PassThru -WindowStyle Hidden
    if ($selftest.ExitCode -ne 0) { exit $selftest.ExitCode }
    Write-Host 'インストールと自己診断に成功しました。' -ForegroundColor Green
}
finally {
    Pop-Location
}

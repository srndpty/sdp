# sdpのWindows per-user installer（Inno Setup 6.3以降）を、検証込みで生成する。
#
#   pwsh -File scripts/build-installer.ps1
#   pwsh -File scripts/build-installer.ps1 -SkipBuild            # 既存のrelease成果物を使う
#   pwsh -File scripts/build-installer.ps1 -InnoSetupCompiler <ISCC.exe path>
#
# 実行順:
#   1. ZIPリリース生成（scripts/build-release.ps1）で配布物を検証する
#   2. dist/sdp が release manifest の content hash と一致することを確かめる
#      （ZIP配布物とinstaller入力が同一内容であることの担保）
#   3. layout検査・ライセンス資料検査・installer契約検査
#   4. 配布物の selftest と 6形式の codec test
#   5. Windows version resource が pyproject の version と一致するか確認
#   6. Inno Setup compilerでcompile（version・入力配布物は /D で注入する）
#   7. SHA-256 と installer manifest を生成
#   8. 全検証成功後に、同versionの3成果物だけを release/ へ確定する
#
# staging はリポジトリ内 tmp/ に置く。途中失敗しても既存 release/ には触れず、
# 前回の正常なinstallerを壊さない。
#
# ライセンスの未解決事項が残るため、生成物は**技術検証用**であり公開配布物ではない。

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$InnoSetupCompiler,
    [string]$SourceDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$releasePath = [IO.Path]::GetFullPath((Join-Path $repoRoot 'release'))
$installerScript = Join-Path $repoRoot 'packaging\installer.iss'
$iconFile = Join-Path $repoRoot 'assets\sdp.ico'
$codecFixtureDirectory = Join-Path $repoRoot 'assets\test_audio'
# releaseゲートと同じ6形式。配布物へは同梱せず、検査時だけpathを渡す。
$codecFixtureNames = @(
    'sine440.wav',
    'sine440.mp3',
    'sine440.flac',
    'sine440.ogg',
    'sine440.opus',
    'sine440.m4a'
)

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$Command
    )

    Write-Host "=== $Label ===" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label に失敗しました（exit code $LASTEXITCODE）"
    }
}

function Invoke-PackagedExecutable {
    param(
        [Parameter(Mandatory)][string]$ExecutablePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Label
    )

    $process = Start-Process -FilePath $ExecutablePath -ArgumentList $Arguments `
        -PassThru -Wait -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "$Label に失敗しました（exit code $($process.ExitCode)）"
    }
}

function Resolve-InnoSetupCompiler {
    param([string]$Explicit)

    # 明示指定 → 環境変数 → PATH → 既定のinstall先、の順で探す。
    $candidates = [Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($Explicit)) {
        $candidates.Add($Explicit)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:INNO_SETUP_COMPILER)) {
        $candidates.Add($env:INNO_SETUP_COMPILER)
    }
    $onPath = Get-Command 'ISCC.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $onPath) {
        $candidates.Add($onPath.Source)
    }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, "$env:LOCALAPPDATA\Programs")) {
        if (-not [string]::IsNullOrWhiteSpace($base)) {
            $candidates.Add((Join-Path $base 'Inno Setup 6\ISCC.exe'))
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw @'
Inno Setup compiler（ISCC.exe）が見つかりません。次のいずれかで解決してください。
  1. Inno Setup 6.3以降を導入する（winget install --id JRSoftware.InnoSetup -e）
  2. -InnoSetupCompiler <ISCC.exeのpath> を指定する
  3. 環境変数 INNO_SETUP_COMPILER へ ISCC.exe のpathを設定する
'@
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -PathType Leaf)) {
    throw "リポジトリルートを確認できません: $repoRoot"
}
foreach ($required in @($installerScript, $iconFile)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "installerの入力がありません: $required"
    }
}
$expectedRelease = [IO.Path]::GetFullPath((Join-Path $repoRoot 'release'))
if ($releasePath -ne $expectedRelease) {
    throw "安全でないrelease targetです: $releasePath"
}

if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
    $SourceDirectory = Join-Path $repoRoot 'dist\sdp'
}
$packagePath = [IO.Path]::GetFullPath($SourceDirectory)

$timer = [Diagnostics.Stopwatch]::StartNew()
$stagingRoot = $null
$originalLocalAppData = $env:LOCALAPPDATA
$originalPythonUtf8 = $env:PYTHONUTF8
$verifyRoot = $null

try {
    $env:PYTHONUTF8 = '1'

    if (-not $SkipBuild) {
        Invoke-Checked -Label '1. ZIPリリースの生成と検証' -Command {
            pwsh -NoProfile -File (Join-Path $PSScriptRoot 'build-release.ps1')
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $packagePath 'sdp.exe') -PathType Leaf)) {
        throw "検証済み配布物がありません。先にscripts/build-release.ps1を実行してください: $packagePath"
    }

    Push-Location $repoRoot
    try {
        $metadata = (uv run python -c @'
from sdp import __version__
from sdp.installer_manifest import installer_name
from sdp.release_manifest import archive_name, normalized_architecture
from sdp.windows_version import format_version_tuple, windows_file_version

architecture = normalized_architecture()
fields = windows_file_version(__version__)
print(__version__)
print(architecture)
print(installer_name(__version__, architecture))
print(archive_name(__version__, architecture))
print(".".join(str(value) for value in fields))
print(format_version_tuple(fields).strip("()").replace(" ", ""))
'@) -split "`r?`n" | Where-Object { $_ -ne '' }
        if ($LASTEXITCODE -ne 0 -or $metadata.Count -ne 6) {
            throw 'versionとinstaller名を決定できませんでした'
        }
        $version = $metadata[0].Trim()
        $architecture = $metadata[1].Trim()
        $installerName = $metadata[2].Trim()
        $archiveName = $metadata[3].Trim()
        $versionInfoVersion = $metadata[4].Trim()

        # ZIP配布物とinstaller入力が同一内容であることを、release manifestの
        # content hashと突き合わせて確かめる（別々のbuildを混ぜない）。
        $manifestName = [IO.Path]::GetFileNameWithoutExtension($archiveName) + '.manifest.json'
        $releaseManifest = Join-Path $releasePath $manifestName
        if (-not (Test-Path -LiteralPath $releaseManifest -PathType Leaf)) {
            throw "release manifestがありません。先にscripts/build-release.ps1を実行してください: $releaseManifest"
        }
        Invoke-Checked -Label '2. ZIP配布物とinstaller入力の一致検証' -Command {
            uv run python -c @'
import json
import sys
from pathlib import Path

from sdp.release_manifest import scan_package

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = manifest["contents"]["content_sha256"]
actual = scan_package(Path(sys.argv[2])).content_sha256
if expected != actual:
    print(f"エラー: ZIP配布物とinstaller入力の内容が一致しません: {expected} != {actual}")
    raise SystemExit(1)
print(f"content hash一致: {actual}")
'@ $releaseManifest $packagePath
        }

        Invoke-Checked -Label '3-1. 配布物のlayout検査' -Command {
            uv run python tools/package_layout.py $packagePath
        }
        # 宣言した原文の欠落（error）はbuildを止める。未解決事項は一覧表示に留める。
        Invoke-Checked -Label '3-2. ライセンス資料の検査' -Command {
            uv run python tools/license_audit.py $packagePath
        }
        Invoke-Checked -Label '3-3. installer契約の検査' -Command {
            uv run python tools/installer_contract.py $installerScript
        }
    }
    finally {
        Pop-Location
    }

    # 配布物の実行検査は、ユーザーdataを汚さないよう隔離したLOCALAPPDATAで行う。
    $verifyRoot = [IO.Path]::GetFullPath(
        (Join-Path ([IO.Path]::GetTempPath()) ("sdp-installer-verify-{0}" -f [Guid]::NewGuid()))
    )
    New-Item -ItemType Directory -Path $verifyRoot | Out-Null
    $env:LOCALAPPDATA = $verifyRoot
    $executable = Join-Path $packagePath 'sdp.exe'

    Write-Host '=== 4. 配布物のselftestとcodec test ===' -ForegroundColor Cyan
    Invoke-PackagedExecutable -ExecutablePath $executable -Arguments @('--selftest') `
        -Label '配布物のselftest'
    $fixtures = @()
    $missingFixtures = @()
    foreach ($name in $codecFixtureNames) {
        $fixture = Join-Path $codecFixtureDirectory $name
        if (Test-Path -LiteralPath $fixture -PathType Leaf) {
            $fixtures += $fixture
        }
        else {
            $missingFixtures += $name
        }
    }
    if ($missingFixtures.Count -gt 0) {
        throw "codec test用音源が不足しています: $($missingFixtures -join ', ')"
    }
    Invoke-PackagedExecutable -ExecutablePath $executable `
        -Arguments (@('--codec-test') + $fixtures) -Label '配布物のcodec test'
    $env:LOCALAPPDATA = $originalLocalAppData

    Write-Host '=== 5. Windows version resourceの確認 ===' -ForegroundColor Cyan
    $versionInfo = (Get-Item -LiteralPath $executable).VersionInfo
    if ($versionInfo.ProductName -ne 'sdp' -or $versionInfo.InternalName -ne 'sdp') {
        throw "sdp.exeのversion resourceが不正です（ProductName=$($versionInfo.ProductName)）"
    }
    $embedded = "{0}.{1}.{2}.{3}" -f $versionInfo.FileMajorPart, $versionInfo.FileMinorPart,
        $versionInfo.FileBuildPart, $versionInfo.FilePrivatePart
    if ($embedded -ne $versionInfoVersion) {
        throw "sdp.exeのFileVersionがpyprojectと一致しません: $embedded != $versionInfoVersion"
    }
    Write-Host ("  FileVersion={0} / ProductVersion={1}" -f $embedded, $versionInfo.ProductVersion)

    $compiler = Resolve-InnoSetupCompiler -Explicit $InnoSetupCompiler
    Write-Host ("=== 6. Inno Setup compile ===`n  compiler: {0}" -f $compiler) -ForegroundColor Cyan

    # stagingはrelease/の外（リポジトリ内のtmp/）。compile失敗が既存installerを壊さない。
    $tmpRoot = Join-Path $repoRoot 'tmp'
    if (-not (Test-Path -LiteralPath $tmpRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $tmpRoot | Out-Null
    }
    $stagingRoot = [IO.Path]::GetFullPath(
        (Join-Path $tmpRoot ("installer-staging-{0}" -f [Guid]::NewGuid()))
    )
    New-Item -ItemType Directory -Path $stagingRoot | Out-Null

    & $compiler `
        "/DAppVersion=$version" `
        "/DVersionInfoVersion=$versionInfoVersion" `
        "/DSourceDir=$packagePath" `
        "/O$stagingRoot" `
        $installerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setupのcompileに失敗しました（exit code $LASTEXITCODE）"
    }

    $stagedInstaller = Join-Path $stagingRoot $installerName
    if (-not (Test-Path -LiteralPath $stagedInstaller -PathType Leaf)) {
        throw "setup exeを生成できませんでした: $installerName"
    }

    Write-Host '=== 7. SHA-256とmanifest ===' -ForegroundColor Cyan
    $hash = (Get-FileHash -LiteralPath $stagedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    $stagedHashFile = "$stagedInstaller.sha256"
    # sha256sum互換の1行（ファイル名だけを書き、絶対pathは残さない）。
    [IO.File]::WriteAllText($stagedHashFile, "$hash  $installerName`n", [Text.UTF8Encoding]::new($false))

    $installerManifestName = "sdp-$version-windows-$(if ($architecture -eq 'x86_64') { 'x64' } else { $architecture })-installer.manifest.json"
    $stagedManifest = Join-Path $stagingRoot $installerManifestName
    Push-Location $repoRoot
    try {
        Invoke-Checked -Label 'installer manifest生成' -Command {
            uv run python tools/installer_manifest.py $stagedInstaller $stagedManifest
        }
    }
    finally {
        Pop-Location
    }

    Write-Host '=== 8. release/への確定 ===' -ForegroundColor Cyan
    # ここまで全部成功したときだけ、同versionの3成果物だけを置換する。
    if (-not (Test-Path -LiteralPath $releasePath -PathType Container)) {
        New-Item -ItemType Directory -Path $releasePath | Out-Null
    }
    $stagedItems = @($stagedInstaller, $stagedHashFile, $stagedManifest)
    $finalTargets = $stagedItems | ForEach-Object { Join-Path $releasePath (Split-Path -Leaf $_) }
    $backupRoot = Join-Path $stagingRoot 'previous'
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
    $restorePairs = @()
    try {
        foreach ($target in $finalTargets) {
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                $backup = Join-Path $backupRoot (Split-Path -Leaf $target)
                Move-Item -LiteralPath $target -Destination $backup
                $restorePairs += , @($backup, $target)
            }
        }
        for ($index = 0; $index -lt $stagedItems.Count; $index++) {
            Move-Item -LiteralPath $stagedItems[$index] -Destination $finalTargets[$index]
        }
    }
    catch {
        # 置換に失敗したら、今回の中途半端な成果物を消し、退避した旧3成果物を戻す。
        foreach ($target in $finalTargets) {
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                Remove-Item -LiteralPath $target -Force
            }
        }
        foreach ($pair in $restorePairs) {
            Move-Item -LiteralPath $pair[0] -Destination $pair[1]
        }
        throw
    }

    $timer.Stop()
    $installerPath = Join-Path $releasePath $installerName
    $installerSize = (Get-Item -LiteralPath $installerPath).Length
    Write-Host ''
    Write-Host ("installerの生成に成功しました: {0}" -f $releasePath) -ForegroundColor Green
    Write-Host ("  setup   : {0} ({1:N1} MiB)" -f $installerName, ($installerSize / 1MB))
    Write-Host ("  sha256  : {0}" -f $hash)
    Write-Host ("  所要時間: {0:N1}秒" -f $timer.Elapsed.TotalSeconds)
    Write-Host ''
    Write-Host '注意: ライセンスの未解決事項が残るため、このinstallerは技術検証用です。' `
        -ForegroundColor Yellow
    Write-Host '      docs/distribution-licenses.md を参照してください。' -ForegroundColor Yellow
}
finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    $env:PYTHONUTF8 = $originalPythonUtf8
    if ($null -ne $verifyRoot -and (Test-Path -LiteralPath $verifyRoot)) {
        Remove-Item -LiteralPath $verifyRoot -Recurse -Force
    }
    if ($null -ne $stagingRoot -and (Test-Path -LiteralPath $stagingRoot)) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}

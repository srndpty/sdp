# sdpのWindows ZIP配布物を、検証込みで再現可能に生成する。
#
#   pwsh -File scripts/build-release.ps1
#
# 実行順:
#   1. onedir配布物をbuild（scripts/build-package.ps1）
#   2. 配布物のlayout検査
#   3. リポジトリ外・制限PATHでのpackage smokeとライセンス資料検査
#   4. ZIP作成（root directoryは sdp/）
#   5. 別tempへ展開し、layout・selftest・codec test（6形式必須）と
#      展開前後のcontent hash一致を再検証
#   6. SHA-256と、展開後の実内容から算出したmanifestを生成
#   7. 全検証成功後に、同versionの3成果物だけをrelease/へ確定
#
# staging はリポジトリ内 tmp/ に置く。途中失敗しても既存 release/ には触れず、
# 前回の正常な成果物を壊さない。不完全な最終archiveも release/ へ残さない。

[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$distPath = [IO.Path]::GetFullPath((Join-Path $repoRoot 'dist'))
$packagePath = Join-Path $distPath 'sdp'
$releasePath = [IO.Path]::GetFullPath((Join-Path $repoRoot 'release'))
$codecFixtureDirectory = Join-Path $repoRoot 'assets\test_audio'
# 配布物へは同梱せず、検査時だけ渡す音源（--codec-testはpath必須）。
$codecFixtureNames = @(
    'sine440.wav',
    'sine440.mp3',
    'sine440.flac',
    'sine440.ogg',
    'sine440.opus',
    'sine440.m4a'
)

function Assert-SafeReleaseTarget {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Leaf)

    $expected = [IO.Path]::GetFullPath((Join-Path $repoRoot $Leaf))
    if ($Path -ne $expected -or -not $Path.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "安全でない削除対象です: $Path"
    }
}

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

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -PathType Leaf)) {
    throw "リポジトリルートを確認できません: $repoRoot"
}
Assert-SafeReleaseTarget -Path $releasePath -Leaf 'release'

$timer = [Diagnostics.Stopwatch]::StartNew()
$temporaryRoot = $null
$stagingRoot = $null
$originalLocalAppData = $env:LOCALAPPDATA
$originalPythonUtf8 = $env:PYTHONUTF8

try {
    $env:PYTHONUTF8 = '1'

    if (-not $SkipBuild) {
        Invoke-Checked -Label '1. onedir配布物のbuild' -Command {
            pwsh -NoProfile -File (Join-Path $PSScriptRoot 'build-package.ps1')
        }
    }
    if (-not (Test-Path -LiteralPath $packagePath -PathType Container)) {
        throw "配布物がありません。先にscripts/build-package.ps1を実行してください: $packagePath"
    }

    Push-Location $repoRoot
    try {
        Invoke-Checked -Label '2. 配布物のlayout検査' -Command {
            uv run python tools/package_layout.py $packagePath
        }
        Invoke-Checked -Label '3. package smoke' -Command {
            pwsh -NoProfile -File (Join-Path $PSScriptRoot 'package-smoke.ps1')
        }
        # 宣言したライセンス原文の同梱を検査する（未解決事項は一覧表示するだけ）。
        Invoke-Checked -Label '3-1. ライセンス資料の検査' -Command {
            uv run python tools/license_audit.py $packagePath
        }

        # ZIPとmanifestはversion付きの名前にする（versionはpyproject由来）。
        $archiveName = (uv run python -c @'
from sdp import __version__
from sdp.release_manifest import archive_name, normalized_architecture

print(archive_name(__version__, normalized_architecture()))
'@)
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($archiveName)) {
            throw 'archive名を決定できませんでした'
        }
        $archiveName = $archiveName.Trim()
    }
    finally {
        Pop-Location
    }

    # stagingはrelease/の外（リポジトリ内のtmp/）へ置く。全検証が成功したときだけ
    # 最終成果物をrelease/へ置換するため、途中失敗が前回の正常な成果物を壊さない。
    $tmpRoot = Join-Path $repoRoot 'tmp'
    if (-not (Test-Path -LiteralPath $tmpRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $tmpRoot | Out-Null
    }
    $stagingRoot = [IO.Path]::GetFullPath(
        (Join-Path $tmpRoot ("release-staging-{0}" -f [Guid]::NewGuid()))
    )
    New-Item -ItemType Directory -Path $stagingRoot | Out-Null
    $stagedArchive = Join-Path $stagingRoot $archiveName

    Write-Host '=== 4. ZIP作成 ===' -ForegroundColor Cyan
    # ZIP内のroot directoryを sdp/ に固定する（展開して1つのフォルダーになる）。
    Compress-Archive -Path $packagePath -DestinationPath $stagedArchive -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $stagedArchive -PathType Leaf)) {
        throw "ZIPを作成できませんでした: $stagedArchive"
    }

    Write-Host '=== 5. 展開後の検証 ===' -ForegroundColor Cyan
    $temporaryRoot = [IO.Path]::GetFullPath(
        (Join-Path ([IO.Path]::GetTempPath()) ("sdp-release-verify-{0}" -f [Guid]::NewGuid()))
    )
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $extractRoot = Join-Path $temporaryRoot 'extracted'
    Expand-Archive -LiteralPath $stagedArchive -DestinationPath $extractRoot
    $extractedPackage = Join-Path $extractRoot 'sdp'
    if (-not (Test-Path -LiteralPath (Join-Path $extractedPackage 'sdp.exe') -PathType Leaf)) {
        throw "展開後にsdp.exeが見つかりません: $extractedPackage"
    }

    Push-Location $repoRoot
    try {
        Invoke-Checked -Label '5-1. 展開後のlayout検査' -Command {
            uv run python tools/package_layout.py $extractedPackage
        }
    }
    finally {
        Pop-Location
    }

    # 展開後の実行は、ユーザーdataを汚さないよう隔離したLOCALAPPDATAで行う。
    $verifyLocalAppData = Join-Path $temporaryRoot 'local-app-data'
    New-Item -ItemType Directory -Path $verifyLocalAppData | Out-Null
    $env:LOCALAPPDATA = $verifyLocalAppData
    $executable = Join-Path $extractedPackage 'sdp.exe'

    Write-Host '5-2. 展開後のselftest' -ForegroundColor Cyan
    Invoke-PackagedExecutable -ExecutablePath $executable -Arguments @('--selftest') `
        -Label '展開後のselftest'

    Write-Host '5-3. 展開後のcodec test' -ForegroundColor Cyan
    # releaseゲートは6形式すべての実decodeを担保する契約なので、fixture不足は
    # 「未検証だがrelease成功」にせず失敗させる（部分検査は手動--codec-testで行う）。
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
        -Arguments (@('--codec-test') + $fixtures) -Label '展開後のcodec test'
    $env:LOCALAPPDATA = $originalLocalAppData

    Write-Host '=== 5-4. 展開前後のcontent hash一致検証 ===' -ForegroundColor Cyan
    # ZIP化前のdist/sdpと、展開後のsdp/が同じ内容であることを確かめる。
    # 将来ZIP作成や除外規則が変わってarchive内容と食い違っても、ここで検出できる。
    Push-Location $repoRoot
    try {
        Invoke-Checked -Label 'content hash一致検証' -Command {
            uv run python -c @'
import sys
from pathlib import Path

from sdp.release_manifest import compare_package_contents, scan_package

extracted = scan_package(Path(sys.argv[2]))
failures = compare_package_contents(
    scan_package(Path(sys.argv[1])),
    extracted,
    expected_label="dist/sdp",
    actual_label="extracted/sdp",
)
if failures:
    for failure in failures:
        print(f"エラー: {failure}")
    raise SystemExit(1)
print(f"content hash一致: {extracted.content_sha256}")
'@ $packagePath $extractedPackage
        }
    }
    finally {
        Pop-Location
    }

    Write-Host '=== 6. SHA-256とmanifest ===' -ForegroundColor Cyan
    $hash = (Get-FileHash -LiteralPath $stagedArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    $stagedHashFile = "$stagedArchive.sha256"
    # sha256sum互換の1行（ファイル名だけを書き、絶対pathは残さない）。
    [IO.File]::WriteAllText($stagedHashFile, "$hash  $archiveName`n", [Text.UTF8Encoding]::new($false))

    $manifestName = [IO.Path]::GetFileNameWithoutExtension($archiveName) + '.manifest.json'
    $stagedManifest = Join-Path $stagingRoot $manifestName
    Push-Location $repoRoot
    try {
        # manifestは最終配布物を記述するので、ZIPへ入った実内容（展開後）から算出する。
        Invoke-Checked -Label 'manifest生成' -Command {
            uv run python tools/release_manifest.py $extractedPackage $stagedArchive $stagedManifest
        }
    }
    finally {
        Pop-Location
    }

    Write-Host '=== 7. release/への確定 ===' -ForegroundColor Cyan
    # ここまで全部成功したときだけ、同versionの3成果物だけを置換する。
    # 既存release/はそのまま残し、別versionの正常な成果物を壊さない。
    if (-not (Test-Path -LiteralPath $releasePath -PathType Container)) {
        New-Item -ItemType Directory -Path $releasePath | Out-Null
    }
    $stagedItems = @($stagedArchive, $stagedHashFile, $stagedManifest)
    $finalTargets = $stagedItems | ForEach-Object {
        Join-Path $releasePath (Split-Path -Leaf $_)
    }
    # 同名の旧3成果物をbackupへ退避し、3ファイルの移動が全部成功した時点で確定する。
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
    $archivePath = Join-Path $releasePath $archiveName
    $archiveSize = (Get-Item -LiteralPath $archivePath).Length
    Write-Host ''
    Write-Host ("リリース生成に成功しました: {0}" -f $releasePath) -ForegroundColor Green
    Write-Host ("  archive : {0} ({1:N1} MiB)" -f $archiveName, ($archiveSize / 1MB))
    Write-Host ("  sha256  : {0}" -f $hash)
    Write-Host ("  所要時間: {0:N1}秒" -f $timer.Elapsed.TotalSeconds)
}
catch {
    # 失敗しても既存のrelease/へは触れない。今回作ったstagingはfinallyで片付ける。
    throw
}
finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    $env:PYTHONUTF8 = $originalPythonUtf8
    if ($null -ne $temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
    if ($null -ne $stagingRoot -and (Test-Path -LiteralPath $stagingRoot)) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}

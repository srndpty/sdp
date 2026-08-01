# sdp installerのinstall／upgrade／uninstall契約を、実Windows環境上で検査する。
#
#   管理者として起動したPowerShellで:
#   pwsh -File scripts/installer-smoke.ps1 -ConfirmMachineChanges
#
# **このscriptはマシン全体のインストール状態を変更する。**
#   - %ProgramFiles%\sdp へinstall／uninstallする
#   - HKLM配下のsdp関連キーを作成・削除する
#   - 全ユーザー用スタートメニュー／デスクトップのsdpショートカットを作成・削除する
#   - 既にsdpをinstallしている場合、その導入は置き換えられ、最後に削除される
# そのため -ConfirmMachineChanges を必須にしており、CIから無条件に実行してはならない。
# 可能ならWindows Sandboxか検証用の新規Windowsユーザーで実行する。
#
# 変更しないもの: %LOCALAPPDATA%\sdp（ユーザーデータ）、UserChoice（既定アプリ）。

[CmdletBinding()]
param(
    [switch]$ConfirmMachineChanges,
    [string]$SetupExecutable
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $ConfirmMachineChanges) {
    throw @'
このscriptはマシン全体（%ProgramFiles%、HKLM、全ユーザー用ショートカット）を変更します。
内容を確認したうえで -ConfirmMachineChanges を付けて実行してください。
CIから無条件に実行しないでください。
'@
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'installer smokeは管理者として起動したPowerShellから実行してください。'
}

$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$appId = '{8F3B7C21-5D4E-4A96-9C2F-1E7A6B0D3F58}'
$progId = 'sdp.AudioFile'
$extensions = @('.wav', '.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac')
$installDirectory = Join-Path $env:ProgramFiles 'sdp'
$userDataDirectory = Join-Path $env:LOCALAPPDATA 'sdp'
# 実行ごとに固有名にして、途中終了した過去の実行の残骸と取り違えないようにする。
$smokeRunId = [Guid]::NewGuid().ToString('N')
$markerFileName = "installer-smoke-marker-$smokeRunId.txt"
$rollbackMarkerFileName = "installer-smoke-rollback-marker-$smokeRunId.txt"
$markerFile = Join-Path $userDataDirectory $markerFileName
$rollbackMarkerFile = Join-Path $userDataDirectory $rollbackMarkerFileName
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'sdp\sdp.lnk'
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'sdp.lnk'
$uninstallKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\${appId}_is1"
$userChoiceRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts'
$codecFixtureNames = @(
    'sine440.wav', 'sine440.mp3', 'sine440.flac',
    'sine440.ogg', 'sine440.opus', 'sine440.m4a'
)

$script:checkCount = 0

function Assert-True {
    param([Parameter(Mandatory)][bool]$Condition, [Parameter(Mandatory)][string]$Label)

    $script:checkCount++
    if (-not $Condition) {
        throw "検査に失敗しました: $Label"
    }
    Write-Host "  [OK] $Label" -ForegroundColor DarkGray
}

function Get-RegistryValue {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $item = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $item -or -not $item.PSObject.Properties.Name.Contains($Name)) {
        return $null
    }
    return $item.$Name
}

function Get-UserChoiceSnapshot {
    $snapshot = @{}
    foreach ($extension in $extensions) {
        $snapshot[$extension] = Get-RegistryValue `
            -Path "$userChoiceRoot\$extension\UserChoice" -Name 'ProgId'
    }
    return $snapshot
}

function Invoke-Executable {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Label,
        [int]$ExpectedExitCode = 0
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -Wait
    if ($process.ExitCode -ne $ExpectedExitCode) {
        throw "$Label の終了コードが不正です（期待 $ExpectedExitCode、実際 $($process.ExitCode)）"
    }
}

function Assert-InstalledState {
    param([Parameter(Mandatory)][string]$Label, [Parameter(Mandatory)][bool]$ExpectDesktopIcon)

    Write-Host "--- $Label ---" -ForegroundColor Cyan
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $installDirectory 'sdp.exe') -PathType Leaf) `
        -Label 'install先にsdp.exeがある'
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $installDirectory '_internal') -PathType Container) `
        -Label 'install先に_internalがある'
    Assert-True -Condition (Test-Path -LiteralPath (Join-Path $installDirectory 'LICENSE') -PathType Leaf) `
        -Label 'install先にLICENSEがある'
    Assert-True -Condition (-not (Test-Path -LiteralPath (Join-Path $installDirectory 'settings.json'))) `
        -Label 'install先にユーザーデータを置いていない'
    $pythonSources = @(
        Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter '*.py' `
            -ErrorAction SilentlyContinue
    )
    Assert-True -Condition ($pythonSources.Count -eq 0) -Label 'install先にPythonソースがない'

    Assert-True -Condition (Test-Path -LiteralPath $startMenuShortcut -PathType Leaf) `
        -Label 'スタートメニューshortcutがある'
    $startMenuDuplicates = @(Get-ChildItem -LiteralPath (Split-Path -Parent $startMenuShortcut) -Filter '*.lnk')
    Assert-True -Condition ($startMenuDuplicates.Count -eq 1) `
        -Label 'スタートメニューshortcutが重複していない'
    if ($ExpectDesktopIcon) {
        Assert-True -Condition (Test-Path -LiteralPath $desktopShortcut -PathType Leaf) `
            -Label 'desktop shortcutがある（taskを選択した場合）'
    }
    else {
        Assert-True -Condition (-not (Test-Path -LiteralPath $desktopShortcut)) `
            -Label 'desktop shortcutを既定では作らない'
    }

    Assert-True -Condition ((Get-RegistryValue -Path "HKLM:\Software\Classes\$progId\shell\open\command" -Name '(default)') `
            -eq "`"$installDirectory\sdp.exe`" `"%1`"") -Label 'ProgIDのopen commandが正しい'
    Assert-True -Condition ((Get-RegistryValue -Path "HKLM:\Software\Classes\Applications\sdp.exe\shell\open\command" -Name '(default)') `
            -eq "`"$installDirectory\sdp.exe`" `"%1`"") -Label 'Open Withのopen commandが正しい'
    Assert-True -Condition ((Get-RegistryValue -Path 'HKLM:\Software\Classes\Applications\sdp.exe' -Name 'FriendlyAppName') -eq 'sdp') `
        -Label 'Open WithのFriendlyAppNameがある'
    foreach ($extension in $extensions) {
        Assert-True -Condition ($null -ne (Get-RegistryValue -Path "HKLM:\Software\Classes\$extension\OpenWithProgids" -Name $progId)) `
            -Label "$extension のOpenWithProgidsへ登録済み"
        Assert-True -Condition ((Get-RegistryValue -Path 'HKLM:\Software\sdp\Capabilities\FileAssociations' -Name $extension) -eq $progId) `
            -Label "$extension のCapabilities登録がある"
    }
    Assert-True -Condition ((Get-RegistryValue -Path 'HKLM:\Software\RegisteredApplications' -Name 'sdp') -eq 'Software\sdp\Capabilities') `
        -Label 'RegisteredApplicationsへ登録済み'
    Assert-True -Condition (-not (Test-Path -LiteralPath 'HKCU:\Software\Classes\sdp.AudioFile')) `
        -Label 'HKCUへ書いていない'
    Assert-True -Condition (Test-Path -LiteralPath $uninstallKey) `
        -Label 'Apps & Featuresへ登録されている'
}

function Assert-NoUserChoiceChange {
    param([Parameter(Mandatory)][hashtable]$Before)

    $after = Get-UserChoiceSnapshot
    foreach ($extension in $extensions) {
        Assert-True -Condition ($Before[$extension] -eq $after[$extension]) `
            -Label "$extension のUserChoice（既定アプリ）を変更していない"
    }
}

function Get-UserDataFileSnapshot {
    $names = @('settings.json', 'playlist.json', 'ui-state.json', $rollbackMarkerFileName)
    $snapshot = @{}
    foreach ($name in $names) {
        $path = Join-Path $userDataDirectory $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
            $snapshot[$name] = "$((Get-Item -LiteralPath $path).Length):$hash"
        }
        else {
            $snapshot[$name] = '<missing>'
        }
    }
    return $snapshot
}

function Assert-UserDataFileSnapshot {
    param([Parameter(Mandatory)][hashtable]$Before)

    $after = Get-UserDataFileSnapshot
    foreach ($name in $Before.Keys) {
        Assert-True -Condition ($Before[$name] -eq $after[$name]) `
            -Label "upgrade失敗時にユーザーデータ $name が不変"
    }
}

function Get-Uninstaller {
    # uninstaller pathは推測せず、Inno Setupの契約どおりregistryから取得する。
    $quiet = Get-RegistryValue -Path $uninstallKey -Name 'QuietUninstallString'
    if ([string]::IsNullOrWhiteSpace($quiet)) {
        throw 'QuietUninstallStringをregistryから取得できません'
    }
    if ($quiet -notmatch '^"(?<path>[^"]+)"\s*(?<arguments>.*)$') {
        throw "QuietUninstallStringを解釈できません: $quiet"
    }
    return [pscustomobject]@{
        Path      = $Matches['path']
        Arguments = @($Matches['arguments'] -split '\s+' | Where-Object { $_ -ne '' })
    }
}

function Wait-ForRemoval {
    param([Parameter(Mandatory)][string]$Path)

    # uninstallerは自分自身を消すため、非同期の後始末が終わるまで待つ。
    $deadline = (Get-Date).AddSeconds(60)
    while ((Test-Path -LiteralPath $Path) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
}

function Remove-SmokeTempDirectory {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $fullPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "安全でない一時directory削除を拒否しました: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

function Assert-NoRunningProcess {
    $processes = @(Get-Process -Name 'sdp' -ErrorAction SilentlyContinue)
    Assert-True -Condition ($processes.Count -eq 0) -Label 'sdp processが残っていない'
}

# --- 入力の決定 -------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($SetupExecutable)) {
    $candidates = @(
        Get-ChildItem -LiteralPath (Join-Path $repoRoot 'release') -Filter 'sdp-*-setup.exe' `
            -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    )
    if ($candidates.Count -eq 0) {
        throw 'release/ にsetup exeがありません。先にscripts/build-installer.ps1を実行してください。'
    }
    $SetupExecutable = $candidates[0].FullName
}
$setupPath = [IO.Path]::GetFullPath($SetupExecutable)
if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
    throw "setup exeがありません: $setupPath"
}

$fixtures = @()
foreach ($name in $codecFixtureNames) {
    $fixture = Join-Path $repoRoot "assets\test_audio\$name"
    if (-not (Test-Path -LiteralPath $fixture -PathType Leaf)) {
        throw "codec test用音源が不足しています: $name"
    }
    $fixtures += $fixture
}

Write-Host "installer smoke: $([IO.Path]::GetFileName($setupPath))" -ForegroundColor Green
Assert-NoRunningProcess

$userChoiceBefore = Get-UserChoiceSnapshot
$userDataExistedBefore = Test-Path -LiteralPath $userDataDirectory -PathType Container

# --- 0. 既存installの除去 ---------------------------------------------------
# 「初回installではdesktop shortcutを作らない」等はクリーンな状態でしか判定できない。
# Inno Setupはupgrade時に前回選んだtaskを引き継ぐため、まず既存installを取り除く。
if (Test-Path -LiteralPath $uninstallKey) {
    Write-Host '=== 0. 既存installの除去 ===' -ForegroundColor Cyan
    $previous = Get-Uninstaller
    Invoke-Executable -FilePath $previous.Path -Label '既存installのuninstall' `
        -Arguments ($previous.Arguments + @('/SUPPRESSMSGBOXES', '/NORESTART'))
    Wait-ForRemoval -Path $installDirectory
    Assert-True -Condition (-not (Test-Path -LiteralPath $uninstallKey)) `
        -Label '既存installを除去できた'
}

# --- 0.5 初回install先の誤cleanup防止 ---------------------------------------
# uninstall登録が無いdirectoryに偶然 sdp.exe があっても、旧sdp install先と誤認して
# 無関係なファイルを消さないことを確認する。
Write-Host '=== 0.5 初回install先の誤cleanup防止 ===' -ForegroundColor Cyan
$trapDirectory = Join-Path ([IO.Path]::GetTempPath()) "sdp-smoke-first-install-trap-$([Guid]::NewGuid())"
$trapInternal = Join-Path $trapDirectory '_internal'
$trapRootMarker = Join-Path $trapDirectory 'unrelated-root-file.txt'
$trapInternalMarker = Join-Path $trapInternal 'unrelated-runtime-file.dll'
New-Item -ItemType Directory -Path $trapInternal -Force | Out-Null
Set-Content -LiteralPath (Join-Path $trapDirectory 'sdp.exe') -Value 'not sdp' -Encoding ASCII
Set-Content -LiteralPath $trapRootMarker -Value 'keep root' -Encoding UTF8
Set-Content -LiteralPath $trapInternalMarker -Value 'keep internal' -Encoding UTF8
try {
    Invoke-Executable -FilePath $setupPath -Label '既存sdp.exeを含むdirectoryへの初回install' `
        -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=$trapDirectory")
    Assert-True -Condition (Test-Path -LiteralPath $trapRootMarker -PathType Leaf) `
        -Label '初回install先の無関係ファイルが保持されている'
    Assert-True -Condition (Test-Path -LiteralPath $trapInternalMarker -PathType Leaf) `
        -Label '初回install先の無関係_internalファイルが保持されている'
    Assert-NoUserChoiceChange -Before $userChoiceBefore

    $trapUninstall = Get-Uninstaller
    Invoke-Executable -FilePath $trapUninstall.Path -Label '初回install誤cleanup検査後のuninstall' `
        -Arguments ($trapUninstall.Arguments + @('/SUPPRESSMSGBOXES', '/NORESTART'))
    Assert-True -Condition (-not (Test-Path -LiteralPath $uninstallKey)) `
        -Label '初回install誤cleanup検査後にuninstall登録が消えている'
    Assert-NoUserChoiceChange -Before $userChoiceBefore
}
finally {
    Remove-SmokeTempDirectory -Path $trapDirectory
}

# --- 1. silent install ------------------------------------------------------
Write-Host '=== 1. silent install ===' -ForegroundColor Cyan
Invoke-Executable -FilePath $setupPath -Label 'silent install' `
    -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
Assert-InstalledState -Label '初回install後の状態' -ExpectDesktopIcon:$false
Assert-NoUserChoiceChange -Before $userChoiceBefore

# --- 2. install済みexeの検査 -------------------------------------------------
Write-Host '=== 2. install済みexeの検査 ===' -ForegroundColor Cyan
$installedExecutable = Join-Path $installDirectory 'sdp.exe'
Invoke-Executable -FilePath $installedExecutable -Arguments @('--selftest') -Label 'install済みselftest'
Write-Host '  [OK] install済みselftest' -ForegroundColor DarkGray
Invoke-Executable -FilePath $installedExecutable -Arguments (@('--codec-test') + $fixtures) `
    -Label 'install済みcodec test'
Write-Host '  [OK] install済みcodec test（6形式）' -ForegroundColor DarkGray
$versionInfo = (Get-Item -LiteralPath $installedExecutable).VersionInfo
Assert-True -Condition ($versionInfo.ProductName -eq 'sdp') -Label 'version resourceのProductName'
Assert-True -Condition ($versionInfo.InternalName -eq 'sdp') -Label 'version resourceのInternalName'
Assert-True -Condition ($versionInfo.OriginalFilename -eq 'sdp.exe') `
    -Label 'version resourceのOriginalFilename'
Assert-True -Condition (-not [string]::IsNullOrWhiteSpace($versionInfo.FileVersion)) `
    -Label 'version resourceのFileVersion'
$displayVersion = Get-RegistryValue -Path $uninstallKey -Name 'DisplayVersion'
Assert-True -Condition ($versionInfo.ProductVersion -eq $displayVersion) `
    -Label 'Apps & FeaturesのDisplayVersionとexeのversionが一致する'
Assert-NoRunningProcess

# --- 3. same-version reinstall（desktop shortcut task 付き） -----------------
Write-Host '=== 3. same-version reinstall ===' -ForegroundColor Cyan
$obsoleteMarker = Join-Path $installDirectory '_internal\sdp-obsolete-probe.dll'
New-Item -ItemType File -Path $obsoleteMarker -Force | Out-Null
Invoke-Executable -FilePath $setupPath -Label 'same-version reinstall' `
    -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/TASKS=desktopicon')
Assert-InstalledState -Label 'reinstall後の状態' -ExpectDesktopIcon:$true
Assert-True -Condition (-not (Test-Path -LiteralPath $obsoleteMarker)) `
    -Label 'upgrade時に不要になったファイルが残らない'
Assert-NoUserChoiceChange -Before $userChoiceBefore
Invoke-Executable -FilePath $installedExecutable -Arguments @('--selftest') `
    -Label 'reinstall後のselftest'
Write-Host '  [OK] reinstall後のselftest' -ForegroundColor DarkGray

# --- 3.5 cleanup後のupgrade失敗rollback -------------------------------------
Write-Host '=== 3.5 cleanup後のupgrade失敗rollback ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Path $userDataDirectory -Force | Out-Null
Set-Content -LiteralPath $rollbackMarkerFile -Value 'installer smoke rollback marker' -Encoding UTF8
$userDataBeforeRollback = Get-UserDataFileSnapshot
$rollbackInternalProbe = Join-Path $installDirectory '_internal\sdp-rollback-probe.dll'
$rollbackRootProbe = Join-Path $installDirectory 'sdp-rollback-probe.txt'
Set-Content -LiteralPath $rollbackInternalProbe -Value 'restore internal' -Encoding ASCII
Set-Content -LiteralPath $rollbackRootProbe -Value 'restore root' -Encoding ASCII

$failedUpgrade = Start-Process -FilePath $setupPath -PassThru -Wait `
    -ArgumentList @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/SDP_FAIL_AFTER_CLEANUP=1'
    )
Assert-True -Condition ($failedUpgrade.ExitCode -ne 0) `
    -Label 'cleanup後に意図的に展開失敗するとinstallerが非0終了'
Assert-True -Condition (Test-Path -LiteralPath $rollbackInternalProbe -PathType Leaf) `
    -Label 'upgrade失敗後に旧_internalが復元されている'
Assert-True -Condition (Test-Path -LiteralPath $rollbackRootProbe -PathType Leaf) `
    -Label 'upgrade失敗後に旧ルートファイルが復元されている'
Assert-True -Condition (Test-Path -LiteralPath $uninstallKey) `
    -Label 'upgrade失敗後もuninstall情報が残っている'
Assert-True -Condition ((Get-RegistryValue -Path $uninstallKey -Name 'Inno Setup: App Path') -eq $installDirectory) `
    -Label 'upgrade失敗後もuninstall情報のinstall directoryが不変'
Assert-UserDataFileSnapshot -Before $userDataBeforeRollback
Invoke-Executable -FilePath $installedExecutable -Arguments @('--selftest') `
    -Label 'upgrade失敗後のselftest'
Write-Host '  [OK] upgrade失敗後のselftest' -ForegroundColor DarkGray

# rollback検査用のinstall先probeはuninstaller管理外なので、最終uninstall前に片付ける。
Remove-Item -LiteralPath @($rollbackInternalProbe, $rollbackRootProbe) -Force -ErrorAction SilentlyContinue

# --- 4. 起動中のupgrade／uninstall -------------------------------------------
# 起動中のsdpを無断で強制終了せず、旧exeと新DLLが混ざった状態も作らないこと。
Write-Host '=== 4. 起動中のupgrade／uninstall ===' -ForegroundColor Cyan
$uninstall = Get-Uninstaller
$uninstaller = $uninstall.Path
$uninstallArguments = $uninstall.Arguments + @('/SUPPRESSMSGBOXES', '/NORESTART')
Assert-True -Condition (Test-Path -LiteralPath $uninstaller -PathType Leaf) `
    -Label 'registryのuninstaller pathが存在する'

$running = Start-Process -FilePath $installedExecutable -PassThru
try {
    Start-Sleep -Seconds 8
    Assert-True -Condition (-not $running.HasExited) -Label '検査用にsdpを起動できた'

    $blocked = Start-Process -FilePath $setupPath -PassThru -Wait `
        -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART')
    Assert-True -Condition ($blocked.ExitCode -ne 0) -Label '起動中のupgradeは中止される'
    Assert-True -Condition (-not $running.HasExited) `
        -Label '起動中のsdpを無断で強制終了しない'

    $blockedUninstall = Start-Process -FilePath $uninstaller -PassThru -Wait `
        -ArgumentList $uninstallArguments
    Assert-True -Condition ($blockedUninstall.ExitCode -ne 0) -Label '起動中のuninstallは中止される'
    Assert-True -Condition (Test-Path -LiteralPath $installedExecutable -PathType Leaf) `
        -Label '中止されたuninstallでinstall先を壊さない'
}
finally {
    if (-not $running.HasExited) {
        $running.CloseMainWindow() | Out-Null
        if (-not $running.WaitForExit(20000)) {
            $running.Kill()
            $running.WaitForExit(10000) | Out-Null
        }
    }
}
Start-Sleep -Seconds 2
Assert-NoRunningProcess

# --- 5. uninstallとユーザーデータの保持 --------------------------------------
Write-Host '=== 5. uninstall ===' -ForegroundColor Cyan
New-Item -ItemType Directory -Path $userDataDirectory -Force | Out-Null
Set-Content -LiteralPath $markerFile -Value 'installer smoke marker' -Encoding UTF8

Invoke-Executable -FilePath $uninstaller -Label 'silent uninstall' -Arguments $uninstallArguments
Wait-ForRemoval -Path $installDirectory

# --- 6. uninstall後の状態 ---------------------------------------------------
Assert-True -Condition (-not (Test-Path -LiteralPath $installDirectory)) `
    -Label 'install directoryが削除された'
Assert-True -Condition (-not (Test-Path -LiteralPath $startMenuShortcut)) `
    -Label 'スタートメニューshortcutが削除された'
Assert-True -Condition (-not (Test-Path -LiteralPath $desktopShortcut)) `
    -Label 'desktop shortcutが削除された'
Assert-True -Condition (-not (Test-Path -LiteralPath "HKLM:\Software\Classes\$progId")) `
    -Label 'ProgIDが削除された'
Assert-True -Condition (-not (Test-Path -LiteralPath 'HKLM:\Software\Classes\Applications\sdp.exe')) `
    -Label 'Open With登録が削除された'
Assert-True -Condition (-not (Test-Path -LiteralPath 'HKLM:\Software\sdp\Capabilities')) `
    -Label 'Capabilitiesが削除された'
Assert-True -Condition ($null -eq (Get-RegistryValue -Path 'HKLM:\Software\RegisteredApplications' -Name 'sdp')) `
    -Label 'RegisteredApplicationsの値が削除された'
foreach ($extension in $extensions) {
    Assert-True -Condition ($null -eq (Get-RegistryValue -Path "HKLM:\Software\Classes\$extension\OpenWithProgids" -Name $progId)) `
        -Label "$extension のOpenWithProgidsから削除された"
}
Assert-True -Condition (-not (Test-Path -LiteralPath $uninstallKey)) `
    -Label 'Apps & Featuresの登録が削除された'
Assert-NoUserChoiceChange -Before $userChoiceBefore
Assert-NoRunningProcess

Assert-True -Condition (Test-Path -LiteralPath $userDataDirectory -PathType Container) `
    -Label 'ユーザーデータdirectoryが保持されている'
Assert-True -Condition (Test-Path -LiteralPath $markerFile -PathType Leaf) `
    -Label 'ユーザーデータのファイルが保持されている'

# smoke専用のmarkerだけ片付ける。%LOCALAPPDATA%\sdp 自体は残す（uninstall契約と同じ）。
Remove-Item -LiteralPath $markerFile -Force
if (Test-Path -LiteralPath $rollbackMarkerFile -PathType Leaf) {
    Remove-Item -LiteralPath $rollbackMarkerFile -Force
}
if (-not $userDataExistedBefore) {
    Write-Host "  注意: 検査のため $userDataDirectory を作成しました（削除していません）。" `
        -ForegroundColor Yellow
}

Write-Host ''
Write-Host ("installer smokeに成功しました（検査{0}件）。" -f $script:checkCount) -ForegroundColor Green
Write-Host 'UserChoice（既定アプリ）は変更していません。' -ForegroundColor Green

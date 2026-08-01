# per-machine版sdpを、実際に利用する非昇格ユーザーの権限で検査する。
#
#   通常権限のPowerShellで:
#   pwsh -File scripts/installer-user-smoke.ps1 -ConfirmUserProfileChanges
#
# --selftestは%LOCALAPPDATA%\sdp\logsへ書き込むため、明示確認を必須にする。

[CmdletBinding()]
param(
    [switch]$ConfirmUserProfileChanges
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $ConfirmUserProfileChanges) {
    throw @'
このscriptは%LOCALAPPDATA%\sdp\logsへselftestのログを書き込みます。
内容を確認したうえで -ConfirmUserProfileChanges を付けて実行してください。
'@
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '通常ユーザーの利用条件を検査するため、管理者ではないPowerShellから実行してください。'
}

$installDirectory = Join-Path $env:ProgramFiles 'sdp'
$executable = Join-Path $installDirectory 'sdp.exe'
$logFile = Join-Path $env:LOCALAPPDATA 'sdp\logs\sdp.log'
$openCommandKey = 'HKLM:\Software\Classes\sdp.AudioFile\shell\open\command'
$writeProbe = Join-Path $installDirectory ".sdp-user-write-probe-$([Guid]::NewGuid()).tmp"

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Program Files版のsdp.exeがありません: $executable"
}

$process = Start-Process -FilePath $executable -ArgumentList @('--selftest') `
    -PassThru -Wait -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "非昇格ユーザーのselftestに失敗しました（exit code $($process.ExitCode)）"
}
if (-not (Test-Path -LiteralPath $logFile -PathType Leaf)) {
    throw "ユーザー別ログを作成できませんでした: $logFile"
}

$openCommand = (Get-Item -LiteralPath $openCommandKey -ErrorAction Stop).GetValue('')
$expectedOpenCommand = "`"$executable`" `"%1`""
if ($openCommand -cne $expectedOpenCommand) {
    throw "HKLMの関連付けcommandが不正です: $openCommand"
}

$writeSucceeded = $false
try {
    Set-Content -LiteralPath $writeProbe -Value 'write must be denied' -Encoding ASCII
    $writeSucceeded = $true
}
catch [UnauthorizedAccessException] {
    # 期待どおり。per-machineのinstall先は通常ユーザーから書き込めない。
}
finally {
    if (Test-Path -LiteralPath $writeProbe -PathType Leaf) {
        Remove-Item -LiteralPath $writeProbe -Force -ErrorAction SilentlyContinue
    }
}
if ($writeSucceeded) {
    throw "通常ユーザーがProgram Filesのinstall先へ書き込めます: $installDirectory"
}

$remaining = @(Get-Process -Name 'sdp' -ErrorAction SilentlyContinue)
if ($remaining.Count -ne 0) {
    throw 'selftest終了後もsdp processが残っています。'
}

Write-Host '非昇格ユーザーのinstaller検査に成功しました。' -ForegroundColor Green
Write-Host "  executable: $executable"
Write-Host "  user log : $logFile"
Write-Host '  Program Filesへの書き込み: 拒否'

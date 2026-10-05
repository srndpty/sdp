"""ローカル導入の制御フローを、マシンを変更せず検証する。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "install-local.ps1"


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("success", 0),
        ("build_failure", 9),
        ("install_failure", 7),
        ("selftest_failure", 5),
        ("cancel", 1223),
        ("running", 1),
    ],
)
def test_local_install_flow(tmp_path: Path, scenario: str, expected: int) -> None:
    """ビルド・導入・自己診断の順序と失敗時の停止を確認する。"""
    shell = shutil.which("pwsh")
    assert shell is not None
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "install-local.ps1"
    shutil.copyfile(_SCRIPT, script)
    release = tmp_path / "release"
    release.mkdir()
    (release / "sdp-test-windows-x64-setup.exe").touch()
    (release / "sdp-test-windows-x64-installer.manifest.json").write_text(
        json.dumps({"app_id": "{test}"}), encoding="utf-8"
    )
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        r"""
param($Scenario, $ScriptPath)
$ErrorActionPreference = 'Stop'
function Get-Process { if ($Scenario -eq 'running') { 'sdp' } }
function pwsh {
    if ($args -notcontains '-SkipBuild' -or $args -notcontains 'C:\Compiler Path\ISCC.exe') {
        throw 'ビルド引数が転送されていません'
    }
    $global:LASTEXITCODE = if ($Scenario -eq 'build_failure') { 9 } else { 0 }
}
function uv {
    if ($Scenario -eq 'build_failure') { throw '失敗後に処理が続いています' }
    $global:LASTEXITCODE = 0
    'sdp-test-windows-x64-setup.exe'
}
function Start-Process {
    param($FilePath, $Verb, [switch]$Wait, [switch]$PassThru, $WindowStyle, $ArgumentList)
    if ($Verb -eq 'RunAs') {
        if ($Scenario -eq 'cancel') {
            throw [ComponentModel.Win32Exception]::new(1223)
        }
        if ($ArgumentList -notcontains '/VERYSILENT') { throw '導入引数が不正です' }
        return [pscustomobject]@{
            ExitCode = $(if ($Scenario -eq 'install_failure') { 7 } else { 0 }) }
    }
    if ($Scenario -eq 'install_failure') { throw '失敗後に自己診断しています' }
    if ($ArgumentList -ne '--selftest' -or $FilePath -ne 'C:\Custom Install\sdp.exe') {
        throw '登録済み導入先で自己診断していません'
    }
    [pscustomobject]@{ ExitCode = $(if ($Scenario -eq 'selftest_failure') { 5 } else { 0 }) }
}
function Get-ItemProperty {
    param($LiteralPath)
    if ($LiteralPath -notlike '*{test}_is1') { throw '登録キーが不正です' }
    [pscustomobject]@{ 'Inno Setup: App Path' = 'C:\Custom Install' }
}
& $ScriptPath -SkipBuild -InnoSetupCompiler 'C:\Compiler Path\ISCC.exe'
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(harness), scenario, str(script)],
        cwd=scripts,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == expected, result.stdout + result.stderr

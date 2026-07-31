"""installerのbuild scriptとsmoke scriptの契約を検証する。

Inno Setup compilerと実インストールを伴う経路はCIで実行できないため、ここでは
「script自体が守ると宣言している安全側の性質」をテキストとして検査する。
実際のcompileとinstallは `scripts/build-installer.ps1` と
`scripts/installer-smoke.ps1` を手動実行して確認する
（[docs/testing-strategy.md](../../docs/testing-strategy.md)）。
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[2]
_BUILD_SCRIPT = _REPO_ROOT / "scripts" / "build-installer.ps1"
_SMOKE_SCRIPT = _REPO_ROOT / "scripts" / "installer-smoke.ps1"


@pytest.fixture(scope="module")
def build_script() -> str:
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def smoke_script() -> str:
    return _SMOKE_SCRIPT.read_text(encoding="utf-8")


def test_build_script_runs_from_any_directory(build_script: str) -> None:
    """呼び出し元のcwdに依存せず、$PSScriptRoot基準でpathを解決する。"""
    assert "$PSScriptRoot" in build_script
    assert "Split-Path -Parent $PSScriptRoot" in build_script
    assert "$ErrorActionPreference = 'Stop'" in build_script
    assert "Set-StrictMode -Version Latest" in build_script


def test_build_script_takes_version_from_pyproject_only(build_script: str) -> None:
    """versionはsdp.__version__（=pyproject）から取り、scriptへ手書きしない。"""
    assert "from sdp import __version__" in build_script
    assert "windows_file_version" in build_script
    assert "/DAppVersion=$version" in build_script
    assert "/DVersionInfoVersion=$versionInfoVersion" in build_script
    assert "0.0.1" not in build_script


def test_build_script_verifies_the_release_package_before_compiling(build_script: str) -> None:
    """ZIP配布物と同一内容であることを確かめてからcompileする。"""
    assert "build-release.ps1" in build_script
    assert "content_sha256" in build_script
    assert "tools/package_layout.py" in build_script
    assert "tools/license_audit.py" in build_script
    assert "tools/installer_contract.py" in build_script
    assert "--selftest" in build_script
    assert "--codec-test" in build_script
    assert "/DSourceDir=$packagePath" in build_script


def test_build_script_requires_all_six_codec_fixtures(build_script: str) -> None:
    """releaseゲートと同じ6形式のcodec fixtureを必須にする。"""
    for name in (
        "sine440.wav",
        "sine440.mp3",
        "sine440.flac",
        "sine440.ogg",
        "sine440.opus",
        "sine440.m4a",
    ):
        assert name in build_script
    assert "codec test用音源が不足しています" in build_script


def test_build_script_stages_outside_release_and_preserves_prior_artifacts(
    build_script: str,
) -> None:
    """stagingはrelease/の外。失敗しても既存の正常なinstallerを消さない。"""
    assert "installer-staging-" in build_script
    assert "Join-Path $repoRoot 'tmp'" in build_script
    # 置換に失敗したら退避した旧成果物を戻す。
    assert "Move-Item -LiteralPath $pair[0] -Destination $pair[1]" in build_script
    assert "$backupRoot" in build_script
    # release/ 配下を無条件に削除する記述を持たない。
    assert "Remove-Item -LiteralPath $releasePath" not in build_script
    assert "安全でないrelease targetです" in build_script


def test_build_script_reports_missing_compiler_clearly(build_script: str) -> None:
    """Inno Setup未導入時に、対処が分かるエラーを返す。"""
    assert "ISCC.exe" in build_script
    assert "INNO_SETUP_COMPILER" in build_script
    assert "-InnoSetupCompiler" in build_script
    assert "JRSoftware.InnoSetup" in build_script
    assert "Inno Setup compiler（ISCC.exe）が見つかりません" in build_script


def test_build_script_propagates_compiler_exit_code(build_script: str) -> None:
    """compile失敗をexit codeで判定し、成果物を作らない。"""
    assert "Inno Setupのcompileに失敗しました（exit code $LASTEXITCODE）" in build_script
    assert "setup exeを生成できませんでした" in build_script


def test_build_script_writes_hash_and_manifest(build_script: str) -> None:
    """SHA-256（sha256sum互換の1行）とinstaller manifestを生成する。"""
    assert "Get-FileHash -LiteralPath $stagedInstaller -Algorithm SHA256" in build_script
    assert '"$hash  $installerName' in build_script
    assert "tools/installer_manifest.py" in build_script
    assert "installer.manifest.json" in build_script


def test_build_script_states_the_build_is_for_technical_verification(build_script: str) -> None:
    """公開配布可能と表現しない。"""
    assert "技術検証用" in build_script
    assert "docs/distribution-licenses.md" in build_script
    assert "公開配布" in build_script


def test_smoke_script_requires_explicit_confirmation(smoke_script: str) -> None:
    """実プロファイルを変更するため、専用フラグを必須にする。"""
    assert "ConfirmProfileChanges" in smoke_script
    assert "CIから無条件に実行しないでください" in smoke_script


def test_smoke_script_checks_the_full_install_lifecycle(smoke_script: str) -> None:
    """install・reinstall・uninstall・ユーザーデータ保持まで自動確認する。"""
    for expectation in (
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "QuietUninstallString",
        "install directoryが削除された",
        "ユーザーデータのファイルが保持されている",
        "sdp processが残っていない",
        "HKLMへ書いていない",
        "upgrade時に不要になったファイルが残らない",
        "初回install先の無関係ファイルが保持されている",
        "既存sdp.exeを含むdirectoryへの初回install",
        "cleanup後に意図的に展開失敗するとinstallerが非0終了",
        "upgrade失敗後のselftest",
        "upgrade失敗時にユーザーデータ",
        "upgrade失敗後もuninstall情報が残っている",
        "既存installを除去できた",
    ):
        assert expectation in smoke_script
    assert "UserChoice" in smoke_script
    assert "Get-UserChoiceSnapshot" in smoke_script


def test_smoke_script_checks_the_running_instance_contract(smoke_script: str) -> None:
    """起動中のupgrade／uninstallが中止され、無断で強制終了されないことを確認する。"""
    for expectation in (
        "起動中のupgradeは中止される",
        "起動中のsdpを無断で強制終了しない",
        "起動中のuninstallは中止される",
        "中止されたuninstallでinstall先を壊さない",
    ):
        assert expectation in smoke_script
    # 強制終了を促すフラグは使わない。
    assert "FORCECLOSEAPPLICATIONS" not in smoke_script.upper()


def test_smoke_script_never_deletes_user_data(smoke_script: str) -> None:
    """ユーザーデータdirectory自体は消さない（uninstall契約と同じ扱い）。"""
    assert "Remove-Item -LiteralPath $userDataDirectory" not in smoke_script
    assert "Remove-Item -LiteralPath $markerFile -Force" in smoke_script


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShellの契約確認はWindowsでのみ行う")
def test_smoke_script_refuses_to_run_without_confirmation(tmp_path: Path) -> None:
    """フラグなしで実行すると、何もせず失敗する。"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(_SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=tmp_path,
        check=False,
    )

    assert completed.returncode != 0
    assert "ConfirmProfileChanges" in completed.stderr

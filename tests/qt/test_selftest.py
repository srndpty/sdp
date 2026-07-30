"""実Qt objectを構築する配布版selftestを検証する。"""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from sdp import selftest


def test_selftest_succeeds_without_window_or_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Window・再生・保存sessionを始めずQt依存と書込先だけ診断する。"""
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    before = set(QApplication.topLevelWidgets())

    result = selftest.run_selftest(["sdp.exe", "--selftest"])

    assert result == selftest.SELFTEST_SUCCESS
    assert set(QApplication.topLevelWidgets()) == before
    app_directory = local_app_data / "sdp"
    assert (app_directory / "logs" / "sdp.log").is_file()
    assert not (app_directory / "settings.json").exists()
    assert not (app_directory / "playlist.json").exists()
    assert not (app_directory / "ui-state.json").exists()
    assert not (app_directory / "cache").exists()
    assert list(app_directory.glob(".sdp-selftest-*")) == []


def test_selftest_dependency_failure_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依存構築失敗をログへ残し、固定コード1を返す。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fail_dependencies(application: QApplication) -> None:
        del application
        raise RuntimeError("Qt Multimediaを構築できません")

    monkeypatch.setattr(selftest, "_check_qt_dependencies", fail_dependencies)

    assert selftest.run_selftest(["sdp.exe", "--selftest"]) == (
        selftest.SELFTEST_DEPENDENCY_FAILURE
    )

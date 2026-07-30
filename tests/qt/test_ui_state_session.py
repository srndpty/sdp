"""UiStateSessionの復元適用・デバウンス保存・障害分離を検証する。

MainWindowの実インスタンスを使わず、UiStateHolder契約を満たす小さなfakeで
セッション側の責務だけを確かめる（geometryの取得はMainWindowのテストで行う）。
"""

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from sdp.services.ui_state import (
    RESTORE_FAILED_MESSAGE,
    SplitterState,
    UiState,
    WindowState,
    load_ui_state,
    save_ui_state,
)
from sdp.services.ui_state_session import UiStateSession

DEBOUNCE_MS = 20
WINDOW = WindowState(x=120, y=80, width=960, height=760, maximized=False)


class FakeWindow(QObject):
    """UiStateHolder契約だけを満たすテスト用Window。"""

    ui_state_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.state = UiState()
        self.restored: list[UiState] = []
        self.capture_error: Exception | None = None

    def capture_ui_state(self) -> UiState:
        if self.capture_error is not None:
            raise self.capture_error
        return self.state

    def restore_ui_state(self, state: UiState) -> None:
        self.restored.append(state)
        self.state = state

    def connect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        self.ui_state_changed.connect(slot)

    def disconnect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        self.ui_state_changed.disconnect(slot)

    def change_state(self, state: UiState) -> None:
        """ユーザー操作でUI状態が変わったことにする。"""
        self.state = state
        self.ui_state_changed.emit()


@pytest.fixture
def window(qtbot: QtBot) -> Iterator[FakeWindow]:
    del qtbot
    yield FakeWindow()


def make_session(path: Path, window: FakeWindow) -> UiStateSession:
    return UiStateSession(path, window, debounce_ms=DEBOUNCE_MS, retry_ms=DEBOUNCE_MS)


def recording_save(saved: list[UiState]) -> Callable[[Path, UiState], None]:
    def record(path: Path, state: UiState) -> None:
        del path
        saved.append(state)

    return record


# -- 復元 -------------------------------------------------------------------


def test_missing_file_restores_the_default_state(tmp_path: Path, window: FakeWindow) -> None:
    """未作成なら既定状態で起動し、保存も有効なまま。"""
    session = make_session(tmp_path / "ui-state.json", window)

    assert session.load_into_window() is None

    assert window.restored == [UiState()]
    assert session.is_save_enabled
    assert not session.is_running


def test_saved_state_is_applied_to_the_window(tmp_path: Path, window: FakeWindow) -> None:
    """保存済み状態をWindowへ適用する。"""
    path = tmp_path / "ui-state.json"
    expected = UiState(
        window=WINDOW,
        main_splitter=SplitterState(400, 300),
        last_open_directory=Path("C:\\Music"),
    )
    save_ui_state(path, expected)
    session = make_session(path, window)

    assert session.load_into_window() is None

    assert window.restored == [expected]


def test_restore_does_not_schedule_a_save(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """復元の適用を保存契機にしない。"""
    path = tmp_path / "ui-state.json"
    save_ui_state(path, UiState(window=WINDOW))
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(path, window)

    session.load_into_window()
    session.start()
    qtbot.wait(DEBOUNCE_MS * 3)

    assert saved == []


def test_build_does_not_start_monitoring(tmp_path: Path, window: FakeWindow) -> None:
    """生成しただけでは監視を始めない。"""
    session = make_session(tmp_path / "ui-state.json", window)

    assert not session.is_running
    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]


# -- デバウンス保存 ---------------------------------------------------------


def test_start_is_idempotent_and_continuous_changes_save_once(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """連続する移動・リサイズは最後のsnapshotだけを1回保存する。"""
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()
    session.start()

    for index in range(4):
        window.change_state(UiState(window=WindowState(100 + index, 50, 900, 700, maximized=False)))

    qtbot.waitUntil(lambda: len(saved) == 1, timeout=2_000)
    assert saved == [UiState(window=WindowState(103, 50, 900, 700, maximized=False))]
    session.stop()


@pytest.mark.parametrize(
    "state",
    [
        UiState(window=WindowState(10, 20, 800, 600, maximized=False)),
        UiState(window=WindowState(120, 80, 960, 760, maximized=True)),
        UiState(main_splitter=SplitterState(500, 200)),
        UiState(last_open_directory=Path("C:\\音 楽")),
    ],
)
def test_every_kind_of_change_is_debounced_and_saved(
    tmp_path: Path, window: FakeWindow, qtbot: QtBot, state: UiState
) -> None:
    """移動・リサイズ・最大化・Splitter・前回フォルダーのどれでも保存する。"""
    path = tmp_path / "ui-state.json"
    session = make_session(path, window)
    session.load_into_window()
    session.start()

    window.change_state(state)

    qtbot.waitUntil(path.is_file, timeout=2_000)
    assert load_ui_state(path) == state
    session.stop()


def test_same_snapshot_is_not_written_again(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """変更がなければファイルを書き換えない。"""
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()
    window.change_state(UiState(window=WINDOW))

    assert session.flush() is True
    assert session.flush() is False

    assert saved == [UiState(window=WINDOW)]
    session.stop()


def test_flush_saves_immediately_and_stops_the_timer(tmp_path: Path, window: FakeWindow) -> None:
    """flushは待機中の変更を即時保存し、タイマーを止める。"""
    path = tmp_path / "ui-state.json"
    session = make_session(path, window)
    session.load_into_window()
    session.start()
    window.change_state(UiState(window=WINDOW))
    assert session._timer.isActive()  # pyright: ignore[reportPrivateUsage]

    assert session.flush() is True

    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]
    assert load_ui_state(path).window == WINDOW
    session.stop()


def test_maximize_and_restore_bursts_save_only_the_last_snapshot(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """最大化・復元の連続イベントでも最終状態だけを保存する。"""
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()

    window.change_state(UiState(window=WindowState(120, 80, 960, 760, maximized=True)))
    window.change_state(UiState(window=WindowState(2, 2, 1900, 1000, maximized=True)))
    window.change_state(UiState(window=WindowState(120, 80, 960, 760, maximized=False)))

    qtbot.waitUntil(lambda: len(saved) == 1, timeout=2_000)
    assert saved == [UiState(window=WindowState(120, 80, 960, 760, maximized=False))]
    session.stop()


# -- 障害 -------------------------------------------------------------------


@pytest.mark.parametrize("content", ["{壊れた", "[]", '{"schema_version": 99}'])
def test_load_failure_disables_saving_and_keeps_the_original(
    tmp_path: Path, window: FakeWindow, content: str, caplog: pytest.LogCaptureFixture
) -> None:
    """破損時は既定状態で起動し、その起動では元ファイルを上書きしない。"""
    path = tmp_path / "ui-state.json"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    session = make_session(path, window)

    with caplog.at_level(logging.ERROR):
        message = session.load_into_window()
    session.start()
    window.change_state(UiState(window=WINDOW))

    assert message == RESTORE_FAILED_MESSAGE
    assert not session.is_save_enabled
    assert session.flush() is False
    assert path.read_bytes() == original
    assert "復元に失敗" in caplog.text
    assert window.restored == []
    session.stop()


def test_save_failure_is_logged_and_retried_once(
    tmp_path: Path,
    window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """デバウンス保存の一時失敗は1回だけ自動再試行する。"""
    calls: list[UiState] = []
    saved: list[UiState] = []

    def flaky_save(path: Path, state: UiState) -> None:
        del path
        calls.append(state)
        if len(calls) == 1:
            raise OSError("一時的な共有違反")
        saved.append(state)

    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", flaky_save)
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()

    with caplog.at_level(logging.INFO):
        window.change_state(UiState(window=WINDOW))
        qtbot.waitUntil(lambda: len(saved) == 1, timeout=2_000)

    assert len(calls) == 2
    assert "再試行" in caplog.text
    session.stop()


def test_capture_failure_after_window_destruction_is_safe(
    tmp_path: Path, window: FakeWindow, caplog: pytest.LogCaptureFixture
) -> None:
    """Windowが破棄済みでもflushは例外を出さず、終了処理を止めない。"""
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    window.capture_error = RuntimeError("Internal C++ object already deleted.")

    with caplog.at_level(logging.WARNING):
        assert session.flush() is False

    assert "破棄済み" in caplog.text
    assert not (tmp_path / "ui-state.json").exists()


# -- 停止と後始末 -----------------------------------------------------------


def test_stop_is_idempotent_and_prevents_new_saves(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """stop後は変更通知を監視せず、保存も予約しない。"""
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()

    session.stop()
    session.stop()
    window.change_state(UiState(window=WINDOW))
    qtbot.wait(DEBOUNCE_MS * 3)

    assert not session.is_running
    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]
    assert saved == []


def test_schedule_after_stop_does_nothing(tmp_path: Path, window: FakeWindow) -> None:
    """stop後にschedule_saveを直接呼んでも予約しない。"""
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()
    session.stop()

    session.schedule_save()

    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]


def test_deleted_session_cancels_the_pending_timer(
    tmp_path: Path, window: FakeWindow, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """QObject削除で待機タイマーも無効化され、遅延保存しない。"""
    saved: list[UiState] = []
    monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", recording_save(saved))
    session = make_session(tmp_path / "ui-state.json", window)
    session.load_into_window()
    session.start()
    window.change_state(UiState(window=WINDOW))
    timer = session._timer  # pyright: ignore[reportPrivateUsage]

    session.deleteLater()
    qtbot.waitUntil(lambda: not isValid(session))

    assert not isValid(timer)
    assert saved == []


# -- 他の保存ファイルとの独立性 ---------------------------------------------


def test_ui_state_failure_does_not_block_the_settings_file(
    tmp_path: Path, window: FakeWindow
) -> None:
    """ui-stateの破損はsettings.jsonの保存を妨げない。"""
    from sdp.services.settings import AppSettings, save_settings

    ui_state_path = tmp_path / "ui-state.json"
    ui_state_path.write_text("{壊れた", encoding="utf-8")
    settings_path = tmp_path / "settings.json"
    session = make_session(ui_state_path, window)

    assert session.load_into_window() == RESTORE_FAILED_MESSAGE

    save_settings(settings_path, AppSettings(1.25, True))
    assert json.loads(settings_path.read_text(encoding="utf-8"))["playback_rate"] == 1.25
    assert ui_state_path.read_text(encoding="utf-8") == "{壊れた"


def test_settings_failure_does_not_block_ui_state_saving(
    tmp_path: Path, window: FakeWindow
) -> None:
    """settings.jsonの破損はui-state.jsonの保存を妨げない。"""
    from sdp.services.settings import AppSettings, SettingsFileError, load_settings

    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{壊れた", encoding="utf-8")
    ui_state_path = tmp_path / "ui-state.json"
    session = make_session(ui_state_path, window)
    session.load_into_window()
    session.start()

    with pytest.raises(SettingsFileError):
        load_settings(settings_path, AppSettings(1.0, True))
    window.change_state(UiState(window=WINDOW))

    assert session.flush() is True
    assert load_ui_state(ui_state_path).window == WINDOW
    session.stop()

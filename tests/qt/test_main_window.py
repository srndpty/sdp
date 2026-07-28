"""MainWindow の責務を FakeBackend + PlaybackController で検証する。

ネイティブのファイルダイアログは開かず、`QFileDialog.getOpenFileName` を差し替える。
"""

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
)
from sdp.ui import main_window as main_window_module
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def window(controller: PlaybackController, qtbot: QtBot) -> Iterator[MainWindow]:
    main = MainWindow(controller)
    qtbot.addWidget(main)
    yield main


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "テスト 音源.wav"
    path.write_bytes(b"RIFF----WAVEfmt ")
    return path


def file_name_text(window: MainWindow) -> str:
    label = window.findChild(QLabel, "fileNameLabel")
    assert label is not None
    return label.text()


def stub_open_dialog(selected: str) -> Callable[..., tuple[str, str]]:
    """`QFileDialog.getOpenFileName` の差し替え。空文字はキャンセルを表す。"""

    def _dialog(*args: object, **kwargs: object) -> tuple[str, str]:
        del args, kwargs
        return (selected, "")

    return _dialog


def action_of(window: MainWindow, name: str) -> QAction:
    action = window.findChild(QAction, name)
    assert action is not None, name
    return action


# -- 依存の向き -------------------------------------------------------------


def test_main_window_only_needs_a_controller() -> None:
    """MainWindow が受け取るのは PlaybackController（と親）だけ。"""
    parameters = list(inspect.signature(MainWindow.__init__).parameters)
    assert parameters == ["self", "controller", "parent"]


def test_main_window_module_does_not_import_the_qt_backend() -> None:
    """MainWindow のモジュールが具体的な Backend を参照していない。"""
    assert not hasattr(main_window_module, "QtMultimediaBackend")
    assert not hasattr(main_window_module, "QMediaPlayer")


def test_main_window_delegates_transport_to_player_controls(window: MainWindow) -> None:
    """再生操作は PlayerControls へ委譲し、MainWindow は持たない。"""
    assert window.findChild(PlayerControls) is not None
    for forbidden in ("play", "pause", "stop", "seek", "set_volume"):
        assert not hasattr(window, forbidden), forbidden


# -- ファイルを開く ---------------------------------------------------------


def test_cancelled_dialog_does_not_load(
    window: MainWindow, backend: FakePlaybackBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイル選択をキャンセルしたら何もしない。"""
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(""))

    window.open_file()

    assert backend.call_names() == []


def test_selected_file_is_loaded_as_path(
    window: MainWindow,
    backend: FakePlaybackBackend,
    audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選択したファイルが Path として Controller へ渡る。"""
    monkeypatch.setattr(
        main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(str(audio_file))
    )

    window.open_file()

    assert backend.call_args("load") == [(audio_file.resolve(),)]


def test_all_files_filter_is_available() -> None:
    """拡張子で再生可否を断定しないため「すべてのファイル」を選べる。"""
    assert "すべてのファイル (*)" in main_window_module.FILE_DIALOG_FILTER


def test_source_change_updates_file_name_and_title(
    window: MainWindow, controller: PlaybackController, audio_file: Path
) -> None:
    """source_changed でファイル名表示・ツールチップ・タイトルが更新される。"""
    controller.load(audio_file)

    assert file_name_text(window) == audio_file.name
    assert window.windowTitle() == f"sdp — {audio_file.name}"
    label = window.findChild(QLabel, "fileNameLabel")
    assert label is not None
    assert label.toolTip() == str(audio_file.resolve())


def test_cleared_source_restores_the_initial_title(
    window: MainWindow, controller: PlaybackController, audio_file: Path
) -> None:
    """source が無くなったらファイル名表示とタイトルを初期状態へ戻す。"""
    controller.load(audio_file)

    controller.source_changed.emit(None)

    assert file_name_text(window) == main_window_module.NO_FILE_TEXT
    assert window.windowTitle() == main_window_module.WINDOW_TITLE
    assert window.statusBar().currentMessage() == "音声ファイルを開いてください。"


# -- ステータス表示 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MediaStatus.LOADING, "読み込み中..."),
        (MediaStatus.LOADED, "読み込み完了"),
        (MediaStatus.BUFFERED, "読み込み完了"),
        (MediaStatus.STALLED, "再生が一時的に停止しています"),
        (MediaStatus.BUFFERING, "バッファリング中..."),
        (MediaStatus.END_OF_MEDIA, "再生終了"),
        (MediaStatus.INVALID_MEDIA, "音声ファイルを読み込めませんでした"),
    ],
)
def test_media_status_updates_the_status_bar(
    window: MainWindow,
    backend: FakePlaybackBackend,
    status: MediaStatus,
    expected: str,
) -> None:
    """MediaStatus に応じてステータスバーが更新される。"""
    backend.emit_media_status(status)

    assert window.statusBar().currentMessage() == expected


def test_error_message_is_shown_without_technical_detail(
    window: MainWindow, backend: FakePlaybackBackend
) -> None:
    """エラーは message だけを表示し、detail を画面へ出さない。"""
    error = PlaybackError(
        code=PlaybackErrorCode.FORMAT_ERROR,
        message="この音声形式は再生できません。",
        detail="QMediaPlayer.Error.FormatError / errorString='unsupported codec'",
    )

    backend.emit_error(error)

    assert window.statusBar().currentMessage() == error.message
    assert error.detail not in window.statusBar().currentMessage()
    assert error.detail not in file_name_text(window)


@pytest.mark.parametrize("error_first", [True, False])
def test_specific_error_wins_over_invalid_media_status(
    window: MainWindow, backend: FakePlaybackBackend, error_first: bool
) -> None:
    """通知順にかかわらず、INVALID_MEDIAより具体的な再生エラーを表示する。"""
    error = PlaybackError(
        code=PlaybackErrorCode.ACCESS_DENIED,
        message="音声ファイルへのアクセスが拒否されました。",
        detail="QMediaPlayer.AccessDeniedError",
    )

    if error_first:
        backend.emit_error(error)
        backend.emit_media_status(MediaStatus.INVALID_MEDIA)
    else:
        backend.emit_media_status(MediaStatus.INVALID_MEDIA)
        backend.emit_error(error)

    assert window.statusBar().currentMessage() == error.message


def test_new_source_clears_specific_error_priority(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """source変更後のINVALID_MEDIAは新しいsourceの一般エラーとして表示する。"""
    backend.emit_error(
        PlaybackError(
            code=PlaybackErrorCode.FORMAT_ERROR,
            message="この音声形式は再生できません。",
            detail="old source",
        )
    )

    controller.load(audio_file)
    backend.emit_media_status(MediaStatus.INVALID_MEDIA)

    assert window.statusBar().currentMessage() == "音声ファイルを読み込めませんでした"


# -- メニュー ---------------------------------------------------------------


def test_quit_action_closes_the_window(window: MainWindow) -> None:
    """終了アクションでウィンドウが閉じる。"""
    window.show()
    assert window.isVisible()

    action_of(window, "quitAction").trigger()

    assert not window.isVisible()


def test_open_action_opens_the_file_dialog(
    window: MainWindow,
    backend: FakePlaybackBackend,
    audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「開く...」アクションからファイル選択が始まる。"""
    monkeypatch.setattr(
        main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(str(audio_file))
    )

    action_of(window, "openAction").trigger()

    assert backend.call_args("load") == [(audio_file.resolve(),)]

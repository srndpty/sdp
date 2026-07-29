"""MainWindow の責務を FakeBackend + PlaybackController で検証する。

ネイティブのファイルダイアログは開かず、`QFileDialog.getOpenFileName` を差し替える。
"""

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QTableView
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
)
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.ui import main_window as main_window_module
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView
from sdp.ui.speed_panel import SpeedPanel


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def playlist_model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def playlist_playback(
    controller: PlaybackController, playlist_model: PlaylistModel
) -> Iterator[PlaylistPlaybackController]:
    yield PlaylistPlaybackController(controller, playlist_model)


@pytest.fixture
def window(
    controller: PlaybackController,
    playlist_model: PlaylistModel,
    playlist_playback: PlaylistPlaybackController,
    qtbot: QtBot,
) -> Iterator[MainWindow]:
    main = MainWindow(controller, playlist_model, playlist_playback)
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


def test_main_window_takes_only_its_three_dependencies() -> None:
    """MainWindow が受け取るのは Controller・Model・プレイリスト再生制御（と親）だけ。"""
    parameters = list(inspect.signature(MainWindow.__init__).parameters)
    assert parameters == ["self", "controller", "playlist_model", "playlist_playback", "parent"]


def test_main_window_module_does_not_import_the_qt_backend() -> None:
    """MainWindow のモジュールが具体的な Backend も永続化も参照していない。"""
    for forbidden in (
        "QtMultimediaBackend",
        "QMediaPlayer",
        "save_playlist",
        "load_playlist",
        "PlaylistSession",
    ):
        assert not hasattr(main_window_module, forbidden), forbidden


def test_main_window_delegates_to_child_widgets(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """再生は PlayerControls、プレイリスト操作は PlaylistView へ委譲する。"""
    controls = window.findChild(PlayerControls)
    speed_panels = window.findChildren(SpeedPanel)
    playlist_views = window.findChildren(PlaylistView)
    assert controls is not None
    assert len(speed_panels) == 1
    assert len(playlist_views) == 1
    assert playlist_views[0].findChild(QTableView, "playlistTable") is not None

    for forbidden in (
        "play",
        "pause",
        "stop",
        "seek",
        "set_volume",
        "set_playback_rate",
        "set_pitch_compensation",
        "add_files",
        "remove_selected",
        "clear_playlist",
    ):
        assert not hasattr(window, forbidden), forbidden


def test_speed_panel_keeps_controller_state_across_source_changes(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """直接loadでsourceが変わっても速度・ピッチ表示を維持する。"""
    spin_box = window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    assert spin_box is not None
    controller.set_playback_rate(1.5)
    controller.set_pitch_compensation(False)
    backend.calls.clear()

    controller.load(audio_file)

    assert spin_box.value() == 1.5
    assert controller.pitch_compensation is False
    assert backend.call_names() == ["load"]


def test_speed_panel_state_survives_playlist_switch_and_repeat_one(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    playlist_model: PlaylistModel,
    playlist_playback: PlaylistPlaybackController,
    tmp_path: Path,
    qtbot: QtBot,
) -> None:
    """プレイリスト曲切替とRepeat ONEのreload後も速度・pitchを維持する。"""
    sources = [tmp_path / "曲A.wav", tmp_path / "曲B.wav"]
    for source in sources:
        source.write_bytes(b"x")
    entry_ids = playlist_model.add_paths(sources)
    spin_box = window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    assert spin_box is not None
    controller.set_playback_rate(1.25)
    controller.set_pitch_compensation(False)

    assert playlist_playback.play_entry(entry_ids[0]) is True
    assert playlist_playback.play_entry(entry_ids[1]) is True
    playlist_playback.set_repeat_mode(RepeatMode.ONE)
    backend.emit_position(100)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    qtbot.waitUntil(lambda: len(backend.call_args("load")) == 3)

    assert controller.playback_rate == 1.25
    assert controller.pitch_compensation is False
    assert spin_box.value() == 1.25
    assert backend.call_args("set_playback_rate") == [(1.25,)]
    assert backend.call_args("set_pitch_compensation") == [(False,)]
    assert backend.call_args("load") == [
        (sources[0].resolve(),),
        (sources[1].resolve(),),
        (sources[1].resolve(),),
    ]


def test_playlist_view_uses_the_given_model(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """配置された PlaylistView に同じ PlaylistModel が設定される。"""
    table = window.findChild(QTableView, "playlistTable")
    assert table is not None
    assert table.model() is playlist_model


def test_playlist_messages_reach_the_status_bar(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """PlaylistView のメッセージ要求がステータスバーへ表示される。"""
    del playlist_model
    view = window.findChild(PlaylistView)
    assert view is not None

    view.message_requested.emit("3曲を追加しました。")

    assert window.statusBar().currentMessage() == "3曲を追加しました。"


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

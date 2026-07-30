"""前回の現在曲（entry_id）の復元を検証する。

復元は「選ぶだけ」で、自動再生しない・位置を進めないことを固定する。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.services.ui_state import UiState, WindowState
from sdp.services.ui_state_session import PlaylistUiStateSource

WINDOW = WindowState(x=100, y=80, width=800, height=600, maximized=False)


class FakeWindow(QObject):
    """UiStateHolder契約だけを満たすテスト用Window。"""

    def __init__(self) -> None:
        super().__init__()
        self.state = UiState()
        self.restored: list[UiState] = []
        self.connected: list[object] = []

    def capture_ui_state(self) -> UiState:
        return self.state

    def restore_ui_state(self, state: UiState) -> None:
        self.restored.append(state)
        self.state = state

    def connect_ui_state_changed(self, slot: object) -> None:
        self.connected.append(slot)

    def disconnect_ui_state_changed(self, slot: object) -> None:
        self.connected.remove(slot)


@pytest.fixture
def backend() -> FakePlaybackBackend:
    return FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> PlaybackController:
    return PlaybackController(backend)


@pytest.fixture
def playlist(qtbot: QtBot) -> PlaylistModel:
    del qtbot
    return PlaylistModel()


@pytest.fixture
def playlist_playback(
    controller: PlaybackController, playlist: PlaylistModel
) -> PlaylistPlaybackController:
    return PlaylistPlaybackController(controller, playlist)


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for name in ("曲 A.wav", "曲 B.mp3", "曲 C.flac"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


@pytest.fixture
def source(
    playlist_playback: PlaylistPlaybackController,
) -> Iterator[tuple[FakeWindow, PlaylistUiStateSource]]:
    window = FakeWindow()
    yield window, PlaylistUiStateSource(window, playlist_playback)


# -- select_entry_by_id -----------------------------------------------------


def test_selecting_an_entry_loads_the_source_without_playing(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """entry_idで現在曲を選び直すが、再生は始めず位置も0のまま。"""
    entry_ids = playlist.add_paths(audio_files)

    assert playlist_playback.select_entry_by_id(entry_ids[1]) is True

    assert playlist_playback.current_entry_id == entry_ids[1]
    assert controller.source == audio_files[1].resolve()
    assert controller.state is not PlaybackState.PLAYING
    assert controller.position_ms == 0
    assert "play" not in backend.call_names()


def test_selecting_a_removed_entry_is_ignored(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    controller: PlaybackController,
    audio_files: list[Path],
) -> None:
    """削除済みentry_idでは何もしない（エラーにしない）。"""
    entry_ids = playlist.add_paths(audio_files)
    playlist.removeRows(1, 1)

    assert playlist_playback.select_entry_by_id(entry_ids[1]) is False

    assert playlist_playback.current_entry_id is None
    assert controller.source is None


def test_selecting_a_missing_file_is_ignored(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """欠損ファイルのentryも選ばない（別の曲へも移らない）。"""
    entry_ids = playlist.add_paths(audio_files)
    audio_files[0].unlink()

    assert playlist_playback.select_entry_by_id(entry_ids[0]) is False

    assert playlist_playback.current_entry_id is None


def test_selecting_distinguishes_duplicate_paths(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """同じパスを2行追加していても、entry_idで正しい行を復元する。"""
    entry_ids = playlist.add_paths([audio_files[0], audio_files[0]])

    assert playlist_playback.select_entry_by_id(entry_ids[1]) is True

    assert playlist_playback.current_entry_id == entry_ids[1]
    assert playlist_playback.current_entry_id != entry_ids[0]


def test_selecting_survives_reordering(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """並べ替え後も同じentryへ戻る（行番号を保存していない）。"""
    entry_ids = playlist.add_paths(audio_files)
    root = playlist.index(0, 0).parent()
    playlist.moveRows(root, 0, 1, root, 3)

    assert playlist_playback.select_entry_by_id(entry_ids[0]) is True

    assert playlist_playback.current_entry_id == entry_ids[0]
    assert playlist.row_of_entry_id(entry_ids[0]) == 2


def test_navigation_is_available_after_restoring(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """復元後は現在曲を基準にPrevious／Nextが使える。"""
    entry_ids = playlist.add_paths(audio_files)

    playlist_playback.select_entry_by_id(entry_ids[1])

    assert playlist_playback.can_play_previous is True
    assert playlist_playback.can_play_next is True


@pytest.mark.parametrize("mode", list(RepeatMode))
def test_restoring_works_with_every_repeat_mode(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    mode: RepeatMode,
) -> None:
    """Repeatの設定に関係なく現在曲を選べる（再生は始めない）。"""
    entry_ids = playlist.add_paths(audio_files)
    playlist_playback.set_repeat_mode(mode)

    assert playlist_playback.select_entry_by_id(entry_ids[2]) is True

    assert playlist_playback.current_entry_id == entry_ids[2]


def test_restoring_with_shuffle_seeds_the_history(
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """シャッフルONで復元した曲は、その回の履歴の起点になる。"""
    entry_ids = playlist.add_paths(audio_files)
    playlist_playback.set_shuffle_enabled(True)

    assert playlist_playback.select_entry_by_id(entry_ids[1]) is True

    assert playlist_playback.current_entry_id == entry_ids[1]
    # 履歴の先頭より前へは戻らない。
    assert playlist_playback.play_previous() is False


# -- PlaylistUiStateSource --------------------------------------------------


def test_capture_adds_the_current_entry_id(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """WindowのUI状態へ現在曲を合成する。"""
    window, ui_state_source = source
    window.state = UiState(window=WINDOW)
    entry_ids = playlist.add_paths(audio_files)
    playlist_playback.select_entry_by_id(entry_ids[0])

    captured = ui_state_source.capture_ui_state()

    assert captured.window == WINDOW
    assert captured.current_playlist_entry_id == entry_ids[0]


def test_capture_without_a_current_entry_is_none(
    source: tuple[FakeWindow, PlaylistUiStateSource],
) -> None:
    """現在曲が無ければNoneのまま保存対象にしない。"""
    _, ui_state_source = source

    assert ui_state_source.capture_ui_state().current_playlist_entry_id is None


def test_removing_the_current_entry_clears_the_saved_id(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在曲が削除されたら保存対象もNoneになる。"""
    _, ui_state_source = source
    entry_ids = playlist.add_paths(audio_files)
    playlist_playback.select_entry_by_id(entry_ids[0])

    playlist.removeRows(0, 1)

    assert ui_state_source.capture_ui_state().current_playlist_entry_id is None


def test_clearing_the_playlist_clears_the_saved_id(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """全消去でも保存対象がNoneになる。"""
    _, ui_state_source = source
    entry_ids = playlist.add_paths(audio_files)
    playlist_playback.select_entry_by_id(entry_ids[0])

    playlist.clear()

    assert ui_state_source.capture_ui_state().current_playlist_entry_id is None


def test_restore_selects_the_saved_entry_without_playing(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """復元でWindow状態を適用し、現在曲も選ぶ（再生はしない）。"""
    window, ui_state_source = source
    entry_ids = playlist.add_paths(audio_files)

    ui_state_source.restore_ui_state(UiState(window=WINDOW, current_playlist_entry_id=entry_ids[2]))

    assert window.restored[0].window == WINDOW
    assert playlist_playback.current_entry_id == entry_ids[2]
    assert controller.state is not PlaybackState.PLAYING
    assert controller.position_ms == 0
    assert "play" not in backend.call_names()


def test_restore_ignores_an_unknown_entry_id(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """存在しないentry_idでも復元全体を失敗にしない。"""
    window, ui_state_source = source
    playlist.add_paths(audio_files)

    ui_state_source.restore_ui_state(
        UiState(window=WINDOW, current_playlist_entry_id="削除済みのID")
    )

    assert window.restored[0].window == WINDOW
    assert playlist_playback.current_entry_id is None
    assert playlist.rowCount() == len(audio_files)


def test_current_entry_change_notifies_the_session(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在曲の変更もUI状態の保存契機として通知する。"""
    _, ui_state_source = source
    entry_ids = playlist.add_paths(audio_files)
    notifications: list[int] = []
    ui_state_source.connect_ui_state_changed(lambda: notifications.append(1))

    playlist_playback.select_entry_by_id(entry_ids[0])

    assert notifications


def test_disconnect_stops_notifications(
    source: tuple[FakeWindow, PlaylistUiStateSource],
    playlist_playback: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """購読解除後は通知しない。"""
    _, ui_state_source = source
    entry_ids = playlist.add_paths(audio_files)
    notifications: list[int] = []

    def slot() -> None:
        notifications.append(1)

    ui_state_source.connect_ui_state_changed(slot)
    ui_state_source.disconnect_ui_state_changed(slot)
    playlist_playback.select_entry_by_id(entry_ids[0])

    assert notifications == []

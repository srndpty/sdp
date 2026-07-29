"""プレイリストからの逐次再生の実音テスト（`audio` マーカー）。

実際の音声出力デバイスを使うため CI からは除外する。
再生前に必ず音量を 0.0 にし、待機はすべて明示的なタイムアウト付きで行う。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController

LOAD_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 5_000
TRACK_TIMEOUT_MS = 20_000

pytestmark = pytest.mark.audio


@pytest.fixture
def playback(qtbot: QtBot) -> Iterator[PlaybackController]:
    """音量 0.0 に設定済みの再生制御（可聴音を出さないため）。"""
    del qtbot
    controller = PlaybackController(QtMultimediaBackend())
    controller.set_volume(0.0)
    assert controller.volume == 0.0
    yield controller


@pytest.fixture
def playlist(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def controller(
    playback: PlaybackController, playlist: PlaylistModel
) -> Iterator[PlaylistPlaybackController]:
    yield PlaylistPlaybackController(playback, playlist)


def test_playlist_advances_to_the_next_track(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    test_audio_dir: Path,
    qtbot: QtBot,
) -> None:
    """1 曲目の終了で 2 曲目へ自動で進み、末尾では先頭へ戻らない。"""
    sources = [test_audio_dir / "sine440.wav", test_audio_dir / "sine440.mp3"]
    for source in sources:
        assert source.is_file(), source
    entry_ids = playlist.add_paths(sources)
    error_spy = QSignalSpy(playback.error_occurred)

    assert controller.play_entry(entry_ids[0]) is True
    assert controller.current_entry_id == entry_ids[0]
    qtbot.waitUntil(lambda: playback.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    qtbot.waitUntil(lambda: playback.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)

    # 1 曲目が終わると 2 曲目へ進む。
    qtbot.waitUntil(lambda: controller.current_entry_id == entry_ids[1], timeout=TRACK_TIMEOUT_MS)
    assert playback.source == sources[1].resolve()
    qtbot.waitUntil(lambda: playback.position_ms > 0, timeout=ACTION_TIMEOUT_MS)

    # 最後の曲が終わっても先頭へ戻らない。
    qtbot.waitUntil(lambda: playback.state is not PlaybackState.PLAYING, timeout=TRACK_TIMEOUT_MS)
    assert controller.current_entry_id == entry_ids[1]
    assert playback.source == sources[1].resolve()
    assert error_spy.count() == 0


def test_missing_entry_is_skipped_during_auto_advance(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    test_audio_dir: Path,
    tmp_path: Path,
    qtbot: QtBot,
) -> None:
    """自動次曲では途中の欠損エントリを飛ばす。"""
    first = test_audio_dir / "sine440.wav"
    third = test_audio_dir / "sine440.flac"
    entry_ids = playlist.add_paths([first, tmp_path / "ない曲.wav", third])
    error_spy = QSignalSpy(playback.error_occurred)

    assert controller.play_entry(entry_ids[0]) is True

    qtbot.waitUntil(lambda: controller.current_entry_id == entry_ids[2], timeout=TRACK_TIMEOUT_MS)
    assert playback.source == third.resolve()
    assert playlist.entry_at(1).is_missing
    assert error_spy.count() == 0

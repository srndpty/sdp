"""実Qt Multimediaで再生中の速度・ピッチ変更を検証するaudioテスト。

音質そのものは自動判定せず、source・state・position・property・errorを確認する。
可聴音を出さないよう、すべてのテストで再生前に音量を0.0へ設定する。
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
from sdp.core.playlist.types import RepeatMode

LOAD_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 5_000
TRACK_TIMEOUT_MS = 20_000

pytestmark = pytest.mark.audio


@pytest.fixture
def playback(qtbot: QtBot) -> Iterator[tuple[PlaybackController, QtMultimediaBackend]]:
    """無音量に設定した実BackendとController。"""
    del qtbot
    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)
    controller.set_volume(0.0)
    assert backend.volume == 0.0
    yield controller, backend
    controller.stop()


def wait_for_position_after(qtbot: QtBot, controller: PlaybackController, position_ms: int) -> None:
    qtbot.waitUntil(lambda: controller.position_ms > position_ms, timeout=ACTION_TIMEOUT_MS)


def test_rate_changes_during_playback_without_reloading(
    playback: tuple[PlaybackController, QtMultimediaBackend],
    test_audio_dir: Path,
    qtbot: QtBot,
) -> None:
    """再生中の1.50→0.75→1.00倍変更で再生と位置前進が継続する。"""
    controller, backend = playback
    source = test_audio_dir / "sweep.wav"
    errors = QSignalSpy(controller.error_occurred)
    sources = QSignalSpy(controller.source_changed)

    controller.load(source)
    qtbot.waitUntil(lambda: controller.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    controller.play()
    qtbot.waitUntil(lambda: controller.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    wait_for_position_after(qtbot, controller, 0)

    for rate in (1.50, 0.75, 1.00):
        before = controller.position_ms
        controller.set_playback_rate(rate)
        assert controller.playback_rate == rate
        assert backend.playback_rate == pytest.approx(rate, rel=1e-6)
        assert controller.source == source.resolve()
        assert controller.state is PlaybackState.PLAYING
        wait_for_position_after(qtbot, controller, before)

    assert sources.count() == 1
    assert errors.count() == 0


def test_pitch_mode_changes_during_playback_without_reloading(
    playback: tuple[PlaybackController, QtMultimediaBackend],
    test_audio_dir: Path,
    qtbot: QtBot,
) -> None:
    """再生中の補正ON/OFF切替でsourceと再生状態を維持する。"""
    controller, backend = playback
    source = test_audio_dir / "sweep.wav"
    errors = QSignalSpy(controller.error_occurred)
    sources = QSignalSpy(controller.source_changed)
    controller.load(source)
    qtbot.waitUntil(lambda: controller.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    controller.play()
    qtbot.waitUntil(lambda: controller.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    wait_for_position_after(qtbot, controller, 0)

    for enabled in (False, True, False, True):
        before = controller.position_ms
        controller.set_pitch_compensation(enabled)
        assert controller.pitch_compensation is enabled
        assert backend.pitch_compensation is enabled
        assert controller.source == source.resolve()
        assert controller.state is PlaybackState.PLAYING
        wait_for_position_after(qtbot, controller, before)

    assert sources.count() == 1
    assert errors.count() == 0


def test_speed_and_pitch_survive_track_changes_and_repeat_one(
    playback: tuple[PlaybackController, QtMultimediaBackend],
    test_audio_dir: Path,
    qtbot: QtBot,
) -> None:
    """次曲・Repeat ONE・直接load後もセッション内設定を維持する。"""
    controller, backend = playback
    playlist = PlaylistModel()
    playlist_playback = PlaylistPlaybackController(controller, playlist)
    sources = [test_audio_dir / "sine440.wav", test_audio_dir / "sweep.wav"]
    entry_ids = playlist.add_paths(sources)
    errors = QSignalSpy(controller.error_occurred)
    source_changes = QSignalSpy(controller.source_changed)
    controller.set_playback_rate(1.25)
    controller.set_pitch_compensation(False)

    assert playlist_playback.play_entry(entry_ids[0]) is True
    qtbot.waitUntil(lambda: controller.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    assert playlist_playback.play_next() is True
    qtbot.waitUntil(lambda: controller.source == sources[1].resolve(), timeout=LOAD_TIMEOUT_MS)
    playlist_playback.set_repeat_mode(RepeatMode.ONE)
    controller.play()
    qtbot.waitUntil(lambda: controller.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    qtbot.waitUntil(lambda: source_changes.count() >= 3, timeout=TRACK_TIMEOUT_MS)

    controller.load(sources[0])
    qtbot.waitUntil(lambda: controller.source == sources[0].resolve(), timeout=LOAD_TIMEOUT_MS)

    assert controller.playback_rate == 1.25
    assert backend.playback_rate == pytest.approx(1.25, rel=1e-6)
    assert controller.pitch_compensation is False
    assert backend.pitch_compensation is False
    assert errors.count() == 0

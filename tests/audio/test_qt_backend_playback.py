"""QtMultimediaBackend の実音再生テスト（`audio` マーカー。ローカル手動実行のみ）。

実際の音声出力デバイスを使うため CI からは除外する
（[docs/testing-strategy.md](../../docs/testing-strategy.md) §4）。
勝手に音を出さないよう、再生前に必ず音量を 0.0 にする。
待機はすべて明示的なタイムアウト付きで行い、失敗時に無期限で待たない。

形式ごとの対応可否は P0-A で 6 形式すべて検証済みのため、ここでは再検証しない。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playback.types import MediaStatus, PlaybackState

LOAD_TIMEOUT_MS = 10_000
ACTION_TIMEOUT_MS = 5_000
END_OF_MEDIA_TIMEOUT_MS = 15_000

pytestmark = pytest.mark.audio


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[QtMultimediaBackend]:
    """音量 0.0 に設定済みの Backend（可聴音を出さないため）。"""
    del qtbot
    instance = QtMultimediaBackend()
    instance.set_volume(0.0)
    assert instance.volume == 0.0
    yield instance


def test_wav_playback_lifecycle(
    backend: QtMultimediaBackend, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """WAV の読み込みから再生・一時停止・シーク・停止・終了通知までを確認する。"""
    source = test_audio_dir / "sine440.wav"
    assert source.is_file(), source
    error_spy = QSignalSpy(backend.error_occurred)
    statuses: list[MediaStatus] = []
    backend.media_status_changed.connect(statuses.append)

    backend.load(source)
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    assert backend.duration_ms == pytest.approx(2000, abs=200)

    backend.play()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    qtbot.waitUntil(lambda: backend.position_ms > 0, timeout=ACTION_TIMEOUT_MS)

    backend.pause()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.PAUSED, timeout=ACTION_TIMEOUT_MS)

    backend.seek(1000)
    qtbot.waitUntil(lambda: backend.position_ms >= 900, timeout=ACTION_TIMEOUT_MS)
    seeked_position = backend.position_ms

    backend.play()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    qtbot.waitUntil(lambda: backend.position_ms > seeked_position, timeout=ACTION_TIMEOUT_MS)

    backend.stop()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.STOPPED, timeout=ACTION_TIMEOUT_MS)

    statuses.clear()
    backend.play()
    qtbot.waitUntil(lambda: MediaStatus.END_OF_MEDIA in statuses, timeout=END_OF_MEDIA_TIMEOUT_MS)
    qtbot.waitUntil(lambda: backend.state is PlaybackState.STOPPED, timeout=ACTION_TIMEOUT_MS)

    assert error_spy.count() == 0


def test_compressed_format_playback(
    backend: QtMultimediaBackend, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """代表的な圧縮形式（MP3）でも読み込みと再生ができる。"""
    source = test_audio_dir / "sine440.mp3"
    assert source.is_file(), source
    error_spy = QSignalSpy(backend.error_occurred)

    backend.load(source)
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)

    backend.play()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    qtbot.waitUntil(lambda: backend.position_ms > 0, timeout=ACTION_TIMEOUT_MS)

    backend.stop()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.STOPPED, timeout=ACTION_TIMEOUT_MS)
    assert error_spy.count() == 0


@pytest.mark.parametrize("pause_before_replace", [False, True])
def test_replace_source_while_playing_or_paused(
    backend: QtMultimediaBackend,
    test_audio_dir: Path,
    qtbot: QtBot,
    pause_before_replace: bool,
) -> None:
    """実再生中・一時停止中にsourceを差し替えると、先頭で停止する。"""
    source_a = test_audio_dir / "sine440.wav"
    source_b = test_audio_dir / "sweep.wav"
    backend.load(source_a)
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    backend.play()
    qtbot.waitUntil(lambda: backend.state is PlaybackState.PLAYING, timeout=ACTION_TIMEOUT_MS)
    qtbot.waitUntil(lambda: backend.position_ms > 0, timeout=ACTION_TIMEOUT_MS)
    if pause_before_replace:
        backend.pause()
        qtbot.waitUntil(lambda: backend.state is PlaybackState.PAUSED, timeout=ACTION_TIMEOUT_MS)

    state_spy = QSignalSpy(backend.state_changed)
    position_spy = QSignalSpy(backend.position_changed)
    error_spy = QSignalSpy(backend.error_occurred)

    backend.load(source_b)

    assert backend.state is PlaybackState.STOPPED
    assert state_spy.count() == 1
    assert state_spy.at(0)[0] is PlaybackState.STOPPED
    assert backend.position_ms == 0
    assert position_spy.count() >= 1
    assert position_spy.at(position_spy.count() - 1)[0] == 0
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    players = backend.findChildren(QMediaPlayer)
    assert len(players) == 1
    assert Path(players[0].source().toLocalFile()) == source_b
    assert error_spy.count() == 0

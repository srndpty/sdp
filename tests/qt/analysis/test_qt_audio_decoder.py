"""実QAudioDecoderによるWAV・圧縮音源・失敗・thread終了を検証する。"""

import threading
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.waveform import WaveformData
from sdp.core.playback.controller import PlaybackController
from sdp.services.waveform_analysis import WaveformAnalysisService

ASSETS = Path(__file__).parents[3] / "assets" / "test_audio"


@pytest.mark.parametrize("filename", ["sine440.wav", "sine440.mp3"])
def test_real_decoder_produces_finite_waveform_on_worker_thread(
    filename: str, tmp_path: Path, qtbot: QtBot
) -> None:
    """代表WAV／MP3を専用threadでデコードし、約2秒の波形を返す。"""
    controller = PlaybackController(FakePlaybackBackend())
    service = WaveformAnalysisService(controller, tmp_path / "cache")
    finished = QSignalSpy(service.analysis_finished)
    partial = QSignalSpy(service.partial_ready)
    decoder_created = QSignalSpy(
        service._worker.decoder_created  # pyright: ignore[reportPrivateUsage]
    )
    service.start()
    controller.load(ASSETS / filename)

    qtbot.waitUntil(lambda: finished.count() == 1, timeout=10_000)
    data = finished.at(0)[2]
    assert isinstance(data, WaveformData)
    assert 1_900 <= data.duration_ms <= 2_100
    assert data.minimum.size > 0
    assert partial.count() >= 1
    assert np.all(np.isfinite(data.minimum))
    assert np.all(data.minimum <= data.maximum)
    assert decoder_created.count() == 1
    assert decoder_created.at(0)[0] is True
    service.shutdown()
    assert not service.is_running


def test_invalid_file_emits_analysis_failure(tmp_path: Path, qtbot: QtBot) -> None:
    """存在する不正ファイルは再生エラーと分離した解析失敗になる。"""
    invalid = tmp_path / "invalid.mp3"
    invalid.write_bytes(b"not audio")
    controller = PlaybackController(FakePlaybackBackend())
    service = WaveformAnalysisService(controller, tmp_path / "cache")
    failed = QSignalSpy(service.analysis_failed)
    service.start()
    controller.load(invalid)
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=10_000)
    assert failed.at(0)[0] == invalid.resolve()
    service.shutdown()


def test_real_decoder_cancel_does_not_publish_old_source(tmp_path: Path, qtbot: QtBot) -> None:
    """実decoderへstopを要求し、直後に切り替えた旧sourceの完了を公開しない。"""
    controller = PlaybackController(FakePlaybackBackend())
    service = WaveformAnalysisService(controller, tmp_path / "cache")
    finished = QSignalSpy(service.analysis_finished)
    decoder_created = threading.Event()

    def record_decoder_created(on_worker_thread: bool) -> None:
        if on_worker_thread:
            decoder_created.set()

    service._worker.decoder_created.connect(  # pyright: ignore[reportPrivateUsage]
        record_decoder_created,
        Qt.ConnectionType.DirectConnection,
    )
    first = (ASSETS / "sine440.wav").resolve()
    second = (ASSETS / "sine440.mp3").resolve()
    service.start()

    controller.load(first)
    assert decoder_created.wait(timeout=5)
    controller.load(second)

    qtbot.waitUntil(lambda: finished.count() == 1, timeout=10_000)
    assert finished.at(0)[0] == second
    service.shutdown()


def test_shutdown_stops_active_real_decoder(tmp_path: Path, qtbot: QtBot) -> None:
    """解析開始直後のshutdownでも専用threadを終了する。"""
    controller = PlaybackController(FakePlaybackBackend())
    service = WaveformAnalysisService(controller, tmp_path / "cache")
    service.start()
    controller.load(ASSETS / "sine440.wav")
    service.shutdown(timeout_ms=5_000)
    qtbot.waitUntil(lambda: not service.is_running)
    assert not service._thread.isRunning()  # pyright: ignore[reportPrivateUsage]

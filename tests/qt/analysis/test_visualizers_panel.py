"""VisualizersPanelによる追加可視化のタイマー・表示制御を検証する。"""

import struct
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.oscilloscope import OscilloscopeFrame
from sdp.core.playback.controller import PlaybackController
from sdp.services.pcm_tap import PcmTap
from sdp.ui.visualizers_panel import FAILED_MESSAGE, STOPPED_MESSAGE, VisualizersPanel


@pytest.fixture
def backend() -> FakePlaybackBackend:
    return FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> PlaybackController:
    return PlaybackController(backend)


@pytest.fixture
def tap(controller: PlaybackController) -> PcmTap:
    return PcmTap(controller)


@pytest.fixture
def window(qtbot: QtBot) -> QMainWindow:
    instance = QMainWindow()
    qtbot.addWidget(instance)
    return instance


@pytest.fixture
def panel(
    controller: PlaybackController,
    tap: PcmTap,
    window: QMainWindow,
    qtbot: QtBot,
) -> VisualizersPanel:
    instance = VisualizersPanel(controller, tap)
    window.setCentralWidget(instance)
    window.resize(600, 520)
    window.show()
    qtbot.waitExposed(window)
    return instance


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "song.wav"
    path.write_bytes(b"dummy")
    return path.resolve()


def pcm_buffer(frames: int = 4_096, sample_rate: int = 48_000) -> QAudioBuffer:
    """1kHz相当のInt16 stereo bufferを作る。"""
    t = np.arange(frames, dtype=np.float64) / sample_rate
    left = (np.sin(2.0 * np.pi * 1_000.0 * t) * 16_384).astype(np.int16)
    right = (np.sin(2.0 * np.pi * 1_100.0 * t) * 16_384).astype(np.int16)
    interleaved = np.empty(frames * 2, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right
    audio_format = QAudioFormat()
    audio_format.setSampleRate(sample_rate)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    data = struct.pack(f"<{interleaved.size}h", *interleaved.tolist())
    return QAudioBuffer(data, audio_format)


def feed(tap: PcmTap) -> None:
    tap.handle_audio_buffer(pcm_buffer())


def start_playing(controller: PlaybackController, tap: PcmTap, source: Path) -> None:
    controller.load(source)
    controller.play()
    feed(tap)


def test_panel_owns_all_additional_widgets(panel: VisualizersPanel) -> None:
    """追加Widget群を1つのPanelが所有する。"""
    assert panel.objectName() == "visualizersPanel"
    assert panel.oscilloscope_widget.parent() is panel
    assert panel.vectorscope_widget.parent() is panel
    assert panel.correlation_meter_widget.parent() is panel
    assert panel.spectrogram_widget.parent() is panel
    assert panel.chromagram_widget.parent() is panel


def test_timer_starts_only_while_playing(
    panel: VisualizersPanel, controller: PlaybackController, tap: PcmTap, source: Path
) -> None:
    """PLAYINGでタイマーが動き、STOPPEDで止まる。"""
    assert not panel.is_timer_active

    start_playing(controller, tap, source)
    assert panel.is_timer_active

    controller.stop()
    assert not panel.is_timer_active
    assert panel.oscilloscope_widget.status_text == STOPPED_MESSAGE


def test_all_visualizers_update_from_one_snapshot(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """1つのPcmTap snapshotから全追加可視化を更新する。"""
    start_playing(controller, tap, source)

    qtbot.waitUntil(lambda: panel.chromagram_count >= 1, timeout=3_000)

    assert panel.snapshot_count >= 1
    assert panel.oscilloscope_count >= 1
    assert panel.vectorscope_count >= 1
    assert panel.correlation_count >= 1
    assert panel.spectrogram_count >= 1
    assert panel.chromagram_count >= 1
    assert panel.oscilloscope_widget.frame is not None
    assert panel.vectorscope_widget.frame is not None
    assert panel.correlation_meter_widget.correlation is not None
    assert panel.spectrogram_widget.frame is not None
    assert panel.chromagram_widget.frame is not None


def test_hiding_one_visualizer_stops_only_that_analysis(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """個別非表示では対象だけ止まり、他の可視化は続く。"""
    start_playing(controller, tap, source)
    qtbot.waitUntil(lambda: panel.oscilloscope_count >= 1, timeout=3_000)

    panel.set_oscilloscope_visible(False)
    count = panel.oscilloscope_count
    chroma = panel.chromagram_count
    qtbot.waitUntil(lambda: panel.chromagram_count >= chroma + 2, timeout=3_000)

    assert panel.oscilloscope_count == count
    assert panel.oscilloscope_widget.frame is None
    assert panel.is_timer_active


def test_hiding_all_visualizers_stops_timer_but_not_pcm_tap(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
) -> None:
    """全追加可視化非表示でタイマーを止めるが、PCMタップは受信を続ける。"""
    start_playing(controller, tap, source)
    received = tap.received_buffer_count

    panel.set_oscilloscope_visible(False)
    panel.set_vectorscope_visible(False)
    panel.set_correlation_meter_visible(False)
    panel.set_spectrogram_visible(False)
    panel.set_chromagram_visible(False)
    feed(tap)

    assert not panel.is_timer_active
    assert not panel.isVisible()
    assert tap.received_buffer_count == received + 1


def test_minimized_window_stops_timer(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    window: QMainWindow,
    qtbot: QtBot,
) -> None:
    """最小化中は追加解析も止める。"""
    start_playing(controller, tap, source)
    assert panel.is_timer_active

    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.waitUntil(lambda: not panel.is_timer_active, timeout=3_000)

    window.setWindowState(Qt.WindowState.WindowNoState)
    qtbot.waitUntil(lambda: panel.is_timer_active, timeout=3_000)


def test_one_visualizer_failure_keeps_others_running(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """オシロスコープだけが失敗しても他の可視化は続く。"""

    def explode(*args: object, **kwargs: object) -> OscilloscopeFrame:
        raise RuntimeError("想定外のオシロスコープ失敗")

    monkeypatch.setattr("sdp.ui.visualizers_panel.compute_oscilloscope", explode)
    start_playing(controller, tap, source)

    qtbot.waitUntil(lambda: panel.oscilloscope_widget.status_text == FAILED_MESSAGE, timeout=3_000)
    qtbot.waitUntil(lambda: panel.chromagram_count >= 2, timeout=3_000)

    assert panel.oscilloscope_count == 0
    assert panel.chromagram_widget.frame is not None
    assert panel.is_timer_active


def test_shutdown_stops_timer(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """shutdown後は再生状態変化でも再開しない。"""
    start_playing(controller, tap, source)
    qtbot.waitUntil(lambda: panel.oscilloscope_count >= 1, timeout=3_000)

    panel.shutdown()
    controller.pause()
    controller.play()

    assert not panel.is_timer_active

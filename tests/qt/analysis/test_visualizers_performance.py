"""追加ビジュアライザー5種の処理量とGUI応答性を、厳格な時間上限なしで確認する。

CIへ数百µs単位の上限は設定しない（[testing-strategy.md](../../../docs/testing-strategy.md) §8）。
ここで担保するのは「1tickのrFFTはスペクトログラムとクロマグラムで共有して最大1回」
「履歴のコピーは1tickで1回」「幅・高さを変えても描画セル数と点数が有界」
「5種すべてONの連続更新でもGUI heartbeatが進む」といった構造的な性質。
実測値はローカルの計測スクリプトと最終報告へ記録する。
"""

import struct
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.spectrogram import (
    SPECTROGRAM_HISTORY,
    SpectrogramProcessor,
    spectrogram_cells,
)
from sdp.core.analysis.spectrum import SPECTRUM_BAND_COUNT, SPECTRUM_TIMER_INTERVAL_MS
from sdp.core.analysis.stereo import VECTORSCOPE_MAX_POINTS
from sdp.core.playback.controller import PlaybackController
from sdp.services.pcm_tap import PcmTap
from sdp.ui.visualizers_panel import VisualizersPanel

SAMPLE_RATE = 48_000
FRAMES_PER_BUFFER = 4_096


@pytest.fixture
def controller() -> PlaybackController:
    return PlaybackController(FakePlaybackBackend())


@pytest.fixture
def tap(controller: PlaybackController) -> PcmTap:
    return PcmTap(controller)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "計測.wav"
    path.write_bytes(b"A")
    return path.resolve()


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
    window.resize(800, 620)
    window.show()
    qtbot.waitExposed(window)
    return instance


def pcm_buffer(frames: int = FRAMES_PER_BUFFER) -> QAudioBuffer:
    """L／Rで周波数の違う4096frameのInt16 stereo buffer。"""
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    left = (np.sin(2.0 * np.pi * 1_000.0 * t) * 16_384).astype(np.int16)
    right = (np.sin(2.0 * np.pi * 1_100.0 * t) * 16_384).astype(np.int16)
    interleaved = np.empty(frames * 2, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right
    audio_format = QAudioFormat()
    audio_format.setSampleRate(SAMPLE_RATE)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return QAudioBuffer(struct.pack(f"<{interleaved.size}h", *interleaved.tolist()), audio_format)


def start_playing(controller: PlaybackController, tap: PcmTap, source: Path) -> None:
    controller.load(source)
    controller.play()
    tap.handle_audio_buffer(pcm_buffer())


def tone(frames: int = FRAMES_PER_BUFFER) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.5 * np.sin(2.0 * np.pi * 1_000.0 * t)).astype(np.float32)


# -- 1tickあたりのFFT回数 --------------------------------------------------


def test_one_timer_tick_runs_at_most_one_fft(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """5種すべてONでも、tickごとのrFFTは1回を超えない（共有している）。"""
    start_playing(controller, tap, source)
    qtbot.waitUntil(lambda: panel.chromagram_count >= 1, timeout=3_000)

    calls: list[int] = []
    original = np.fft.rfft

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return cast("object", original(*args, **kwargs))  # pyright: ignore[reportCallIssue, reportArgumentType]

    monkeypatch.setattr(np.fft, "rfft", counting)
    ticks_before = panel.snapshot_count
    qtbot.waitUntil(lambda: panel.snapshot_count >= ticks_before + 10, timeout=5_000)
    ticks = panel.snapshot_count - ticks_before

    assert len(calls) <= ticks
    panel.shutdown()


# -- 描画量の上限 -----------------------------------------------------------


def test_spectrogram_cell_count_stays_bounded_across_widths(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    window: QMainWindow,
    qtbot: QtBot,
) -> None:
    """幅・高さを変えてもセル数は履歴×band数を超えない。"""
    start_playing(controller, tap, source)
    qtbot.waitUntil(lambda: panel.spectrogram_widget.frame is not None, timeout=3_000)
    limit = SPECTROGRAM_HISTORY * SPECTRUM_BAND_COUNT

    for width in (320, 800, 1_920, 3_840):
        window.resize(width, 620)
        panel.spectrogram_widget.repaint()
        assert panel.spectrogram_widget.last_cell_count <= limit

    panel.shutdown()


def test_vectorscope_point_count_stays_bounded_across_widths(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    window: QMainWindow,
    qtbot: QtBot,
) -> None:
    """幅を変えても描画する点数は上限を超えない。"""
    start_playing(controller, tap, source)
    qtbot.waitUntil(lambda: panel.vectorscope_widget.frame is not None, timeout=3_000)

    for width in (320, 800, 1_920, 3_840):
        window.resize(width, 620)
        panel.vectorscope_widget.repaint()
        assert panel.vectorscope_widget.last_point_count <= VECTORSCOPE_MAX_POINTS

    panel.shutdown()


# -- 履歴と間引きのコスト ---------------------------------------------------


def test_spectrogram_history_is_copied_once_per_tick() -> None:
    """1列の追記でリング全体をシフトせず、フレームは1回のコピーで作る。"""
    processor = SpectrogramProcessor()
    copies: list[int] = []
    original = np.concatenate

    def counting(*args: object, **kwargs: object) -> object:
        copies.append(1)
        return cast("object", original(*args, **kwargs))  # pyright: ignore[reportCallIssue, reportArgumentType]

    samples = tone()
    processor.process(samples, SAMPLE_RATE)
    np.concatenate = counting  # pyright: ignore[reportAttributeAccessIssue]
    try:
        for _ in range(10):
            processor.process(samples, SAMPLE_RATE)
    finally:
        np.concatenate = original  # pyright: ignore[reportAttributeAccessIssue]

    assert len(copies) == 10


def test_spectrogram_column_and_cell_cost_is_recorded() -> None:
    """1列追加＋1,920×620相当への間引きの時間を記録する（上限は設けない）。"""
    processor = SpectrogramProcessor()
    samples = tone()
    frame = processor.process(samples, SAMPLE_RATE)
    cells = spectrogram_cells(frame, column_count=1_920, row_count=620)
    timer = QElapsedTimer()
    durations: list[int] = []

    for _ in range(200):
        timer.start()
        frame = processor.process(samples, SAMPLE_RATE)
        cells = spectrogram_cells(frame, column_count=1_920, row_count=620)
        durations.append(timer.nsecsElapsed())

    average_ms = sum(durations) / len(durations) / 1e6
    print(
        f"スペクトログラム1tick（列追加＋間引き）平均 {average_ms:.4f}ms"
        f" / 最大 {max(durations) / 1e6:.4f}ms"
    )
    assert cells.columns == SPECTROGRAM_HISTORY
    assert cells.rows == SPECTRUM_BAND_COUNT


# -- GUI応答性 --------------------------------------------------------------


def test_gui_heartbeat_continues_with_all_visualizers_on(
    panel: VisualizersPanel,
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """5種すべてONの30FPS更新中も、別のQTimerが通常のevent loopで進む。"""
    heartbeats: list[int] = []
    heartbeat = QTimer()
    heartbeat.setInterval(SPECTRUM_TIMER_INTERVAL_MS)
    heartbeat.timeout.connect(lambda: heartbeats.append(1))
    heartbeat.start()

    start_playing(controller, tap, source)

    try:
        qtbot.waitUntil(lambda: panel.chromagram_count >= 15, timeout=10_000)
        qtbot.waitUntil(lambda: len(heartbeats) >= 10, timeout=10_000)
    finally:
        heartbeat.stop()
        panel.shutdown()

    assert panel.chromagram_count >= 15
    assert len(heartbeats) >= 10

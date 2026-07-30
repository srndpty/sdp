"""スペクトラムとレベルメーターの処理量とGUI応答性を、厳格な時間上限なしで確認する。

CIへ数百µs単位の上限は設定しない（[testing-strategy.md](../../../docs/testing-strategy.md) §8）。
ここで担保するのは「音声コールバック内でFFTやPeak／RMSをしない」
「1tickでFFTとLevelは各最大1回」「連続tickでもGUI heartbeatが進む」
「3本のリングバッファ容量とbar数が固定」といった構造的な性質。
実測値はローカルの計測スクリプトと最終報告へ記録する。
"""

import struct
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.level import LEVEL_WINDOW_SIZE, LevelProcessor
from sdp.core.analysis.ring_buffer import PcmRingBuffer
from sdp.core.analysis.spectrum import (
    FFT_SIZE,
    SPECTRUM_BAND_COUNT,
    SPECTRUM_TIMER_INTERVAL_MS,
    SpectrumProcessor,
    compute_spectrum,
)
from sdp.core.playback.controller import PlaybackController
from sdp.services import pcm_tap as pcm_tap_module
from sdp.services.pcm_tap import PcmTap
from sdp.ui.spectrum_panel import SpectrumPanel

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


def pcm_buffer(frames: int = FRAMES_PER_BUFFER) -> QAudioBuffer:
    """P0-C実測と同じ4096frameのInt16 stereo buffer。"""
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    wave = (np.sin(2.0 * np.pi * 1_000.0 * t) * 16_384).astype(np.int16)
    interleaved = np.repeat(wave, 2)
    audio_format = QAudioFormat()
    audio_format.setSampleRate(SAMPLE_RATE)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return QAudioBuffer(struct.pack(f"<{interleaved.size}h", *interleaved.tolist()), audio_format)


# -- コールバックの軽さ -----------------------------------------------------


def test_audio_callback_does_not_run_an_fft(tap: PcmTap, monkeypatch: pytest.MonkeyPatch) -> None:
    """音声コールバックからFFTを呼ばない（GUIタイマー側の責務）。"""
    calls: list[int] = []

    def record(*args: object, **kwargs: object) -> object:
        calls.append(1)
        raise AssertionError("音声コールバック内でFFTしてはならない")

    monkeypatch.setattr(np.fft, "rfft", record)
    buffer = pcm_buffer()

    for _ in range(20):
        tap.handle_audio_buffer(buffer)

    assert calls == []
    assert tap.received_buffer_count == 20


def test_audio_callback_only_normalizes_and_appends(tap: PcmTap) -> None:
    """コールバックの成果はリングバッファへの追記だけで、容量は固定のまま。"""
    buffer = pcm_buffer()
    capacity = tap.ring_buffer.capacity

    for _ in range(200):
        tap.handle_audio_buffer(buffer)

    assert tap.ring_buffer.capacity == capacity
    assert tap.available_frame_count == capacity
    assert tap.discarded_buffer_count == 0


def test_callback_cost_is_recorded_without_a_strict_limit(tap: PcmTap) -> None:
    """コールバック1回の平均・最大時間を記録する（上限は設けない）。"""
    buffer = pcm_buffer()
    timer = QElapsedTimer()
    durations: list[int] = []

    for _ in range(200):
        timer.start()
        tap.handle_audio_buffer(buffer)
        durations.append(timer.nsecsElapsed())

    average_ms = sum(durations) / len(durations) / 1e6
    print(f"PCMコールバック 平均 {average_ms:.4f}ms / 最大 {max(durations) / 1e6:.4f}ms")
    assert tap.received_buffer_count == 200


def test_fft_and_band_aggregation_cost_is_recorded() -> None:
    """4096点FFT＋96band集約の時間を記録する。"""
    samples = np.sin(
        2.0 * np.pi * 1_000.0 * np.arange(FFT_SIZE, dtype=np.float64) / SAMPLE_RATE
    ).astype(np.float32)
    timer = QElapsedTimer()
    durations: list[int] = []
    frame = compute_spectrum(samples, SAMPLE_RATE)

    for _ in range(200):
        timer.start()
        frame = compute_spectrum(samples, SAMPLE_RATE)
        durations.append(timer.nsecsElapsed())

    average_ms = sum(durations) / len(durations) / 1e6
    print(f"4096点FFT＋96band 平均 {average_ms:.4f}ms / 最大 {max(durations) / 1e6:.4f}ms")
    assert frame.band_count == SPECTRUM_BAND_COUNT


def test_level_cost_is_recorded_without_a_strict_limit() -> None:
    """Peak／RMS＋Peak holdの1tick分の時間を記録する。"""
    samples = np.sin(
        2.0 * np.pi * 1_000.0 * np.arange(LEVEL_WINDOW_SIZE, dtype=np.float64) / SAMPLE_RATE
    ).astype(np.float32)
    samples.setflags(write=False)
    processor = LevelProcessor()
    timer = QElapsedTimer()
    durations: list[int] = []
    frame = processor.process(samples, samples, elapsed_seconds=0.033)

    for _ in range(200):
        timer.start()
        frame = processor.process(samples, samples, elapsed_seconds=0.033)
        durations.append(timer.nsecsElapsed())

    average_ms = sum(durations) / len(durations) / 1e6
    print(
        f"Peak＋RMS（L／R各4096sample）平均 {average_ms:.4f}ms / 最大 {max(durations) / 1e6:.4f}ms"
    )
    assert frame.left_peak_db > -90.0


def test_three_ring_buffers_stay_fixed_in_size(tap: PcmTap) -> None:
    """mono／L／Rの3本とも容量が増えず、合計メモリが固定になる。"""
    buffer = pcm_buffer()
    capacities = [
        tap.ring_buffer.capacity,
        tap.left_ring_buffer.capacity,
        tap.right_ring_buffer.capacity,
    ]

    for _ in range(300):
        tap.handle_audio_buffer(buffer)

    assert [
        tap.ring_buffer.capacity,
        tap.left_ring_buffer.capacity,
        tap.right_ring_buffer.capacity,
    ] == capacities
    total_bytes = sum(capacity * 4 for capacity in capacities)
    print(f"リングバッファ合計 {total_bytes / 1024:.0f}KB（3本 × {capacities[0]} sample）")
    assert total_bytes == capacities[0] * 3 * 4


def test_ring_buffer_memory_is_fixed() -> None:
    """リングバッファのメモリ量は容量だけで決まる（48kHz 2秒で約384KB）。"""
    buffer = PcmRingBuffer(96_000)
    chunk = np.zeros(FRAMES_PER_BUFFER, dtype=np.float32)

    for _ in range(1_000):
        buffer.append(chunk)

    assert buffer.capacity == 96_000
    assert buffer.snapshot(96_000).nbytes == 96_000 * 4


# -- タイマー1tickあたりのFFT回数 ------------------------------------------


def test_one_timer_tick_runs_at_most_one_fft(
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """タイマーが何度回ってもtickごとのFFTは1回を超えない。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(600, 200)
    window.show()
    qtbot.waitExposed(window)

    fft_calls: list[int] = []
    original = SpectrumProcessor.process

    def counting(instance: SpectrumProcessor, samples: object, sample_rate: int) -> object:
        fft_calls.append(1)
        return original(instance, samples, sample_rate)  # pyright: ignore[reportArgumentType]

    controller.load(source)
    controller.play()
    tap.handle_audio_buffer(pcm_buffer())

    qtbot.waitUntil(lambda: panel.analysis_count >= 1, timeout=3_000)
    SpectrumProcessor.process = counting  # pyright: ignore[reportAttributeAccessIssue]
    try:
        ticks_before = panel.snapshot_count
        qtbot.waitUntil(lambda: panel.snapshot_count >= ticks_before + 10, timeout=5_000)
        ticks = panel.snapshot_count - ticks_before
    finally:
        SpectrumProcessor.process = original  # pyright: ignore[reportAttributeAccessIssue]

    assert len(fft_calls) <= ticks
    panel.shutdown()


def test_one_timer_tick_runs_at_most_one_level_calculation(
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """タイマーが何度回ってもtickごとのPeak／RMS計算は1回を超えない。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(600, 260)
    window.show()
    qtbot.waitExposed(window)

    controller.load(source)
    controller.play()
    tap.handle_audio_buffer(pcm_buffer())

    qtbot.waitUntil(lambda: panel.level_count >= 1, timeout=3_000)
    ticks_before = panel.stereo_snapshot_count
    levels_before = panel.level_count
    qtbot.waitUntil(lambda: panel.stereo_snapshot_count >= ticks_before + 10, timeout=5_000)

    ticks = panel.stereo_snapshot_count - ticks_before
    assert panel.level_count - levels_before <= ticks
    # スペクトラムとレベルは同じtickを共有する（snapshotも各1回）。
    assert panel.stereo_snapshot_count == panel.snapshot_count
    panel.shutdown()


def test_gui_heartbeat_continues_during_continuous_spectrum_updates(
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """30FPS相当の更新を続けても、別のQTimerが通常のevent loopで進む。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(800, 200)
    window.show()
    qtbot.waitExposed(window)

    heartbeats: list[int] = []
    heartbeat = QTimer()
    heartbeat.setInterval(SPECTRUM_TIMER_INTERVAL_MS)
    heartbeat.timeout.connect(lambda: heartbeats.append(1))
    heartbeat.start()

    controller.load(source)
    controller.play()
    tap.handle_audio_buffer(pcm_buffer())

    try:
        qtbot.waitUntil(lambda: panel.analysis_count >= 15, timeout=10_000)
        qtbot.waitUntil(lambda: len(heartbeats) >= 10, timeout=10_000)
    finally:
        heartbeat.stop()
        panel.shutdown()

    assert panel.analysis_count >= 15
    assert len(heartbeats) >= 10


def test_bar_count_stays_bounded_across_widths(
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    qtbot: QtBot,
) -> None:
    """幅を変えてもbar数はband数を超えず、描画が増え続けない。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.show()
    qtbot.waitExposed(window)
    controller.load(source)
    controller.play()
    tap.handle_audio_buffer(pcm_buffer())
    qtbot.waitUntil(lambda: panel.spectrum_widget.frame is not None, timeout=3_000)

    for width in (320, 800, 1_920, 3_840):
        window.resize(width, 220)
        panel.spectrum_widget.repaint()
        assert panel.spectrum_widget.last_bar_count <= SPECTRUM_BAND_COUNT

    panel.shutdown()


def test_pcm_tap_module_never_imports_the_fft_or_level() -> None:
    """PcmTapのモジュールがFFT関数もPeak／RMS関数も取り込んでいない。"""
    assert not hasattr(pcm_tap_module, "compute_spectrum")
    assert not hasattr(pcm_tap_module, "SpectrumProcessor")
    assert not hasattr(pcm_tap_module, "LevelProcessor")
    assert not hasattr(pcm_tap_module, "peak_amplitude")
    assert not hasattr(pcm_tap_module, "rms_amplitude")
    # FFT長の定数だけはリングバッファ容量の下限として参照する。
    assert pcm_tap_module.FFT_SIZE == FFT_SIZE

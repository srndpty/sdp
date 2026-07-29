"""SpectrumPanelによる再生状態・タイマー・FFT呼出・表示状態の調停を検証する。

固定sleepは使わず、状態変化とqtbotの待機で判定する。
"""

import struct
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat
from PySide6.QtWidgets import QLabel, QMainWindow
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.spectrum import (
    FFT_SIZE,
    SPECTRUM_TIMER_INTERVAL_MS,
    SpectrumFrame,
)
from sdp.core.playback.controller import PlaybackController
from sdp.services.pcm_tap import PcmTap
from sdp.ui.spectrum_panel import FAILED_MESSAGE, STOPPED_MESSAGE, SpectrumPanel
from sdp.ui.spectrum_widget import NO_SOURCE_MESSAGE, SpectrumWidget


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
    """最小化・復帰を検証するため、top-level windowの中へ置く。"""
    instance = QMainWindow()
    qtbot.addWidget(instance)
    return instance


@pytest.fixture
def panel(
    controller: PlaybackController,
    tap: PcmTap,
    window: QMainWindow,
    qtbot: QtBot,
) -> SpectrumPanel:
    instance = SpectrumPanel(controller, tap)
    window.setCentralWidget(instance)
    window.resize(600, 200)
    window.show()
    qtbot.waitExposed(window)
    return instance


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "A.wav"
    second = tmp_path / "B.wav"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    return first.resolve(), second.resolve()


def pcm_buffer(
    amplitude: int = 16_384,
    frames: int = 2_048,
    sample_rate: int = 48_000,
) -> QAudioBuffer:
    """1kHz相当の識別しやすいInt16 stereo bufferを作る。"""
    t = np.arange(frames, dtype=np.float64) / sample_rate
    wave = (np.sin(2.0 * np.pi * 1_000.0 * t) * amplitude).astype(np.int16)
    interleaved = np.repeat(wave, 2)
    audio_format = QAudioFormat()
    audio_format.setSampleRate(sample_rate)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    data = struct.pack(f"<{interleaved.size}h", *interleaved.tolist())
    return QAudioBuffer(data, audio_format)


def feed(tap: PcmTap, buffer: QAudioBuffer | None = None) -> None:
    tap.handle_audio_buffer(pcm_buffer() if buffer is None else buffer)


def start_playing(
    controller: PlaybackController,
    tap: PcmTap,
    source: Path,
    *,
    feed_pcm: bool = True,
) -> None:
    controller.load(source)
    controller.play()
    if feed_pcm:
        feed(tap)


def widget_of(panel: SpectrumPanel) -> SpectrumWidget:
    return panel.spectrum_widget


# -- 構造 -------------------------------------------------------------------


def test_panel_takes_only_the_controller_and_the_tap(panel: SpectrumPanel, tap: PcmTap) -> None:
    """受け取るのはPlaybackControllerとPcmTapだけで、Widgetは1つ。"""
    assert panel.objectName() == "spectrumPanel"
    assert panel.pcm_tap is tap
    assert len(panel.findChildren(SpectrumWidget)) == 1
    assert panel.findChildren(QLabel) == []


def test_panel_does_not_reference_other_services() -> None:
    """波形解析・PlaylistModel・cache・Backend具体型を参照しない。"""
    import sdp.ui.spectrum_panel as panel_module

    for forbidden in (
        "WaveformAnalysisService",
        "WaveformCache",
        "PlaylistModel",
        "QtMultimediaBackend",
        "QAudioBufferOutput",
        "QAudioBuffer",
    ):
        assert not hasattr(panel_module, forbidden), forbidden


def test_widget_db_floor_matches_the_processor(panel: SpectrumPanel) -> None:
    """Widgetの表示範囲はProcessorのfloorと揃う。"""
    assert widget_of(panel).db_floor == -90.0


# -- タイマーの起動条件 -----------------------------------------------------


def test_timer_is_stopped_without_a_source(panel: SpectrumPanel) -> None:
    """sourceなしではタイマーを動かさない。"""
    assert not panel.is_timer_active
    assert widget_of(panel).status_text == NO_SOURCE_MESSAGE


def test_load_alone_does_not_start_the_timer(
    panel: SpectrumPanel, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """loadだけ（STOPPED）ではPLAYINGまでタイマーを動かさない。"""
    controller.load(sources[0])

    assert not panel.is_timer_active
    assert widget_of(panel).status_text == STOPPED_MESSAGE


def test_playing_starts_the_timer(
    panel: SpectrumPanel, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """PLAYINGでタイマーが動き出す。"""
    controller.load(sources[0])
    controller.play()

    assert panel.is_timer_active
    assert panel.spectrum_widget.status_text == ""


def test_paused_stops_the_timer_and_keeps_the_last_frame(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """PAUSEDでFFTを止め、最後のフレームを静止表示する。"""
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)
    frame = widget_of(panel).frame

    controller.pause()

    assert not panel.is_timer_active
    assert widget_of(panel).frame is frame
    analyses = panel.analysis_count
    qtbot.wait(SPECTRUM_TIMER_INTERVAL_MS * 4)
    assert panel.analysis_count == analyses


def test_resume_restarts_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
) -> None:
    """再開でタイマーが戻る。"""
    start_playing(controller, tap, sources[0])
    controller.pause()

    controller.play()

    assert panel.is_timer_active


def test_stopped_stops_the_timer_and_resets_the_frame(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """STOPPEDでタイマーを止め、旧フレームを残さない。"""
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)

    controller.stop()

    assert not panel.is_timer_active
    assert widget_of(panel).frame is None
    assert widget_of(panel).status_text == STOPPED_MESSAGE


def test_no_media_state_stops_and_resets(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    backend: FakePlaybackBackend,
    qtbot: QtBot,
) -> None:
    """NO_MEDIAでもタイマーを止めてフレームを捨てる。

    文言はControllerのsourceで決まる。sourceを保持したままのNO_MEDIAは
    「停止中」であり、source未設定の初期状態だけがプレースホルダーになる。
    """
    from sdp.core.playback.types import PlaybackState

    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)

    backend.emit_state(PlaybackState.NO_MEDIA)

    assert not panel.is_timer_active
    assert widget_of(panel).frame is None
    assert widget_of(panel).status_text == STOPPED_MESSAGE
    assert tap.available_frame_count == 0


def test_source_change_resets_the_frame(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """source切替で前曲のフレームを持ち越さない。"""
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)

    controller.load(sources[1])

    assert widget_of(panel).frame is None
    assert tap.available_frame_count == 0


def test_sample_rate_change_resets_the_processor(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """sample rate変更でprocessorの平滑化履歴を捨てる。"""
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: panel.analysis_count > 0, timeout=2_000)

    feed(tap, pcm_buffer(sample_rate=44_100))

    qtbot.waitUntil(lambda: tap.sample_rate == 44_100, timeout=2_000)
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)
    frame = widget_of(panel).frame
    assert frame is not None
    assert isinstance(frame, SpectrumFrame)


# -- タイマー1tickの処理 ---------------------------------------------------


def test_each_timeout_takes_one_snapshot_and_one_fft(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """1tickでsnapshot 1回・FFT最大1回になる。"""
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: panel.analysis_count >= 3, timeout=3_000)

    assert panel.snapshot_count == panel.analysis_count
    assert panel.snapshot_count >= 3


def test_timeout_updates_the_widget_frame(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """tickごとにWidgetのフレームが更新される。"""
    start_playing(controller, tap, sources[0])

    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=2_000)
    frame = widget_of(panel).frame
    assert frame is not None
    assert frame.band_count == 96
    assert float(frame.levels_db.max()) > -90.0


def test_snapshot_uses_the_fft_size(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """snapshot長はFFT_SIZEに一致する。"""
    requested: list[int] = []
    original = PcmTap.snapshot

    def record(instance: PcmTap, frame_count: int) -> object:
        requested.append(frame_count)
        return original(instance, frame_count)

    monkeypatch.setattr(PcmTap, "snapshot", record)
    start_playing(controller, tap, sources[0])

    qtbot.waitUntil(lambda: bool(requested), timeout=2_000)

    assert set(requested) == {FFT_SIZE}


def test_no_fft_before_the_first_pcm_arrives(
    panel: SpectrumPanel,
    controller: PlaybackController,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """PCMが届く前はプレースホルダーのまま待ち、FFTしない。"""
    controller.load(sources[0])
    controller.play()

    qtbot.waitUntil(lambda: panel.snapshot_count > 0, timeout=2_000)

    assert panel.analysis_count == 0
    assert widget_of(panel).frame is None


def test_reentrancy_is_prevented(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
) -> None:
    """処理中の再入では二重にFFTしない。"""
    start_playing(controller, tap, sources[0])
    panel._processing = True  # pyright: ignore[reportPrivateUsage]
    before = panel.analysis_count

    panel._on_timeout()  # pyright: ignore[reportPrivateUsage]

    assert panel.analysis_count == before


def test_late_timeout_after_stop_is_safe(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
) -> None:
    """タイマー停止後に古いtimeoutが届いても何もしない。"""
    start_playing(controller, tap, sources[0])
    controller.stop()
    before = panel.analysis_count

    panel._on_timeout()  # pyright: ignore[reportPrivateUsage]

    assert panel.analysis_count == before
    assert widget_of(panel).frame is None


# -- 表示・最小化 -----------------------------------------------------------


def test_hidden_panel_stops_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
) -> None:
    """非表示では不要な更新を止める（SPEC-04）。"""
    start_playing(controller, tap, sources[0])
    assert panel.is_timer_active

    panel.hide()

    assert not panel.is_timer_active


def test_showing_again_while_playing_restarts_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    qtbot: QtBot,
) -> None:
    """再表示かつPLAYINGならタイマーを再開する。"""
    start_playing(controller, tap, sources[0])
    panel.hide()

    panel.show()
    qtbot.waitUntil(lambda: panel.is_timer_active, timeout=2_000)

    assert panel.is_timer_active


def test_showing_again_while_stopped_does_not_start_the_timer(
    panel: SpectrumPanel, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """停止中に再表示してもタイマーは動かさない。"""
    controller.load(sources[0])
    panel.hide()

    panel.show()

    assert not panel.is_timer_active


def test_minimizing_the_window_stops_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    window: QMainWindow,
    qtbot: QtBot,
) -> None:
    """top-level windowの最小化でタイマーを止める。"""
    start_playing(controller, tap, sources[0])
    assert panel.is_timer_active

    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.waitUntil(lambda: not panel.is_timer_active, timeout=3_000)

    assert not panel.is_timer_active


def test_restoring_the_window_restarts_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    window: QMainWindow,
    qtbot: QtBot,
) -> None:
    """復帰かつPLAYINGならタイマーを再開する。"""
    start_playing(controller, tap, sources[0])
    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.waitUntil(lambda: not panel.is_timer_active, timeout=3_000)

    window.setWindowState(Qt.WindowState.WindowNoState)
    qtbot.waitUntil(lambda: panel.is_timer_active, timeout=3_000)

    assert panel.is_timer_active


# -- 失敗時の独立性 ---------------------------------------------------------


def test_analysis_failure_does_not_change_playback(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    backend: FakePlaybackBackend,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """FFT失敗でも再生を止めず、Controllerへ何も要求しない。"""
    from sdp.core.analysis.spectrum import SpectrumProcessor
    from sdp.core.playback.types import PlaybackState

    def explode(*args: object, **kwargs: object) -> SpectrumFrame:
        raise RuntimeError("想定外のFFT失敗")

    monkeypatch.setattr(SpectrumProcessor, "process", explode)
    start_playing(controller, tap, sources[0])
    calls_before = list(backend.calls)

    qtbot.waitUntil(lambda: widget_of(panel).status_text == FAILED_MESSAGE, timeout=3_000)

    assert not panel.is_timer_active
    assert widget_of(panel).frame is None
    assert backend.calls == calls_before
    assert controller.state is PlaybackState.PLAYING


def test_source_change_recovers_from_a_failed_state(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """失敗後もsource変更で通常表示へ戻れる。"""
    from sdp.core.analysis.spectrum import SpectrumProcessor

    original = SpectrumProcessor.process

    def explode(*args: object, **kwargs: object) -> SpectrumFrame:
        raise RuntimeError("想定外のFFT失敗")

    monkeypatch.setattr(SpectrumProcessor, "process", explode)
    start_playing(controller, tap, sources[0])
    qtbot.waitUntil(lambda: widget_of(panel).status_text == FAILED_MESSAGE, timeout=3_000)

    monkeypatch.setattr(SpectrumProcessor, "process", original)
    start_playing(controller, tap, sources[1])

    assert panel.is_timer_active
    qtbot.waitUntil(lambda: widget_of(panel).frame is not None, timeout=3_000)


# -- 後始末 -----------------------------------------------------------------


def test_shutdown_stops_the_timer(
    panel: SpectrumPanel,
    controller: PlaybackController,
    tap: PcmTap,
    sources: tuple[Path, Path],
) -> None:
    """終了処理でタイマーを止める（冪等）。"""
    start_playing(controller, tap, sources[0])

    panel.shutdown()
    panel.shutdown()

    assert not panel.is_timer_active

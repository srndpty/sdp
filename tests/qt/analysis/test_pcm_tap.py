"""PcmTapによるQAudioBuffer受領・mono化・リングバッファ書込・clear契約を検証する。"""

import logging
import struct
import threading
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtMultimedia import QAudioBuffer, QAudioBufferOutput, QAudioFormat
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.ring_buffer import PcmRingBuffer, pcm_ring_capacity
from sdp.core.analysis.spectrum import FFT_SIZE
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.services.pcm_tap import PcmTap


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
def sources(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "A.wav"
    second = tmp_path / "B.wav"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    return first.resolve(), second.resolve()


def audio_format(
    sample_format: QAudioFormat.SampleFormat = QAudioFormat.SampleFormat.Int16,
    channels: int = 2,
    sample_rate: int = 48_000,
) -> QAudioFormat:
    value = QAudioFormat()
    value.setSampleRate(sample_rate)
    value.setChannelCount(channels)
    value.setSampleFormat(sample_format)
    return value


def int16_buffer(
    values: list[int],
    channels: int = 2,
    sample_rate: int = 48_000,
) -> QAudioBuffer:
    data = struct.pack(f"<{len(values)}h", *values)
    return QAudioBuffer(data, audio_format(channels=channels, sample_rate=sample_rate))


def send(tap: PcmTap, buffer: object) -> None:
    """QAudioBufferOutput から届いた場合と同じ経路でtapへ渡す。"""
    tap.handle_audio_buffer(buffer)


# -- 初期状態 ---------------------------------------------------------------


def test_initial_state_without_a_source(tap: PcmTap) -> None:
    """sourceなしではsample rate未確定・PCMなしで安全に始まる。"""
    assert tap.sample_rate == 0
    assert tap.available_frame_count == 0
    assert tap.received_buffer_count == 0
    assert tap.discarded_buffer_count == 0
    assert np.all(tap.snapshot(FFT_SIZE) == 0.0)


def test_default_ring_buffer_holds_two_seconds_at_48khz(tap: PcmTap) -> None:
    """既定容量は48kHzで2秒（FFT長を下回らない）。"""
    assert tap.ring_buffer.capacity == pcm_ring_capacity(48_000, FFT_SIZE)


def test_uses_an_injected_ring_buffer(controller: PlaybackController) -> None:
    """外部から与えたリングバッファを共有する。"""
    ring_buffer = PcmRingBuffer(1_024)

    tap = PcmTap(controller, ring_buffer)

    assert tap.ring_buffer is ring_buffer


# -- 正常な buffer ----------------------------------------------------------


def test_valid_buffer_is_converted_and_appended(tap: PcmTap) -> None:
    """有効bufferはmono化されてリングバッファへ入る。"""
    send(tap, int16_buffer([16384, 0, -32768, -32768]))

    assert tap.received_buffer_count == 1
    assert tap.discarded_buffer_count == 0
    assert tap.available_frame_count == 2
    assert tap.snapshot(2).tolist() == pytest.approx([0.25, -1.0])


def test_sample_rate_is_published_from_the_buffer(tap: PcmTap, qtbot: QtBot) -> None:
    """最初のbufferでsample rateを公開する。"""
    notified: list[int] = []
    tap.sample_rate_changed.connect(notified.append)

    with qtbot.waitSignal(tap.sample_rate_changed, timeout=1_000):
        send(tap, int16_buffer([0, 0], sample_rate=44_100))

    assert notified == [44_100]
    assert tap.sample_rate == 44_100


def test_same_sample_rate_is_not_renotified(tap: PcmTap) -> None:
    """同じsample rateでは重複通知しない。"""
    rates: list[int] = []
    tap.sample_rate_changed.connect(rates.append)

    send(tap, int16_buffer([0, 0]))
    send(tap, int16_buffer([0, 0]))

    assert rates == [48_000]


def test_mono_source_is_accepted(tap: PcmTap) -> None:
    """monoのbufferもそのまま扱える。"""
    send(tap, int16_buffer([16384, -16384], channels=1))

    assert tap.snapshot(2).tolist() == pytest.approx([0.5, -0.5])


def test_appends_accumulate_across_buffers(tap: PcmTap) -> None:
    """連続したbufferは順に追記される。"""
    send(tap, int16_buffer([32767, 32767]))
    send(tap, int16_buffer([-32768, -32768]))

    assert tap.available_frame_count == 2
    assert tap.snapshot(2).tolist() == pytest.approx([1.0, -1.0], abs=1e-4)


# -- format 変更 ------------------------------------------------------------


def test_sample_rate_change_clears_and_resizes_the_ring_buffer(tap: PcmTap, qtbot: QtBot) -> None:
    """sample rate変更で旧formatのPCMを混ぜず、容量も作り直す。"""
    send(tap, int16_buffer([32767, 32767], sample_rate=44_100))
    assert tap.available_frame_count == 1
    assert tap.ring_buffer.capacity == pcm_ring_capacity(44_100, FFT_SIZE)

    with qtbot.waitSignal(tap.sample_rate_changed, timeout=1_000):
        send(tap, int16_buffer([0, 0], sample_rate=48_000))

    assert tap.sample_rate == 48_000
    assert tap.ring_buffer.capacity == pcm_ring_capacity(48_000, FFT_SIZE)
    # 旧44.1kHzの1frameは残らず、新しい1frameだけになる。
    assert tap.available_frame_count == 1
    assert tap.snapshot(2).tolist() == [0.0, 0.0]


# -- 無効 buffer ------------------------------------------------------------


def test_invalid_buffer_is_counted_and_discarded(tap: PcmTap) -> None:
    """再生終端の空buffer（P5-A probeで実測）は捨てて数える。"""
    send(tap, QAudioBuffer())

    assert tap.received_buffer_count == 0
    assert tap.discarded_buffer_count == 1
    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0


def test_non_buffer_value_is_discarded(tap: PcmTap) -> None:
    """QAudioBuffer以外が通知されても落ちない。"""
    send(tap, "not a buffer")

    assert tap.discarded_buffer_count == 1


def test_discarded_buffers_do_not_flood_the_log(
    tap: PcmTap, caplog: pytest.LogCaptureFixture
) -> None:
    """音声コールバックからログを大量に出さない。"""
    with caplog.at_level(logging.DEBUG, logger="sdp.services.pcm_tap"):
        for _ in range(250):
            send(tap, QAudioBuffer())

    assert tap.discarded_buffer_count == 250
    assert len(caplog.records) <= 3


def test_callback_does_not_leak_exceptions(tap: PcmTap, monkeypatch: pytest.MonkeyPatch) -> None:
    """予期しない例外もコールバックの外へ漏らさない（PySide6は握り潰す）。"""
    import sdp.services.pcm_tap as pcm_tap_module

    def explode(buffer: object) -> tuple[object, int]:
        del buffer
        raise RuntimeError("想定外")

    monkeypatch.setattr(pcm_tap_module, "audio_buffer_to_mono", explode)

    send(tap, int16_buffer([0, 0]))

    assert tap.discarded_buffer_count == 1
    assert tap.received_buffer_count == 0


# -- clear 契約 -------------------------------------------------------------


def test_source_change_clears_the_ring_buffer(
    tap: PcmTap, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """source変更で旧PCMを即時破棄する。"""
    controller.load(sources[0])
    send(tap, int16_buffer([32767, 32767]))
    assert tap.available_frame_count == 1

    controller.load(sources[1])

    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0


def test_stop_clears_the_ring_buffer(
    tap: PcmTap, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """停止で旧PCMを残さない（次曲へ持ち越さない）。"""
    controller.load(sources[0])
    controller.play()
    send(tap, int16_buffer([32767, 32767]))

    controller.stop()

    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0


def test_pause_keeps_the_ring_buffer(
    tap: PcmTap, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """一時停止では最後のPCMを保持する（静止表示のため）。"""
    controller.load(sources[0])
    controller.play()
    send(tap, int16_buffer([32767, 32767]))

    controller.pause()

    assert tap.available_frame_count == 1
    assert tap.sample_rate == 48_000


def test_source_release_clears_the_ring_buffer(
    tap: PcmTap, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """NO_MEDIAへ戻った場合もPCMを破棄する。"""
    send(tap, int16_buffer([32767, 32767]))

    backend.emit_state(PlaybackState.NO_MEDIA)

    assert tap.available_frame_count == 0


def test_clear_is_idempotent(tap: PcmTap) -> None:
    """clearの多重呼出でも通知が増えない。"""
    rates: list[int] = []
    tap.sample_rate_changed.connect(rates.append)
    send(tap, int16_buffer([0, 0]))

    tap.clear()
    tap.clear()

    assert rates == [48_000, 0]


# -- 責務の境界 -------------------------------------------------------------


def test_tap_does_not_touch_the_controller(
    tap: PcmTap, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """PCM受信でControllerへsetterやtransportを呼ばない。"""
    controller.play()
    calls_before = list(backend.calls)

    for _ in range(5):
        send(tap, int16_buffer([1000, 1000]))

    assert backend.calls == calls_before


def test_tap_module_does_not_import_fft_or_widgets() -> None:
    """PcmTapはFFT・Hann窓・dB変換・QWidgetを持たない。"""
    import sdp.services.pcm_tap as pcm_tap_module

    for forbidden in (
        "compute_spectrum",
        "SpectrumProcessor",
        "SpectrumFrame",
        "QWidget",
        "QPainter",
        "PlaylistModel",
        "WaveformAnalysisService",
    ):
        assert not hasattr(pcm_tap_module, forbidden), forbidden


def test_tap_is_not_a_widget(tap: PcmTap) -> None:
    """QObjectであってWidgetではない。"""
    assert not isinstance(tap, QWidget)


def test_tap_does_not_hold_the_audio_buffer(tap: PcmTap) -> None:
    """QAudioBufferをリングバッファ外へ保持しない。"""
    send(tap, int16_buffer([16384, 16384]))

    held: list[object] = [
        value
        for value in vars(tap).values()
        if isinstance(value, QAudioBuffer | QAudioFormat | memoryview)
    ]

    assert held == []


def test_callback_thread_is_the_gui_thread(tap: PcmTap) -> None:
    """コールバックの実行threadを記録する（P0-C・P5-A probeともGUI thread）。"""
    observed: list[str] = []
    original = tap.ring_buffer.append

    def record(samples: object) -> None:
        observed.append(threading.current_thread().name)
        original(samples)  # pyright: ignore[reportArgumentType]

    tap.ring_buffer.append = record  # pyright: ignore[reportAttributeAccessIssue]
    send(tap, int16_buffer([0, 0]))

    assert observed == [threading.current_thread().name]


# -- 接続と後始末 -----------------------------------------------------------


def test_connects_and_receives_from_a_real_audio_buffer_output(tap: PcmTap) -> None:
    """実QAudioBufferOutputのSignal経路でPCMが届く。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_output(buffer_output)

    buffer_output.audioBufferReceived.emit(int16_buffer([16384, 16384]))

    assert tap.received_buffer_count == 1
    assert tap.snapshot(1).tolist() == pytest.approx([0.5])


def test_reconnecting_the_same_output_does_not_duplicate_deliveries(tap: PcmTap) -> None:
    """同じ出力を二重接続しても1回しか受け取らない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_output(buffer_output)
    tap.connect_audio_buffer_output(buffer_output)

    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))

    assert tap.received_buffer_count == 1


def test_disconnect_stops_further_deliveries(tap: PcmTap) -> None:
    """切断後はPCMを受け取らない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_output(buffer_output)

    tap.disconnect_audio_buffer_output()
    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))

    assert tap.received_buffer_count == 0


def test_shutdown_is_idempotent_and_clears(
    tap: PcmTap, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """shutdownはPCM受信とsource監視を止め、二重呼出でも落ちない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_output(buffer_output)
    send(tap, int16_buffer([32767, 32767]))

    tap.shutdown()
    tap.shutdown()

    assert tap.available_frame_count == 0
    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))
    assert tap.received_buffer_count == 1
    # source監視も解除済みのため、以降のController通知で例外にならない。
    controller.load(sources[0])
    assert tap.sample_rate == 0


def test_deleted_tap_does_not_crash_on_a_later_signal(
    controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """PcmTap破棄後のControllerシグナルでクラッシュしない。"""
    tap = PcmTap(controller)
    tap.shutdown()
    del tap

    controller.load(sources[0])
    controller.play()
    controller.stop()

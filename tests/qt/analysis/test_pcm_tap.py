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
from sdp.services.pcm_tap import PcmTap, VisualizationPcmSnapshot


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
    """既定容量は48kHzで2秒（FFT長を下回らない）。mono／L／Rで同じ固定容量。"""
    expected = pcm_ring_capacity(48_000, FFT_SIZE)

    assert tap.ring_buffer.capacity == expected
    assert tap.left_ring_buffer.capacity == expected
    assert tap.right_ring_buffer.capacity == expected


def test_uses_an_injected_ring_buffer(controller: PlaybackController) -> None:
    """外部から与えたmonoリングバッファを共有し、L／Rも同容量で用意する。"""
    ring_buffer = PcmRingBuffer(1_024)

    tap = PcmTap(controller, ring_buffer)

    assert tap.ring_buffer is ring_buffer
    assert tap.left_ring_buffer.capacity == 1_024
    assert tap.right_ring_buffer.capacity == 1_024
    assert tap.left_ring_buffer is not tap.right_ring_buffer


def test_channel_count_is_unknown_before_the_first_buffer(tap: PcmTap) -> None:
    """PCMが届く前のchannel countは0。"""
    assert tap.channel_count == 0
    left, right = tap.snapshot_stereo(FFT_SIZE)
    assert np.all(left == 0.0)
    assert np.all(right == 0.0)


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


def test_stereo_buffer_fills_mono_left_and_right_together(tap: PcmTap) -> None:
    """1回のbuffer受信でmono／left／rightへ同時にappendする。"""
    # frame0: L=0.5 / R=-0.5、frame1: L=-1.0 / R=1.0
    send(tap, int16_buffer([16384, -16384, -32768, 32767]))

    left, right = tap.snapshot_stereo(2)

    assert tap.channel_count == 2
    assert tap.available_frame_count == 2
    assert left.tolist() == pytest.approx([0.5, -1.0], abs=1e-4)
    assert right.tolist() == pytest.approx([-0.5, 1.0], abs=1e-4)
    # monoは左右の平均。左右が混ざっていないことを同時に確認する。
    assert tap.snapshot_mono(2).tolist() == pytest.approx([0.0, 0.0], abs=1e-4)


def test_mono_buffer_duplicates_the_channel_into_left_and_right(tap: PcmTap) -> None:
    """mono音源では左右が同じ値になる。"""
    send(tap, int16_buffer([16384, -16384], channels=1))

    left, right = tap.snapshot_stereo(2)

    assert tap.channel_count == 1
    assert left.tolist() == pytest.approx([0.5, -0.5])
    assert right.tolist() == left.tolist()
    assert tap.snapshot_mono(2).tolist() == left.tolist()


def test_snapshot_and_snapshot_mono_agree(tap: PcmTap) -> None:
    """既存の``snapshot``はmonoの互換APIとして残る。"""
    send(tap, int16_buffer([16384, 0]))

    assert tap.snapshot(4).tolist() == tap.snapshot_mono(4).tolist()


def test_stereo_snapshots_are_independent_read_only_copies(tap: PcmTap) -> None:
    """L／Rのsnapshotは別配列でread-onlyになる。"""
    send(tap, int16_buffer([16384, -16384]))

    left, right = tap.snapshot_stereo(8)

    assert left is not right
    assert not left.flags.writeable
    assert not right.flags.writeable
    assert not np.shares_memory(left, right)


def test_visualization_snapshot_contains_one_format_generation(tap: PcmTap) -> None:
    """統合snapshotはformatとmono／L／Rを同一世代から返す。"""
    send(tap, int16_buffer([3_277, 6_554], sample_rate=44_100))

    snapshot = tap.snapshot_visualization(mono_frames=1, level_frames=1)

    assert snapshot.sample_rate == 44_100
    assert snapshot.channel_count == 2
    assert snapshot.mono.tolist() == pytest.approx([0.15], abs=1e-4)
    assert snapshot.left.tolist() == pytest.approx([0.1], abs=1e-4)
    assert snapshot.right.tolist() == pytest.approx([0.2], abs=1e-4)
    assert not snapshot.mono.flags.writeable
    assert not snapshot.left.flags.writeable
    assert not snapshot.right.flags.writeable


def test_visualization_snapshot_cannot_observe_a_partial_format_switch(tap: PcmTap) -> None:
    """writer threadのformat切替途中でも、旧新世代を混在させない。"""
    send(tap, int16_buffer([1_000, 2_000], sample_rate=44_100))
    mono_appended = threading.Event()
    release_writer = threading.Event()
    snapshot_started = threading.Event()
    snapshot_finished = threading.Event()
    original_append = tap.ring_buffer.append
    observed: list[VisualizationPcmSnapshot] = []

    def blocking_append(samples: object) -> None:
        original_append(samples)  # pyright: ignore[reportArgumentType]
        mono_appended.set()
        assert release_writer.wait(timeout=2.0)

    def write_new_generation() -> None:
        send(tap, int16_buffer([3_000, 5_000], sample_rate=48_000))

    def take_snapshot() -> None:
        snapshot_started.set()
        observed.append(tap.snapshot_visualization(mono_frames=1, level_frames=1))
        snapshot_finished.set()

    tap.ring_buffer.append = blocking_append  # pyright: ignore[reportAttributeAccessIssue]
    writer = threading.Thread(target=write_new_generation)
    reader = threading.Thread(target=take_snapshot)
    writer.start()
    try:
        assert mono_appended.wait(timeout=2.0)
        reader.start()
        assert snapshot_started.wait(timeout=2.0)
        # mono追記中はTap全体のlockを保持しているため、統合snapshotは完了しない。
        assert not snapshot_finished.wait(timeout=0.05)
    finally:
        release_writer.set()
        writer.join(timeout=2.0)
        if reader.ident is not None:
            reader.join(timeout=2.0)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert len(observed) == 1
    snapshot = observed[0]
    assert snapshot.sample_rate == 48_000
    assert snapshot.channel_count == 2
    assert snapshot.mono.tolist() == pytest.approx([4_000 / 32_768], abs=1e-4)
    assert snapshot.left.tolist() == pytest.approx([3_000 / 32_768], abs=1e-4)
    assert snapshot.right.tolist() == pytest.approx([5_000 / 32_768], abs=1e-4)


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


def test_sample_rate_change_rebuilds_all_three_buffers(tap: PcmTap) -> None:
    """sample rate変更ではmono／L／Rの3本すべてを作り直す。"""
    send(tap, int16_buffer([32767, -32768], sample_rate=44_100))
    assert tap.left_ring_buffer.available == 1
    assert tap.right_ring_buffer.available == 1

    send(tap, int16_buffer([0, 0], sample_rate=48_000))

    expected = pcm_ring_capacity(48_000, FFT_SIZE)
    for buffer in (tap.ring_buffer, tap.left_ring_buffer, tap.right_ring_buffer):
        assert buffer.capacity == expected
        assert buffer.available == 1
    left, right = tap.snapshot_stereo(2)
    assert left.tolist() == [0.0, 0.0]
    assert right.tolist() == [0.0, 0.0]


def test_channel_count_change_rebuilds_all_three_buffers(tap: PcmTap, qtbot: QtBot) -> None:
    """channel count変更でも旧layoutのPCMを混ぜず、3本を作り直して通知する。"""
    send(tap, int16_buffer([32767, -32768], channels=2))
    assert tap.channel_count == 2

    counts: list[int] = []
    tap.channel_count_changed.connect(counts.append)

    with qtbot.waitSignal(tap.channel_count_changed, timeout=1_000):
        send(tap, int16_buffer([16384], channels=1))

    assert counts == [1]
    assert tap.channel_count == 1
    # 旧stereoの1frameは残らない。
    assert tap.available_frame_count == 1
    left, right = tap.snapshot_stereo(1)
    assert left.tolist() == pytest.approx([0.5])
    assert right.tolist() == pytest.approx([0.5])


def test_same_channel_count_is_not_renotified(tap: PcmTap) -> None:
    """同じchannel countでは重複通知しない。"""
    counts: list[int] = []
    tap.channel_count_changed.connect(counts.append)

    send(tap, int16_buffer([0, 0]))
    send(tap, int16_buffer([0, 0]))

    assert counts == [2]


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

    def explode(buffer: object) -> object:
        del buffer
        raise RuntimeError("想定外")

    monkeypatch.setattr(pcm_tap_module, "audio_buffer_to_pcm_chunk", explode)

    send(tap, int16_buffer([0, 0]))

    assert tap.discarded_buffer_count == 1
    assert tap.received_buffer_count == 0


def test_unexpected_callback_errors_do_not_flood_the_log(
    tap: PcmTap,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """予期しない例外は初回だけtracebackを残し、以後は100件単位で集約する。"""
    import sdp.services.pcm_tap as pcm_tap_module

    def explode(buffer: object) -> object:
        del buffer
        raise RuntimeError("想定外")

    monkeypatch.setattr(pcm_tap_module, "audio_buffer_to_pcm_chunk", explode)

    with caplog.at_level(logging.WARNING, logger="sdp.services.pcm_tap"):
        for _ in range(250):
            send(tap, int16_buffer([0, 0]))

    assert tap.discarded_buffer_count == 250
    assert len(caplog.records) == 3
    assert sum(record.exc_info is not None for record in caplog.records) == 1
    assert "累計100件" in caplog.records[1].message
    assert "累計200件" in caplog.records[2].message


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
    assert tap.channel_count == 0
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0


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
    assert tap.channel_count == 0
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0


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
    assert tap.channel_count == 2
    assert tap.left_ring_buffer.available == 1
    assert tap.right_ring_buffer.available == 1


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
    counts: list[int] = []
    tap.sample_rate_changed.connect(rates.append)
    tap.channel_count_changed.connect(counts.append)
    send(tap, int16_buffer([0, 0]))

    tap.clear()
    tap.clear()

    assert rates == [48_000, 0]
    assert counts == [2, 0]


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


def test_tap_module_does_not_import_fft_level_or_widgets() -> None:
    """PcmTapはFFT・Hann窓・Peak／RMS・dB変換・Peak hold・QWidgetを持たない。"""
    import sdp.services.pcm_tap as pcm_tap_module

    for forbidden in (
        "compute_spectrum",
        "SpectrumProcessor",
        "SpectrumFrame",
        "LevelProcessor",
        "StereoLevelFrame",
        "peak_amplitude",
        "rms_amplitude",
        "amplitude_to_dbfs",
        "QTimer",
        "QWidget",
        "QPainter",
        "PlaylistModel",
        "WaveformAnalysisService",
    ):
        assert not hasattr(pcm_tap_module, forbidden), forbidden


def test_callback_does_not_compute_peak_or_rms(
    tap: PcmTap, monkeypatch: pytest.MonkeyPatch
) -> None:
    """音声コールバック内でPeak／RMSを計算しない（GUIタイマー側の責務）。"""
    import sdp.core.analysis.level as level_module

    calls: list[str] = []

    def record_peak(samples: object) -> float:
        calls.append("peak")
        return 0.0

    def record_rms(samples: object) -> float:
        calls.append("rms")
        return 0.0

    monkeypatch.setattr(level_module, "peak_amplitude", record_peak)
    monkeypatch.setattr(level_module, "rms_amplitude", record_rms)

    for _ in range(5):
        send(tap, int16_buffer([16384, -16384]))

    assert calls == []
    assert tap.received_buffer_count == 5


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


def test_ring_buffer_capacity_stays_fixed_under_many_buffers(tap: PcmTap) -> None:
    """大量のPCMでも3本の容量は増えない（全履歴を保持しない）。"""
    expected = pcm_ring_capacity(48_000, FFT_SIZE)

    for _ in range(200):
        send(tap, int16_buffer([16384, -16384] * 512))

    for buffer in (tap.ring_buffer, tap.left_ring_buffer, tap.right_ring_buffer):
        assert buffer.capacity == expected
        assert buffer.available == expected


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
    """実QAudioBufferOutputのSignal経路でPCMが届く。

    本番では Backend の世代フィルター済みポートへ接続するが、``QAudioBuffer`` を
    運ぶSignalであれば同じように扱えることを、実Qtオブジェクトで確かめる。
    """
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_source(buffer_output.audioBufferReceived)

    buffer_output.audioBufferReceived.emit(int16_buffer([16384, 16384]))

    assert tap.received_buffer_count == 1
    assert tap.snapshot(1).tolist() == pytest.approx([0.5])


def test_reconnecting_the_same_source_does_not_duplicate_deliveries(tap: PcmTap) -> None:
    """同じ供給口を二重接続しても1回しか受け取らない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_source(buffer_output.audioBufferReceived)
    tap.connect_audio_buffer_source(buffer_output.audioBufferReceived)

    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))

    assert tap.received_buffer_count == 1


def test_disconnect_stops_further_deliveries(tap: PcmTap) -> None:
    """切断後はPCMを受け取らない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_source(buffer_output.audioBufferReceived)

    tap.disconnect_audio_buffer_source()
    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))

    assert tap.received_buffer_count == 0


def test_shutdown_is_idempotent_and_clears(
    tap: PcmTap, controller: PlaybackController, sources: tuple[Path, Path]
) -> None:
    """shutdown後は全入力を拒否し、二重呼出でも状態を変えない。"""
    buffer_output = QAudioBufferOutput()
    tap.connect_audio_buffer_source(buffer_output.audioBufferReceived)
    send(tap, int16_buffer([32767, 32767]))

    tap.shutdown()
    tap.shutdown()

    assert tap.available_frame_count == 0
    buffer_output.audioBufferReceived.emit(int16_buffer([0, 0]))
    send(tap, int16_buffer([0, 0]))
    assert tap.received_buffer_count == 1
    assert tap.discarded_buffer_count == 0
    # source監視も解除済みのため、以降のController通知で例外にならない。
    controller.load(sources[0])
    controller.play()
    controller.stop()
    assert tap.sample_rate == 0
    assert tap.channel_count == 0
    assert tap.available_frame_count == 0
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0

    with pytest.raises(RuntimeError, match="shutdown後"):
        tap.connect_audio_buffer_source(QAudioBufferOutput().audioBufferReceived)


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

"""実 QAudioBufferOutput から再生中PCMを取得できることを実音源で検証する。

音量は 0.0 固定で可聴音を出さない。待機は必ずタイムアウト付きで行う。
取得PCMは音量・ミュート適用**前**の信号なので、音量 0.0 でもレベルは振れる。
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QMainWindow
from pytestqt.qtbot import QtBot

from sdp.core.analysis.level import (
    LEVEL_DB_FLOOR,
    LEVEL_WINDOW_SIZE,
    LevelProcessor,
    amplitude_to_dbfs,
    peak_amplitude,
    rms_amplitude,
)
from sdp.core.analysis.spectrum import FFT_SIZE, SpectrumProcessor, compute_spectrum
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playback.types import PlaybackState
from sdp.services.pcm_tap import PcmTap
from sdp.ui.spectrum_panel import SpectrumPanel

pytestmark = pytest.mark.audio

TIMEOUT_MS = 15_000


@pytest.fixture
def backend() -> Iterator[QtMultimediaBackend]:
    instance = QtMultimediaBackend()
    yield instance
    instance.stop()


@pytest.fixture
def controller(backend: QtMultimediaBackend) -> PlaybackController:
    instance = PlaybackController(backend)
    # 可聴音を出さない。P0-C 実測どおり音量は取得PCMへ影響しない。
    instance.set_volume(0.0)
    return instance


@pytest.fixture
def tap(controller: PlaybackController, backend: QtMultimediaBackend) -> Iterator[PcmTap]:
    instance = PcmTap(controller)
    instance.connect_audio_buffer_source(backend.audio_buffer_received)
    yield instance
    instance.shutdown()


def wav(test_audio_dir: Path) -> Path:
    return test_audio_dir / "sine440.wav"


def mp3(test_audio_dir: Path) -> Path:
    return test_audio_dir / "sine440.mp3"


def sweep(test_audio_dir: Path) -> Path:
    """左右同振幅（0.5 / 0.5）のスイープ音源。"""
    return test_audio_dir / "sweep.wav"


def play(controller: PlaybackController, source: Path) -> None:
    controller.load(source)
    controller.play()


def wait_for_pcm(tap: PcmTap, qtbot: QtBot, minimum: int = 3) -> None:
    qtbot.waitUntil(lambda: tap.received_buffer_count >= minimum, timeout=TIMEOUT_MS)


# -- PCM 取得 ---------------------------------------------------------------


def test_wav_playback_delivers_pcm_buffers(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """WAV 再生で PCM buffer を受信し、mono float32 が取り出せる。"""
    play(controller, wav(test_audio_dir))

    wait_for_pcm(tap, qtbot)

    # P0-C 実測: WAV は Int16 / 44100Hz / 2ch。
    assert tap.sample_rate == 44_100
    samples = tap.snapshot(FFT_SIZE)
    assert samples.dtype == np.dtype(np.float32)
    assert samples.shape == (FFT_SIZE,)
    assert np.all(np.isfinite(samples))
    assert float(np.abs(samples).max()) > 0.0
    assert tap.discarded_buffer_count == 0


def test_mp3_playback_delivers_pcm_buffers(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """MP3（P0-C 実測では Float 形式）でも PCM buffer を受信する。"""
    play(controller, mp3(test_audio_dir))

    wait_for_pcm(tap, qtbot)

    assert tap.sample_rate == 44_100
    assert np.all(np.isfinite(tap.snapshot(FFT_SIZE)))


def test_playback_continues_while_tapping_pcm(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """PCM タップを付けても音声出力と position の前進が続き、error も出ない。"""
    errors: list[object] = []
    controller.error_occurred.connect(errors.append)
    play(controller, wav(test_audio_dir))

    wait_for_pcm(tap, qtbot)
    qtbot.waitUntil(lambda: controller.position_ms > 0, timeout=TIMEOUT_MS)

    assert controller.state is PlaybackState.PLAYING
    assert errors == []


def test_sine_peak_is_near_440hz(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """440Hz 音源のスペクトラムのピークが 440Hz 付近へ来る。"""
    play(controller, wav(test_audio_dir))
    qtbot.waitUntil(
        lambda: tap.available_frame_count >= FFT_SIZE and tap.sample_rate > 0,
        timeout=TIMEOUT_MS,
    )

    frame = compute_spectrum(tap.snapshot(FFT_SIZE), tap.sample_rate)
    peak_hz = float(frame.frequencies_hz[int(np.argmax(frame.levels_db))])

    assert peak_hz == pytest.approx(440.0, rel=0.10)


def test_callback_runs_on_the_gui_thread(
    controller: PlaybackController,
    backend: QtMultimediaBackend,
    tap: PcmTap,
    test_audio_dir: Path,
    qtbot: QtBot,
) -> None:
    """audioBufferReceived の受信 thread を実測する（P0-C と同じ結論の再確認）。"""
    observed: set[str] = set()

    def record(buffer: object) -> None:
        del buffer
        observed.add(threading.current_thread().name)

    backend.audio_buffer_received.connect(record)
    play(controller, wav(test_audio_dir))

    wait_for_pcm(tap, qtbot)

    assert observed == {threading.current_thread().name}


# -- 速度・ピッチ -----------------------------------------------------------


@pytest.mark.parametrize("rate", [0.75, 1.5])
def test_pcm_keeps_flowing_at_other_playback_rates(
    controller: PlaybackController,
    tap: PcmTap,
    test_audio_dir: Path,
    qtbot: QtBot,
    rate: float,
) -> None:
    """速度変更後も PCM が届き、format は変わらない（通知間隔だけが変わる）。"""
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    controller.set_playback_rate(rate)
    received = tap.received_buffer_count
    qtbot.waitUntil(lambda: tap.received_buffer_count > received + 2, timeout=TIMEOUT_MS)

    assert tap.sample_rate == 44_100
    assert np.all(np.isfinite(tap.snapshot(FFT_SIZE)))


@pytest.mark.parametrize("pitch_compensation", [True, False])
def test_pcm_keeps_flowing_with_either_pitch_mode(
    controller: PlaybackController,
    tap: PcmTap,
    test_audio_dir: Path,
    qtbot: QtBot,
    pitch_compensation: bool,
) -> None:
    """ピッチ補正の ON/OFF いずれでも PCM の format が変わらない。

    P0-C §8.6 のとおり、取得 PCM は速度・ピッチ処理**前**の信号である。
    """
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    controller.set_playback_rate(2.0)
    controller.set_pitch_compensation(pitch_compensation)
    received = tap.received_buffer_count
    qtbot.waitUntil(lambda: tap.received_buffer_count > received + 2, timeout=TIMEOUT_MS)

    assert tap.sample_rate == 44_100


# -- clear 契約 -------------------------------------------------------------


def test_stop_clears_the_ring_buffer(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """停止でリングバッファが空になる。"""
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    controller.stop()

    qtbot.waitUntil(lambda: tap.available_frame_count == 0, timeout=TIMEOUT_MS)
    assert tap.sample_rate == 0


def test_source_change_clears_the_previous_pcm(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """曲切替で前曲の PCM を持ち越さない（再生中の直接 setSource を含む）。"""
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    controller.load(mp3(test_audio_dir))
    assert tap.available_frame_count == 0

    controller.play()
    wait_for_pcm(tap, qtbot, minimum=tap.received_buffer_count + 3)
    assert tap.sample_rate == 44_100


def test_end_of_media_does_not_leave_stale_pcm(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """再生終端まで進むと停止し、PCM が残らない。

    終端では format 未設定の空 buffer が届くが（P5-A probe 実測）、
    無効 buffer として数えて捨てるだけで再生を妨げない。
    """
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    qtbot.waitUntil(lambda: controller.state is not PlaybackState.PLAYING, timeout=TIMEOUT_MS)

    assert tap.available_frame_count == 0


# -- Panel の統合 -----------------------------------------------------------


def test_spectrum_panel_follows_real_playback(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """実再生で Panel のタイマーが動き、96band のフレームが描かれる。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(700, 220)
    window.show()
    qtbot.waitExposed(window)

    play(controller, wav(test_audio_dir))

    qtbot.waitUntil(lambda: panel.is_timer_active, timeout=TIMEOUT_MS)
    qtbot.waitUntil(lambda: panel.spectrum_widget.frame is not None, timeout=TIMEOUT_MS)
    frame = panel.spectrum_widget.frame
    assert frame is not None
    assert frame.band_count == 96
    assert float(frame.levels_db.max()) > -90.0

    controller.pause()
    qtbot.waitUntil(lambda: not panel.is_timer_active, timeout=TIMEOUT_MS)
    assert panel.spectrum_widget.frame is frame

    panel.shutdown()


# -- レベルメーター（P5-B）--------------------------------------------------


def wait_for_full_window(tap: PcmTap, qtbot: QtBot) -> None:
    qtbot.waitUntil(
        lambda: tap.available_frame_count >= LEVEL_WINDOW_SIZE and tap.sample_rate > 0,
        timeout=TIMEOUT_MS,
    )


def test_wav_playback_delivers_left_and_right_levels(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """WAV 再生で L／R 個別のレベルが取得できる（音量 0.0 でも振れる）。"""
    play(controller, wav(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert tap.channel_count == 2
    assert controller.volume == pytest.approx(0.0)
    assert frame.left_peak_db > LEVEL_DB_FLOOR
    assert frame.right_peak_db > LEVEL_DB_FLOOR
    assert frame.left_rms_db <= frame.left_peak_db
    assert frame.left_peak_hold_db >= frame.left_peak_db


def test_mp3_playback_delivers_left_and_right_levels(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """MP3（Float形式）でも L／R レベルが取得できる。"""
    play(controller, mp3(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert frame.left_peak_db > LEVEL_DB_FLOOR
    assert frame.right_peak_db > LEVEL_DB_FLOOR


def test_in_phase_stereo_source_has_matching_channels(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """左右同振幅のスイープ音源では L と R がおおむね一致する。"""
    play(controller, sweep(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert frame.left_peak_db == pytest.approx(frame.right_peak_db, abs=0.5)
    assert frame.left_rms_db == pytest.approx(frame.right_rms_db, abs=0.5)


def test_asymmetric_source_shows_a_louder_left_channel(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """sine440 は L が R より 6dB 大きい音源のため、メーターにも左右差が出る。

    振幅は ``tools/gen_test_audio.py`` で L=0.5 / R=0.25 と決めている。
    """
    play(controller, wav(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert frame.left_peak_db > frame.right_peak_db
    assert frame.left_peak_db == pytest.approx(-6.02, abs=0.2)
    assert frame.right_peak_db == pytest.approx(-12.04, abs=0.2)


def test_sine_levels_match_the_known_amplitude(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """440Hz 正弦波の Peak は既知振幅、RMS は Peak - 約3.01dB になる。"""
    play(controller, wav(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, _ = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    peak_db = amplitude_to_dbfs(peak_amplitude(left))
    rms_db = amplitude_to_dbfs(rms_amplitude(left))

    assert peak_db == pytest.approx(-6.02, abs=0.2)
    assert rms_db == pytest.approx(peak_db - 3.01, abs=0.5)


def test_muted_playback_still_produces_levels(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """ミュート中でもレベルは振れる（出力音量計ではない）。"""
    controller.set_muted(True)
    play(controller, wav(test_audio_dir))
    wait_for_full_window(tap, qtbot)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert controller.muted is True
    assert frame.left_peak_db > LEVEL_DB_FLOOR
    assert frame.right_peak_db > LEVEL_DB_FLOOR


@pytest.mark.parametrize("rate", [0.75, 2.0])
def test_levels_keep_updating_at_other_playback_rates(
    controller: PlaybackController,
    tap: PcmTap,
    test_audio_dir: Path,
    qtbot: QtBot,
    rate: float,
) -> None:
    """速度変更後もレベルの取得が続く（取得PCMは速度処理前の信号）。"""
    play(controller, wav(test_audio_dir))
    wait_for_full_window(tap, qtbot)
    controller.set_playback_rate(rate)
    received = tap.received_buffer_count
    qtbot.waitUntil(lambda: tap.received_buffer_count > received + 2, timeout=TIMEOUT_MS)

    left, right = tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
    frame = LevelProcessor().process(left, right, elapsed_seconds=0.033)

    assert frame.left_peak_db > LEVEL_DB_FLOOR


def test_stop_clears_the_stereo_buffers(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """停止で3本のリングバッファが空になる。"""
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    controller.stop()

    qtbot.waitUntil(lambda: tap.available_frame_count == 0, timeout=TIMEOUT_MS)
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0
    assert tap.channel_count == 0


def test_panel_follows_real_playback_for_both_visualizations(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """実再生でスペクトラムとレベルが同時に追従し、pause で静止・stop で消える。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(700, 320)
    window.show()
    qtbot.waitExposed(window)

    play(controller, wav(test_audio_dir))
    qtbot.waitUntil(lambda: panel.level_meter_widget.frame is not None, timeout=TIMEOUT_MS)
    level_frame = panel.level_meter_widget.frame
    assert level_frame is not None
    assert level_frame.left_peak_db > LEVEL_DB_FLOOR
    assert panel.spectrum_widget.frame is not None

    controller.pause()
    qtbot.waitUntil(lambda: not panel.is_timer_active, timeout=TIMEOUT_MS)
    assert panel.level_meter_widget.frame is level_frame

    controller.stop()
    assert panel.level_meter_widget.frame is None

    panel.shutdown()


def test_source_change_discards_the_previous_peak_hold(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """曲切替で前曲の Peak hold を引き継がない。"""
    window = QMainWindow()
    qtbot.addWidget(window)
    panel = SpectrumPanel(controller, tap)
    window.setCentralWidget(panel)
    window.resize(700, 320)
    window.show()
    qtbot.waitExposed(window)

    play(controller, wav(test_audio_dir))
    qtbot.waitUntil(lambda: panel.level_meter_widget.frame is not None, timeout=TIMEOUT_MS)

    controller.load(mp3(test_audio_dir))

    assert panel.level_meter_widget.frame is None
    controller.play()
    qtbot.waitUntil(lambda: panel.level_meter_widget.frame is not None, timeout=TIMEOUT_MS)
    frame = panel.level_meter_widget.frame
    assert frame is not None
    # 新曲の最初のフレームでは hold が現在 Peak と同じ（前曲の hold が残らない）。
    assert frame.left_peak_hold_db == pytest.approx(frame.left_peak_db, abs=1e-6)

    panel.shutdown()


def test_shutdown_is_safe_during_playback(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """再生中の shutdown でも例外なく PCM 受信が止まる。"""
    play(controller, wav(test_audio_dir))
    wait_for_pcm(tap, qtbot)

    tap.shutdown()
    received = tap.received_buffer_count
    qtbot.wait(300)

    assert tap.received_buffer_count == received
    assert tap.available_frame_count == 0
    assert controller.state is PlaybackState.PLAYING


def test_smoothing_recovers_after_a_source_switch(
    controller: PlaybackController, tap: PcmTap, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """曲切替後も processor が新しい PCM から追従を再開できる。"""
    processor = SpectrumProcessor()
    play(controller, wav(test_audio_dir))
    qtbot.waitUntil(lambda: tap.available_frame_count >= FFT_SIZE, timeout=TIMEOUT_MS)
    processor.process(tap.snapshot(FFT_SIZE), tap.sample_rate)

    controller.load(mp3(test_audio_dir))
    processor.reset()
    controller.play()
    qtbot.waitUntil(lambda: tap.available_frame_count >= FFT_SIZE, timeout=TIMEOUT_MS)

    frame = processor.process(tap.snapshot(FFT_SIZE), tap.sample_rate)

    assert frame.band_count == 96
    assert float(frame.levels_db.max()) > -90.0

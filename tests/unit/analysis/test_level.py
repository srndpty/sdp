"""Peak／RMS／dBFS変換とPeak holdの純粋ロジックを検証する。

Qt も音声デバイスも使わない。Peak hold は実時間（秒）で駆動するため、
固定 sleep ではなく明示した経過秒を渡して検証する。
"""

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from sdp.core.analysis.level import (
    LEVEL_DB_FLOOR,
    LEVEL_WINDOW_SIZE,
    PEAK_HOLD_RELEASE_DB_PER_SECOND,
    PEAK_HOLD_SECONDS,
    LevelProcessor,
    StereoLevelFrame,
    amplitude_to_dbfs,
    peak_amplitude,
    rms_amplitude,
    silent_level_frame,
)

SAMPLE_RATE = 48_000


def constant(value: float, size: int = 1_024) -> NDArray[np.float32]:
    return np.full(size, value, dtype=np.float32)


def sine(amplitude: float, frequency_hz: float = 1_000.0, size: int = 4_800) -> NDArray[np.float32]:
    """整数周期ぶんの正弦波（端の切れによるRMS誤差を避ける）。"""
    frames = round(SAMPLE_RATE / frequency_hz) * (size // round(SAMPLE_RATE / frequency_hz))
    t = np.arange(max(frames, 1), dtype=np.float64) / SAMPLE_RATE
    return (np.sin(2.0 * np.pi * frequency_hz * t) * amplitude).astype(np.float32)


def empty() -> NDArray[np.float32]:
    return np.empty(0, dtype=np.float32)


# -- StereoLevelFrame -------------------------------------------------------


def frame_values(
    *,
    left_peak: float = -6.0,
    right_peak: float = -6.0,
    left_rms: float = -12.0,
    right_rms: float = -12.0,
    left_hold: float = -3.0,
    right_hold: float = -3.0,
) -> StereoLevelFrame:
    return StereoLevelFrame(
        left_peak_db=left_peak,
        right_peak_db=right_peak,
        left_rms_db=left_rms,
        right_rms_db=right_rms,
        left_peak_hold_db=left_hold,
        right_peak_hold_db=right_hold,
    )


def test_frame_keeps_the_given_db_values() -> None:
    """dBFSの6値をそのまま保持する。"""
    frame = frame_values(left_peak=-6.0, right_peak=-9.0)

    assert frame.left_peak_db == -6.0
    assert frame.right_peak_db == -9.0
    assert frame.left_rms_db == -12.0
    assert frame.left_peak_hold_db == -3.0


def test_frame_is_immutable() -> None:
    """frozen dataclassのため書き換えられない。"""
    frame = frame_values()

    with pytest.raises(AttributeError):
        frame.left_peak_db = 0.0  # type: ignore[misc]


def test_frame_rejects_bool_as_a_number() -> None:
    """boolを数値として受理しない。"""
    with pytest.raises(TypeError):
        frame_values(left_peak=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_frame_rejects_non_finite_values(value: float) -> None:
    """NaN／infを保持しない。"""
    with pytest.raises(ValueError, match="有限"):
        frame_values(left_rms=value)


@pytest.mark.parametrize("value", [0.5, -90.5, -1_000.0])
def test_frame_rejects_values_outside_the_display_range(value: float) -> None:
    """floor未満と0dB超は受け付けない。"""
    with pytest.raises(ValueError, match="dB以上"):
        frame_values(left_peak=value, left_rms=min(value, -12.0), left_hold=max(value, -3.0))


def test_frame_rejects_rms_above_peak() -> None:
    """RMSがPeakを超えるフレームは作れない。"""
    with pytest.raises(ValueError, match="RMS"):
        frame_values(left_peak=-20.0, left_rms=-10.0, left_hold=-3.0)


def test_frame_rejects_peak_hold_below_peak() -> None:
    """Peak holdがPeakを下回るフレームは作れない。"""
    with pytest.raises(ValueError, match="Peak hold"):
        frame_values(right_peak=-6.0, right_hold=-20.0)


def test_frame_allows_equal_values_at_the_floor() -> None:
    """無音では3値すべてがfloorで等しくなる。"""
    frame = silent_level_frame()

    assert frame.left_peak_db == LEVEL_DB_FLOOR
    assert frame.right_rms_db == LEVEL_DB_FLOOR
    assert frame.left_peak_hold_db == LEVEL_DB_FLOOR


def test_frame_does_not_hold_arrays_or_colors() -> None:
    """描画情報や時刻オブジェクトを持たない。"""
    frame = frame_values()

    for value in (
        frame.left_peak_db,
        frame.right_peak_db,
        frame.left_rms_db,
        frame.right_rms_db,
        frame.left_peak_hold_db,
        frame.right_peak_hold_db,
    ):
        assert isinstance(value, float)


# -- Peak -------------------------------------------------------------------


def test_silence_peak_is_the_floor() -> None:
    """無音のPeakは0（dBFSではfloor）。"""
    assert peak_amplitude(constant(0.0)) == 0.0
    assert amplitude_to_dbfs(peak_amplitude(constant(0.0))) == LEVEL_DB_FLOOR


def test_full_scale_constant_peak_is_zero_db() -> None:
    """定値1.0のPeakは0dB。"""
    assert peak_amplitude(constant(1.0)) == pytest.approx(1.0)
    assert amplitude_to_dbfs(peak_amplitude(constant(1.0))) == pytest.approx(0.0)


def test_half_scale_is_about_minus_six_db() -> None:
    """定値0.5は約-6.02dB。"""
    assert amplitude_to_dbfs(peak_amplitude(constant(0.5))) == pytest.approx(-6.02, abs=0.01)


def test_peak_uses_the_absolute_value() -> None:
    """負側の振幅もPeakへ反映する。"""
    samples = np.array([0.1, -0.8, 0.2], dtype=np.float32)

    assert peak_amplitude(samples) == pytest.approx(0.8)


def test_full_scale_sine_peak_is_about_zero_db() -> None:
    """振幅1.0の正弦波のPeakは約0dB。"""
    assert amplitude_to_dbfs(peak_amplitude(sine(1.0))) == pytest.approx(0.0, abs=0.01)


def test_empty_input_peak_is_zero() -> None:
    """空入力のPeakは0とする（失敗にしない）。"""
    assert peak_amplitude(empty()) == 0.0


# -- RMS --------------------------------------------------------------------


def test_silence_rms_is_the_floor() -> None:
    """無音のRMSは0（dBFSではfloor）。"""
    assert rms_amplitude(constant(0.0)) == 0.0
    assert amplitude_to_dbfs(rms_amplitude(constant(0.0))) == LEVEL_DB_FLOOR


def test_full_scale_constant_rms_is_zero_db() -> None:
    """定値1.0のRMSは0dB。"""
    assert amplitude_to_dbfs(rms_amplitude(constant(1.0))) == pytest.approx(0.0)


def test_half_scale_constant_rms_is_about_minus_six_db() -> None:
    """定値0.5のRMSは約-6.02dB。"""
    assert amplitude_to_dbfs(rms_amplitude(constant(0.5))) == pytest.approx(-6.02, abs=0.01)


def test_full_scale_sine_rms_is_about_minus_three_db() -> None:
    """振幅1.0の正弦波のRMSは約-3.01dB。"""
    assert amplitude_to_dbfs(rms_amplitude(sine(1.0))) == pytest.approx(-3.01, abs=0.05)


def test_half_scale_sine_rms_is_about_minus_nine_db() -> None:
    """振幅0.5の正弦波のRMSは約-9.03dB。"""
    assert amplitude_to_dbfs(rms_amplitude(sine(0.5))) == pytest.approx(-9.03, abs=0.05)


def test_empty_input_rms_is_zero() -> None:
    """空入力のRMSは0とする。"""
    assert rms_amplitude(empty()) == 0.0


def test_rms_does_not_exceed_peak() -> None:
    """RMSは常にPeak以下になる。"""
    for samples in (sine(1.0), sine(0.25), constant(0.75), np.array([0.0, 1.0], dtype=np.float32)):
        assert rms_amplitude(samples) <= peak_amplitude(samples) + 1e-9


def test_rms_of_many_full_scale_samples_does_not_overflow() -> None:
    """float32の二乗和で精度低下・overflowを起こさない（float64へ昇格）。"""
    samples = constant(1.0, size=2_000_000)

    assert rms_amplitude(samples) == pytest.approx(1.0)


def test_level_functions_do_not_modify_the_input() -> None:
    """入力配列を変更しない（read-onlyのsnapshotをそのまま渡せる）。"""
    samples = sine(0.5)
    read_only = samples.copy()
    read_only.setflags(write=False)
    expected = samples.copy()

    peak_amplitude(read_only)
    rms_amplitude(read_only)

    np.testing.assert_array_equal(read_only, expected)


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_non_finite_samples_are_rejected(value: float) -> None:
    """NaN／infを含むPCMは明示的に失敗させる（floorへ黙って丸めない）。"""
    samples = np.array([0.1, value], dtype=np.float32)

    with pytest.raises(ValueError, match="NaN"):
        peak_amplitude(samples)
    with pytest.raises(ValueError, match="NaN"):
        rms_amplitude(samples)


def test_wrong_dtype_and_shape_are_rejected() -> None:
    """dtype float32・1次元以外は受け付けない。"""
    with pytest.raises(TypeError):
        peak_amplitude(np.zeros(4, dtype=np.float64))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="1次元"):
        rms_amplitude(np.zeros((2, 2), dtype=np.float32))  # type: ignore[arg-type]


# -- dBFS 変換 ---------------------------------------------------------------


def test_clipping_amplitude_is_clamped_to_zero_db() -> None:
    """1.0を超える振幅も0dBへclampする。"""
    assert amplitude_to_dbfs(2.5) == 0.0


def test_zero_amplitude_is_clamped_to_the_floor() -> None:
    """0はepsilon経由でfloorへclampする（log(0)を作らない）。"""
    assert amplitude_to_dbfs(0.0) == LEVEL_DB_FLOOR


def test_custom_floor_is_respected() -> None:
    """floorを上げると、それ以下がfloorへ寄る。"""
    assert amplitude_to_dbfs(0.0001, db_floor=-60.0) == -60.0


def test_non_finite_amplitude_is_rejected() -> None:
    """非有限の振幅は受け付けない。"""
    with pytest.raises(ValueError, match="有限"):
        amplitude_to_dbfs(math.nan)


# -- LevelProcessor ---------------------------------------------------------


def test_processor_defaults_match_the_module_constants() -> None:
    """既定値は定数1か所へ集約されている。"""
    processor = LevelProcessor()

    assert processor.db_floor == LEVEL_DB_FLOOR
    assert processor.hold_seconds == PEAK_HOLD_SECONDS
    assert processor.release_db_per_second == PEAK_HOLD_RELEASE_DB_PER_SECOND
    assert processor.window_size == LEVEL_WINDOW_SIZE


def test_processor_reports_both_channels_independently() -> None:
    """左右で振幅が違えば別の値になる。"""
    processor = LevelProcessor()

    frame = processor.process(constant(1.0), constant(0.5), elapsed_seconds=0.0)

    assert frame.left_peak_db == pytest.approx(0.0)
    assert frame.right_peak_db == pytest.approx(-6.02, abs=0.01)
    assert frame.left_rms_db == pytest.approx(0.0)
    assert frame.right_rms_db == pytest.approx(-6.02, abs=0.01)


def test_processor_handles_silence_and_empty_input() -> None:
    """無音・空入力でもfloorのフレームを返す。"""
    processor = LevelProcessor()

    silence = processor.process(constant(0.0), constant(0.0), elapsed_seconds=0.0)
    nothing = processor.process(empty(), empty(), elapsed_seconds=0.0)

    assert silence == silent_level_frame()
    assert nothing == silent_level_frame()


def test_peak_hold_rises_immediately() -> None:
    """新しいPeakがholdより高ければ即時に追従する。"""
    processor = LevelProcessor()

    processor.process(constant(0.25), constant(0.25), elapsed_seconds=0.0)
    frame = processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.033)

    assert frame.left_peak_hold_db == pytest.approx(0.0)
    assert frame.right_peak_hold_db == pytest.approx(0.0)


def test_peak_hold_is_kept_during_the_hold_time() -> None:
    """保持時間内は減衰しない。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    frame = processor.process(constant(0.0), constant(0.0), elapsed_seconds=PEAK_HOLD_SECONDS * 0.9)

    assert frame.left_peak_hold_db == pytest.approx(0.0)
    assert frame.left_peak_db == LEVEL_DB_FLOOR


def test_peak_hold_decays_after_the_hold_time() -> None:
    """保持時間を過ぎた分だけ減衰する。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    frame = processor.process(constant(0.0), constant(0.0), elapsed_seconds=PEAK_HOLD_SECONDS + 0.5)

    expected = -PEAK_HOLD_RELEASE_DB_PER_SECOND * 0.5
    assert frame.left_peak_hold_db == pytest.approx(expected)
    assert frame.right_peak_hold_db == pytest.approx(expected)


def test_peak_hold_decay_depends_on_elapsed_time_not_tick_count() -> None:
    """同じ合計経過秒なら、tick数が違っても同じ結果になる。"""
    single = LevelProcessor()
    divided = LevelProcessor()
    single.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)
    divided.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    once = single.process(constant(0.0), constant(0.0), elapsed_seconds=2.0)
    many = divided.process(constant(0.0), constant(0.0), elapsed_seconds=0.1)
    for _ in range(19):
        many = divided.process(constant(0.0), constant(0.0), elapsed_seconds=0.1)

    assert once.left_peak_hold_db == pytest.approx(many.left_peak_hold_db, abs=1e-9)
    assert once.left_peak_hold_db == pytest.approx(-PEAK_HOLD_RELEASE_DB_PER_SECOND * 1.0)


def test_peak_hold_does_not_fall_below_the_current_peak() -> None:
    """減衰中も現在Peakより下へは落ちない。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    frame = processor.process(constant(0.5), constant(0.5), elapsed_seconds=10.0)

    assert frame.left_peak_hold_db == pytest.approx(frame.left_peak_db)
    assert frame.left_peak_hold_db == pytest.approx(-6.02, abs=0.01)


def test_peak_hold_does_not_fall_below_the_floor() -> None:
    """極端に大きいelapsedでもfloor未満へ行かない。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    frame = processor.process(constant(0.0), constant(0.0), elapsed_seconds=10_000.0)

    assert frame.left_peak_hold_db == LEVEL_DB_FLOOR
    assert frame.right_peak_hold_db == LEVEL_DB_FLOOR


def test_peak_hold_decays_per_channel_independently() -> None:
    """片chのPeak更新が他chのholdを延命しない。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    frame = processor.process(constant(1.0), constant(0.0), elapsed_seconds=PEAK_HOLD_SECONDS + 0.5)

    assert frame.left_peak_hold_db == pytest.approx(0.0)
    assert frame.right_peak_hold_db == pytest.approx(-PEAK_HOLD_RELEASE_DB_PER_SECOND * 0.5)


def test_not_processing_keeps_the_previous_hold() -> None:
    """pause相当（processを呼ばない）ではholdの時間が進まない。"""
    processor = LevelProcessor()
    before = processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    # 何度読み出しても状態は変わらない（processだけが時間を進める）。
    after = processor.process(constant(0.0), constant(0.0), elapsed_seconds=0.0)

    assert before.left_peak_hold_db == pytest.approx(0.0)
    assert after.left_peak_hold_db == pytest.approx(0.0)


def test_reset_discards_the_hold() -> None:
    """resetでPeak holdと経過時間を捨てる。"""
    processor = LevelProcessor()
    processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    processor.reset()
    frame = processor.process(constant(0.0), constant(0.0), elapsed_seconds=0.0)

    assert frame == silent_level_frame()


@pytest.mark.parametrize("elapsed", [-0.001, -1.0, math.nan, math.inf])
def test_invalid_elapsed_is_rejected(elapsed: float) -> None:
    """負・非有限の経過秒は受け付けない。"""
    processor = LevelProcessor()

    with pytest.raises(ValueError, match="elapsed_seconds"):
        processor.process(constant(0.0), constant(0.0), elapsed_seconds=elapsed)


def test_bool_elapsed_is_rejected() -> None:
    """boolを経過秒として受理しない。"""
    processor = LevelProcessor()

    with pytest.raises(TypeError):
        processor.process(constant(0.0), constant(0.0), elapsed_seconds=True)  # type: ignore[arg-type]


def test_processor_rejects_an_invalid_configuration() -> None:
    """floor・保持時間・減衰速度の不正値を受け付けない。"""
    with pytest.raises(ValueError):
        LevelProcessor(db_floor=0.0)
    with pytest.raises(ValueError):
        LevelProcessor(db_floor=-120.0)
    with pytest.raises(ValueError):
        LevelProcessor(hold_seconds=-1.0)
    with pytest.raises(ValueError):
        LevelProcessor(release_db_per_second=0.0)


def test_processor_respects_a_custom_floor() -> None:
    """floorを上げると無音がそのfloorになる。"""
    processor = LevelProcessor(db_floor=-60.0)

    frame = processor.process(constant(0.0), constant(0.0), elapsed_seconds=0.0)

    assert frame.left_peak_db == -60.0
    assert frame == silent_level_frame(-60.0)


def test_clipping_input_stays_at_zero_db() -> None:
    """clippingしたPCMでも0dBを超えない。"""
    processor = LevelProcessor()

    frame = processor.process(constant(1.0), constant(1.0), elapsed_seconds=0.0)

    assert frame.left_peak_db == pytest.approx(0.0)
    assert frame.left_rms_db == pytest.approx(0.0)
    assert frame.left_peak_hold_db == pytest.approx(0.0)


def test_processor_keeps_rms_below_peak_for_real_signals() -> None:
    """実信号ではRMSがPeakを下回る。"""
    processor = LevelProcessor()

    frame = processor.process(sine(1.0), sine(0.5), elapsed_seconds=0.0)

    assert frame.left_rms_db < frame.left_peak_db
    assert frame.right_rms_db < frame.right_peak_db


def test_processor_does_not_modify_its_inputs() -> None:
    """processは入力配列を変更しない。"""
    processor = LevelProcessor()
    left = sine(0.5)
    right = sine(0.25)
    expected_left = left.copy()
    expected_right = right.copy()

    processor.process(left, right, elapsed_seconds=0.0)

    np.testing.assert_array_equal(left, expected_left)
    np.testing.assert_array_equal(right, expected_right)

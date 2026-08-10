"""stereo.py のベクトルスコープと位相相関の数値契約を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.stereo import (
    VECTORSCOPE_MAX_POINTS,
    CorrelationProcessor,
    VectorscopeFrame,
    compute_vectorscope,
    empty_vectorscope_frame,
    phase_correlation,
)

SAMPLE_RATE = 48_000


def _tone(frequency: float, frames: int = 4_096, phase: float = 0.0) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.5 * np.sin(2.0 * np.pi * frequency * t + phase)).astype(np.float32)


# -- ベクトルスコープ -------------------------------------------------------


def test_empty_input_returns_empty_frame() -> None:
    """空入力では点を持たない空フレームを返す。"""
    frame = compute_vectorscope(np.empty(0, dtype=np.float32), np.empty(0, dtype=np.float32))
    assert frame.point_count == 0


def test_mono_signal_collapses_to_a_vertical_line() -> None:
    """モノラル（L==R）ではx≈0の縦線になる。"""
    tone = _tone(1_000.0)
    frame = compute_vectorscope(tone, tone)
    assert np.allclose(frame.x, 0.0, atol=1e-6)
    assert np.any(np.abs(frame.y) > 0.1)


def test_out_of_phase_signal_spreads_horizontally() -> None:
    """逆相（L==-R）ではy≈0の横線になる。"""
    tone = _tone(1_000.0)
    frame = compute_vectorscope(tone, -tone)
    assert np.allclose(frame.y, 0.0, atol=1e-6)
    assert np.any(np.abs(frame.x) > 0.1)


def test_points_are_decimated_to_the_maximum() -> None:
    """点数の上限を超える入力は間引かれる。"""
    left = _tone(440.0, frames=VECTORSCOPE_MAX_POINTS * 4)
    right = _tone(441.0, frames=VECTORSCOPE_MAX_POINTS * 4)
    frame = compute_vectorscope(left, right)
    assert 0 < frame.point_count <= VECTORSCOPE_MAX_POINTS


def test_values_stay_within_unit_range() -> None:
    """フルスケール入力でも結果は-1〜1へ収まる。"""
    left = np.ones(64, dtype=np.float32)
    right = -np.ones(64, dtype=np.float32)
    frame = compute_vectorscope(left, right)
    assert float(frame.x.max()) <= 1.0
    assert float(frame.x.min()) >= -1.0


def test_mismatched_shapes_are_rejected() -> None:
    """L／Rの長さが違う場合は失敗する。"""
    with pytest.raises(ValueError, match="shape"):
        compute_vectorscope(np.zeros(4, dtype=np.float32), np.zeros(5, dtype=np.float32))


def test_frame_arrays_are_read_only_copies() -> None:
    """フレームの配列はread-onlyで、入力とメモリを共有しない。"""
    left = _tone(1_000.0, frames=128)
    frame = compute_vectorscope(left, left)
    assert not frame.x.flags.writeable
    assert not frame.y.flags.writeable


def test_empty_frame_helper() -> None:
    """明示的な空フレームは長さ0。"""
    assert empty_vectorscope_frame().point_count == 0


def test_vectorscope_frame_rejects_mismatched_axes() -> None:
    """VectorscopeFrameはx/yのshape不一致を拒否する。"""
    with pytest.raises(ValueError, match="shape"):
        VectorscopeFrame(
            x=np.zeros(3, dtype=np.float32),
            y=np.zeros(4, dtype=np.float32),
        )


# -- 位相相関 ---------------------------------------------------------------


def test_identical_channels_correlate_to_one() -> None:
    """同一チャンネルは相関+1。"""
    tone = _tone(1_000.0)
    assert phase_correlation(tone, tone) == pytest.approx(1.0, abs=1e-6)


def test_inverted_channels_correlate_to_minus_one() -> None:
    """逆相は相関-1。"""
    tone = _tone(1_000.0)
    assert phase_correlation(tone, -tone) == pytest.approx(-1.0, abs=1e-6)


def test_silent_channel_returns_zero() -> None:
    """片チャンネルが無音なら相関は0（未定義を0とする）。"""
    tone = _tone(1_000.0)
    assert phase_correlation(tone, np.zeros_like(tone)) == 0.0


def test_empty_input_correlates_to_zero() -> None:
    """空入力は相関0。"""
    empty = np.empty(0, dtype=np.float32)
    assert phase_correlation(empty, empty) == 0.0


def test_correlation_is_clamped_to_range() -> None:
    """相関は-1〜1へ収まる。"""
    left = np.array([1.0, -1.0, 1.0, -1.0], dtype=np.float32)
    right = np.array([1.0, 1.0, -1.0, -1.0], dtype=np.float32)
    value = phase_correlation(left, right)
    assert -1.0 <= value <= 1.0


# -- CorrelationProcessor ---------------------------------------------------


def test_processor_first_frame_uses_raw_value() -> None:
    """初回は生の相関値を返す。"""
    processor = CorrelationProcessor()
    tone = _tone(1_000.0)
    assert processor.process(tone, tone) == pytest.approx(1.0, abs=1e-6)


def test_processor_smooths_towards_new_values() -> None:
    """2フレーム目以降は平滑化され、いきなり飛ばない。"""
    processor = CorrelationProcessor()
    tone = _tone(1_000.0)
    processor.process(tone, tone)  # +1 から開始
    smoothed = processor.process(tone, -tone)  # -1 方向へ
    assert -1.0 < smoothed < 1.0


def test_processor_reset_forgets_history() -> None:
    """resetで履歴を捨て、次フレームは生値から始まる。"""
    processor = CorrelationProcessor()
    tone = _tone(1_000.0)
    processor.process(tone, tone)
    processor.reset()
    assert processor.process(tone, -tone) == pytest.approx(-1.0, abs=1e-6)


def test_processor_window_size_is_exposed() -> None:
    """窓長を公開する（Panelがsnapshot長に使う）。"""
    assert CorrelationProcessor().window_size > 0


@pytest.mark.parametrize("value", [0.0, 1.5, float("nan"), True])
def test_invalid_coefficient_is_rejected(value: float) -> None:
    """平滑化係数は0より大きく1以下に限る。"""
    with pytest.raises(ValueError, match="attack"):
        CorrelationProcessor(attack=value)

"""oscilloscope.py のトリガー整列と固定shape契約を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.oscilloscope import (
    OSCILLOSCOPE_WINDOW,
    OscilloscopeFrame,
    compute_oscilloscope,
    silent_oscilloscope_frame,
)

SAMPLE_RATE = 48_000


def _tone(frequency: float, frames: int) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.8 * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def test_empty_input_returns_silent_frame() -> None:
    """空入力では全0の無音フレームを返す。"""
    frame = compute_oscilloscope(np.empty(0, dtype=np.float32))
    assert frame.sample_count == OSCILLOSCOPE_WINDOW
    assert np.all(frame.samples == 0.0)


def test_output_length_matches_the_window() -> None:
    """出力長は要求した窓長で固定。"""
    samples = _tone(1_000.0, frames=OSCILLOSCOPE_WINDOW * 3)
    frame = compute_oscilloscope(samples, window=512)
    assert frame.sample_count == 512


def test_short_input_is_left_padded() -> None:
    """窓長に満たない入力は左を0で埋める。"""
    samples = _tone(1_000.0, frames=100)
    frame = compute_oscilloscope(samples, window=512)
    assert frame.sample_count == 512
    assert np.all(frame.samples[: 512 - 100] == 0.0)


def test_trigger_starts_on_a_rising_zero_crossing() -> None:
    """トリガー整列後の先頭サンプルは概ね0から立ち上がる。"""
    samples = _tone(1_000.0, frames=OSCILLOSCOPE_WINDOW * 3)
    frame = compute_oscilloscope(samples, window=OSCILLOSCOPE_WINDOW)
    # 立ち上がりトリガーなら先頭は0付近で、直後は正へ向かう。
    assert abs(float(frame.samples[0])) < 0.1
    assert float(frame.samples[1]) >= float(frame.samples[0])


def test_trigger_is_stable_across_shifts() -> None:
    """入力が数サンプルずれても、整列後の波形はほぼ一致する。"""
    base = _tone(500.0, frames=OSCILLOSCOPE_WINDOW * 3)
    shifted = _tone(500.0, frames=OSCILLOSCOPE_WINDOW * 3 + 17)[17:]
    first = compute_oscilloscope(base, window=1_024).samples
    second = compute_oscilloscope(shifted, window=1_024).samples
    assert np.allclose(first, second, atol=0.05)


def test_values_are_preserved_from_the_input() -> None:
    """DC除去やスケーリングをせず、入力値をそのまま表示する。"""
    samples = _tone(1_000.0, frames=4_096)
    frame = compute_oscilloscope(samples, window=256)
    assert float(np.abs(frame.samples).max()) == pytest.approx(
        float(np.abs(samples).max()), abs=0.05
    )


def test_frame_is_read_only_copy() -> None:
    """フレームの配列はread-only。"""
    samples = _tone(1_000.0, frames=4_096)
    frame = compute_oscilloscope(samples, window=256)
    assert not frame.samples.flags.writeable


def test_silent_frame_helper() -> None:
    """無音フレームは全0で窓長ぶん。"""
    frame = silent_oscilloscope_frame(128)
    assert frame.sample_count == 128
    assert np.all(frame.samples == 0.0)


@pytest.mark.parametrize("window", [0, -1])
def test_invalid_window_is_rejected(window: int) -> None:
    """窓長は1以上に限る。"""
    with pytest.raises(ValueError, match="window"):
        compute_oscilloscope(_tone(1_000.0, frames=256), window=window)


def test_frame_rejects_non_finite_values() -> None:
    """NaNを含む配列は受け付けない。"""
    bad = np.array([0.0, float("nan"), 0.0], dtype=np.float32)
    with pytest.raises(ValueError, match="NaN"):
        OscilloscopeFrame(samples=bad)

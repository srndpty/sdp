"""chroma.py のピッチクラス集約と正規化・平滑化を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.chroma import (
    CHROMA_CLASS_COUNT,
    PITCH_CLASS_NAMES,
    ChromaProcessor,
    compute_chroma,
    empty_chroma_frame,
)

SAMPLE_RATE = 48_000

# 平均律のA4=440Hzを基準にした主要音の周波数。
_A4_HZ = 440.0
_C4_HZ = 261.63
_E4_HZ = 329.63
_G4_HZ = 392.0

_A_CLASS = 9
_C_CLASS = 0


def _tone(frequency: float, frames: int = 8_192) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.5 * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def test_note_names_cover_twelve_classes() -> None:
    """音名は12個そろっている。"""
    assert len(PITCH_CLASS_NAMES) == CHROMA_CLASS_COUNT


def test_silence_returns_empty_frame() -> None:
    """無音では全0のフレーム。"""
    frame = compute_chroma(np.zeros(8_192, dtype=np.float32), SAMPLE_RATE)
    assert np.all(frame.values == 0.0)


def test_a4_tone_peaks_on_the_a_class() -> None:
    """A4の純音はAのクラスが最大になる。"""
    frame = compute_chroma(_tone(_A4_HZ), SAMPLE_RATE)
    assert int(np.argmax(frame.values)) == _A_CLASS
    assert frame.values[_A_CLASS] == pytest.approx(1.0)


def test_c4_tone_peaks_on_the_c_class() -> None:
    """C4の純音はCのクラスが最大になる。"""
    frame = compute_chroma(_tone(_C4_HZ), SAMPLE_RATE)
    assert int(np.argmax(frame.values)) == _C_CLASS


def test_values_are_normalized_to_unit_range() -> None:
    """強度は0〜1へ正規化される。"""
    frame = compute_chroma(_tone(_G4_HZ), SAMPLE_RATE)
    assert float(frame.values.max()) == pytest.approx(1.0)
    assert float(frame.values.min()) >= 0.0


def test_chord_lights_multiple_classes() -> None:
    """和音（C+E+G）では複数のクラスが立つ。"""
    mix = (_tone(_C4_HZ) + _tone(_E4_HZ) + _tone(_G4_HZ)).astype(np.float32)
    mix /= float(np.abs(mix).max())
    frame = compute_chroma(mix, SAMPLE_RATE)
    strong = int(np.sum(frame.values > 0.3))
    assert strong >= 3


def test_frame_is_read_only() -> None:
    """フレームの配列はread-only。"""
    frame = compute_chroma(_tone(_A4_HZ), SAMPLE_RATE)
    assert not frame.values.flags.writeable


def test_empty_frame_helper() -> None:
    """空フレームは長さ12の全0。"""
    frame = empty_chroma_frame()
    assert frame.values.size == CHROMA_CLASS_COUNT
    assert np.all(frame.values == 0.0)


def test_invalid_sample_rate_is_rejected() -> None:
    """sample rateは1以上に限る。"""
    with pytest.raises(ValueError, match="sample_rate"):
        compute_chroma(_tone(_A4_HZ), 0)


# -- ChromaProcessor --------------------------------------------------------


def test_processor_first_frame_matches_raw() -> None:
    """初回は生のクロマ値を返す。"""
    processor = ChromaProcessor()
    frame = processor.process(_tone(_A4_HZ), SAMPLE_RATE)
    assert int(np.argmax(frame.values)) == _A_CLASS


def test_processor_reset_forgets_sample_rate() -> None:
    """resetでsample rate履歴を捨てる。"""
    processor = ChromaProcessor()
    processor.process(_tone(_A4_HZ), SAMPLE_RATE)
    processor.reset()
    assert processor.sample_rate is None


def test_processor_resets_on_sample_rate_change() -> None:
    """sample rate変更で平滑化履歴を作り直す。"""
    processor = ChromaProcessor()
    processor.process(_tone(_A4_HZ), SAMPLE_RATE)
    processor.process(_tone(_A4_HZ), 44_100)
    assert processor.sample_rate == 44_100


def test_processor_values_stay_in_range() -> None:
    """平滑化後も0〜1に収まる。"""
    processor = ChromaProcessor()
    frame = processor.process(_tone(_A4_HZ), SAMPLE_RATE)
    for _ in range(5):
        frame = processor.process(_tone(_A4_HZ), SAMPLE_RATE)
    assert float(frame.values.max()) <= 1.0
    assert float(frame.values.min()) >= 0.0

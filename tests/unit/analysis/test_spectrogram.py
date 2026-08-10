"""spectrogram.py の履歴リングと固定shape契約を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.spectrogram import (
    SPECTROGRAM_HISTORY,
    SpectrogramFrame,
    SpectrogramProcessor,
)
from sdp.core.analysis.spectrum import SPECTRUM_BAND_COUNT, SPECTRUM_DB_FLOOR

SAMPLE_RATE = 48_000


def _tone(frequency: float, frames: int = 4_096) -> np.ndarray:
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.5 * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def test_frame_shape_is_history_by_bands() -> None:
    """フレームは (history, band_count) の形。"""
    processor = SpectrogramProcessor()
    frame = processor.process(_tone(1_000.0), SAMPLE_RATE)
    assert frame.history == SPECTROGRAM_HISTORY
    assert frame.band_count == SPECTRUM_BAND_COUNT


def test_initial_history_is_floor() -> None:
    """最初の1列以外はfloorで埋まっている。"""
    processor = SpectrogramProcessor(history=8)
    frame = processor.process(_tone(1_000.0), SAMPLE_RATE)
    # 右端が最新、それ以外はfloor。
    assert np.all(frame.columns[:-1] == SPECTRUM_DB_FLOOR)


def test_newest_column_is_on_the_right() -> None:
    """最新の列は右端（最終行）に置かれる。"""
    processor = SpectrogramProcessor(history=4)
    processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)
    frame = processor.process(_tone(1_000.0), SAMPLE_RATE)
    newest = frame.columns[-1]
    assert float(newest.max()) > SPECTRUM_DB_FLOOR


def test_history_scrolls_left() -> None:
    """新しい列を積むと古い列が左へ流れる。"""
    processor = SpectrogramProcessor(history=3)
    processor.process(_tone(1_000.0), SAMPLE_RATE)
    first_newest = processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE).columns[-1]
    second = processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)
    # 1つ前の最新列が、1つ左（-2行目）へ移動している。
    assert np.allclose(second.columns[-2], first_newest)


def test_reset_clears_history_to_floor() -> None:
    """resetで全列をfloorへ戻す。"""
    processor = SpectrogramProcessor(history=4)
    processor.process(_tone(1_000.0), SAMPLE_RATE)
    processor.reset()
    frame = processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)
    assert np.all(frame.columns[:-1] == SPECTRUM_DB_FLOOR)
    assert processor.sample_rate == SAMPLE_RATE


def test_sample_rate_change_resets_history() -> None:
    """sample rate変更で履歴を捨てる。"""
    processor = SpectrogramProcessor(history=4)
    processor.process(_tone(1_000.0), SAMPLE_RATE)
    frame = processor.process(_tone(1_000.0), 44_100)
    assert processor.sample_rate == 44_100
    assert np.all(frame.columns[:-1] == SPECTRUM_DB_FLOOR)


def test_frame_columns_are_read_only() -> None:
    """フレームの列はread-only。"""
    frame = SpectrogramProcessor(history=4).process(_tone(1_000.0), SAMPLE_RATE)
    assert not frame.columns.flags.writeable


def test_frame_within_floor_and_zero() -> None:
    """dB値はfloor以上0以下に収まる。"""
    frame = SpectrogramProcessor(history=4).process(_tone(1_000.0), SAMPLE_RATE)
    assert float(frame.columns.min()) >= SPECTRUM_DB_FLOOR
    assert float(frame.columns.max()) <= 0.0


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"history": 0}, "history"),
        ({"band_count": 0}, "band_count"),
        ({"db_floor": 1.0}, "db_floor"),
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object], match: str) -> None:
    """不正な構成値は失敗させる。"""
    with pytest.raises(ValueError, match=match):
        SpectrogramProcessor(**kwargs)  # pyright: ignore[reportArgumentType]


def test_low_sample_rate_without_bands_uses_floor() -> None:
    """有効帯域が無い低sample rateでも列をfloorで埋めて落ちない。"""
    processor = SpectrogramProcessor(history=4)
    frame = processor.process(_tone(20.0, frames=256), 100)
    assert frame.columns.shape[1] == SPECTRUM_BAND_COUNT


def test_frame_rejects_one_dimensional_columns() -> None:
    """SpectrogramFrameは1次元配列を拒否する。"""
    with pytest.raises(ValueError, match="2次元"):
        SpectrogramFrame(columns=np.zeros(4, dtype=np.float32), db_floor=SPECTRUM_DB_FLOOR)

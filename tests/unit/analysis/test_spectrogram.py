"""spectrogram.py の履歴リングと固定shape契約を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.spectrogram import (
    CELL_LEVEL_MAX,
    SPECTROGRAM_HISTORY,
    SpectrogramFrame,
    SpectrogramProcessor,
    spectrogram_cells,
)
from sdp.core.analysis.spectrum import (
    SPECTRUM_BAND_COUNT,
    SPECTRUM_DB_FLOOR,
    compute_frequency_analysis,
)

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


def test_frame_does_not_share_memory_with_the_processor_ring() -> None:
    """返したフレームは、その後の書き込みで書き換わらない。"""
    processor = SpectrogramProcessor(history=4)
    first = processor.process(_tone(1_000.0), SAMPLE_RATE)
    snapshot = first.columns.copy()

    for _ in range(6):
        processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)

    assert np.array_equal(first.columns, snapshot)


def test_history_wraps_without_shifting_the_whole_matrix() -> None:
    """リングを1周しても、最新列が右端・古い列が左という並びを保つ。"""
    processor = SpectrogramProcessor(history=3)
    for _ in range(5):
        processor.process(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)
    frame = processor.process(_tone(1_000.0), SAMPLE_RATE)

    assert float(frame.columns[-1].max()) > SPECTRUM_DB_FLOOR
    assert np.all(frame.columns[:-1] == SPECTRUM_DB_FLOOR)


def test_shared_analysis_skips_the_fft(monkeypatch: pytest.MonkeyPatch) -> None:
    """共有結果を渡した列の生成はrFFTを実行しない。"""
    samples = _tone(1_000.0)
    analysis = compute_frequency_analysis(samples, SAMPLE_RATE)
    calls: list[int] = []

    def explode(*args: object, **kwargs: object) -> object:
        calls.append(1)
        raise AssertionError("共有結果があるときにrFFTしてはならない")

    monkeypatch.setattr(np.fft, "rfft", explode)
    frame = SpectrogramProcessor(history=4).process(samples, SAMPLE_RATE, analysis=analysis)

    assert calls == []
    assert float(frame.columns[-1].max()) > SPECTRUM_DB_FLOOR


# -- 描画用セルへの間引き ---------------------------------------------------


def test_cells_are_capped_by_the_available_resolution() -> None:
    """表示解像度が履歴・band数を超えても、データ以上に細かくしない。"""
    frame = SpectrogramProcessor(history=8).process(_tone(1_000.0), SAMPLE_RATE)
    cells = spectrogram_cells(frame, column_count=1_920, row_count=1_080)

    assert cells.columns == 8
    assert cells.rows == SPECTRUM_BAND_COUNT


def test_cells_downsample_to_the_requested_size() -> None:
    """要求が履歴より粗い場合はその解像度へ間引く。"""
    processor = SpectrogramProcessor(history=64)
    for _ in range(64):
        processor.process(_tone(1_000.0), SAMPLE_RATE)
    cells = spectrogram_cells(
        processor.process(_tone(1_000.0), SAMPLE_RATE), column_count=16, row_count=12
    )

    assert cells.rows == 12
    assert cells.columns == 16
    assert cells.painted_count == int(np.count_nonzero(cells.indices))


def test_floor_only_history_paints_nothing() -> None:
    """floorだけの履歴は1セルも塗らない（背景のまま）。"""
    frame = SpectrogramFrame(
        columns=np.full((4, SPECTRUM_BAND_COUNT), SPECTRUM_DB_FLOOR, dtype=np.float32),
        db_floor=SPECTRUM_DB_FLOOR,
    )
    cells = spectrogram_cells(frame, column_count=4, row_count=4)

    assert cells.painted_count == 0
    assert int(cells.indices.max()) == 0


def test_loud_history_reaches_the_maximum_level() -> None:
    """0dBの履歴は最大強度になる。"""
    frame = SpectrogramFrame(
        columns=np.zeros((4, SPECTRUM_BAND_COUNT), dtype=np.float32),
        db_floor=SPECTRUM_DB_FLOOR,
    )
    cells = spectrogram_cells(frame, column_count=4, row_count=4)

    assert int(cells.indices.min()) == CELL_LEVEL_MAX


def test_cells_put_high_frequencies_on_top_and_the_newest_on_the_right() -> None:
    """行0が高域、最終列が最新になる。"""
    columns = np.full((2, 4), SPECTRUM_DB_FLOOR, dtype=np.float32)
    columns[-1, -1] = 0.0  # 最新列の最高域だけ強い
    frame = SpectrogramFrame(columns=columns, db_floor=SPECTRUM_DB_FLOOR)

    cells = spectrogram_cells(frame, column_count=2, row_count=4)

    assert int(cells.indices[0, cells.columns - 1]) == CELL_LEVEL_MAX
    assert cells.painted_count == 1


def test_cell_rows_are_padded_to_a_four_byte_boundary() -> None:
    """行は4byte境界へ揃え、埋め草は描かない（画像bufferの走査線要件）。"""
    frame = SpectrogramProcessor(history=13).process(_tone(1_000.0), SAMPLE_RATE)
    cells = spectrogram_cells(frame, column_count=13, row_count=7)

    assert cells.columns == 13
    assert cells.row_stride == 16
    assert cells.row_stride % 4 == 0
    assert int(cells.indices[:, cells.columns :].max()) == 0


@pytest.mark.parametrize(("column_count", "row_count"), [(0, 4), (4, 0)])
def test_cells_reject_non_positive_sizes(column_count: int, row_count: int) -> None:
    """0以下の解像度は失敗させる。"""
    frame = SpectrogramProcessor(history=4).process(_tone(1_000.0), SAMPLE_RATE)
    with pytest.raises(ValueError, match="1以上"):
        spectrogram_cells(frame, column_count=column_count, row_count=row_count)

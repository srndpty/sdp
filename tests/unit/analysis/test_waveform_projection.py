"""中央固定波形のpixel投影と座標変換を検証する。"""

import numpy as np
import pytest

from sdp.core.analysis.waveform import WaveformData
from sdp.core.analysis.waveform_projection import (
    WAVEFORM_WINDOW_MS,
    WaveformColumns,
    project_waveform,
    seek_position_from_x,
)


def waveform(
    minimum: list[float],
    maximum: list[float],
    *,
    bucket_ms: float = 1_000.0,
    duration_ms: int | None = None,
    complete: bool = True,
) -> WaveformData:
    return WaveformData(
        np.asarray(minimum, dtype=np.float32),
        np.asarray(maximum, dtype=np.float32),
        bucket_ms,
        len(minimum) * round(bucket_ms) if duration_ms is None else duration_ms,
        complete,
    )


def test_columns_are_independent_read_only_arrays() -> None:
    """列は入力を共有せず、3配列とも変更不能。"""
    minimum = np.array([-0.5], dtype=np.float32)
    maximum = np.array([0.5], dtype=np.float32)
    valid = np.array([True], dtype=np.bool_)
    columns = WaveformColumns(minimum, maximum, valid)
    minimum[0] = 0
    maximum[0] = 0
    valid[0] = False
    assert columns.minimum.tolist() == [-0.5]
    assert columns.maximum.tolist() == [0.5]
    assert columns.valid.tolist() == [True]
    assert not columns.minimum.flags.writeable
    assert not columns.maximum.flags.writeable
    assert not columns.valid.flags.writeable


def test_columns_reject_non_finite_valid_values() -> None:
    """描画対象の列にNaNやinfを許可しない。"""
    with pytest.raises(ValueError, match="NaN"):
        WaveformColumns(
            np.array([np.nan], dtype=np.float32),
            np.array([0.5], dtype=np.float32),
            np.array([True], dtype=np.bool_),
        )


def test_project_zero_width_and_empty_data() -> None:
    """0幅と空波形は安全な空または無効列になる。"""
    data = waveform([], [], duration_ms=0)
    empty = project_waveform(data, center_ms=0, pixel_width=0)
    columns = project_waveform(data, center_ms=0, pixel_width=3)
    assert empty.minimum.size == 0
    assert columns.minimum.shape == (3,)
    assert not columns.valid.any()


@pytest.mark.parametrize("window_ms", [0, -1, True])
def test_project_rejects_invalid_window(window_ms: int) -> None:
    """表示窓は正の整数に限定する。"""
    with pytest.raises(ValueError):
        project_waveform(
            waveform([], [], duration_ms=0),
            center_ms=0,
            window_ms=window_ms,
            pixel_width=1,
        )


def test_one_bucket_per_pixel_and_silence() -> None:
    """1bucket＝1pixelでは値を保ち、無音も有効列になる。"""
    data = waveform([-0.5, 0.0, -0.2], [0.4, 0.0, 0.3])
    columns = project_waveform(data, center_ms=1_500, window_ms=3_000, pixel_width=3)
    np.testing.assert_array_equal(columns.valid, [True, True, True])
    np.testing.assert_allclose(columns.minimum, data.minimum)
    np.testing.assert_allclose(columns.maximum, data.maximum)


def test_multiple_buckets_per_pixel_preserve_both_peaks() -> None:
    """複数bucketの平均ではなくminimum最小・maximum最大を残す。"""
    data = waveform([-0.1, -0.9, -0.2, -0.3], [0.2, 0.3, 0.95, 0.4])
    columns = project_waveform(data, center_ms=2_000, window_ms=4_000, pixel_width=2)
    np.testing.assert_allclose(columns.minimum, [-0.9, -0.3])
    np.testing.assert_allclose(columns.maximum, [0.3, 0.95])


def test_one_bucket_can_expand_to_multiple_pixels() -> None:
    """pixelがbucketより細かい場合は同じpeakを対応列へ展開する。"""
    data = waveform([-0.6], [0.7])
    columns = project_waveform(data, center_ms=500, window_ms=1_000, pixel_width=4)
    np.testing.assert_array_equal(columns.valid, [True, True, True, True])
    np.testing.assert_allclose(columns.minimum, [-0.6] * 4)
    np.testing.assert_allclose(columns.maximum, [0.7] * 4)


def test_start_and_end_outside_audio_stay_invalid_with_center_fixed() -> None:
    """先頭・末尾へ窓を寄せず、音源外を空白列として残す。"""
    data = waveform([-0.5] * 4, [0.5] * 4)
    at_start = project_waveform(data, center_ms=0, window_ms=4_000, pixel_width=4)
    at_end = project_waveform(data, center_ms=4_000, window_ms=4_000, pixel_width=4)
    np.testing.assert_array_equal(at_start.valid, [False, False, True, True])
    np.testing.assert_array_equal(at_end.valid, [True, True, False, False])


def test_partial_unanalyzed_range_is_invalid() -> None:
    """partialのdurationより後ろを音源全体とみなさず無効にする。"""
    data = waveform([-0.5, -0.4], [0.5, 0.4], complete=False)
    columns = project_waveform(data, center_ms=2_000, window_ms=4_000, pixel_width=4)
    np.testing.assert_array_equal(columns.valid, [True, True, False, False])


def test_projection_does_not_modify_input() -> None:
    """投影前後で元のWaveformDataを変更しない。"""
    data = waveform([-0.5, -0.4], [0.5, 0.4])
    before_minimum = data.minimum.copy()
    before_maximum = data.maximum.copy()
    project_waveform(data, center_ms=1_000, window_ms=2_000, pixel_width=20)
    np.testing.assert_array_equal(data.minimum, before_minimum)
    np.testing.assert_array_equal(data.maximum, before_maximum)


@pytest.mark.parametrize("center_ms", [0, 180_000_000, 200_000_000])
def test_long_track_projects_only_fixed_pixel_count(center_ms: int) -> None:
    """180,000 bucketの長尺でも出力は表示幅だけで、範囲外中心も安全。"""
    values = np.linspace(-1.0, 0.0, 180_000, dtype=np.float32)
    data = WaveformData(values, -values, 20.0, 3_600_000, True)
    columns = project_waveform(data, center_ms=center_ms, pixel_width=1_920)
    assert columns.minimum.size == 1_920
    assert columns.maximum.size == 1_920
    assert columns.valid.size == 1_920


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0.0, 30_000), (50.0, 60_000), (100.0, 90_000), (-10.0, 30_000), (110.0, 90_000)],
)
def test_seek_position_maps_window_and_clamps_x(x: float, expected: int) -> None:
    """左端・中央・右端を中央固定窓へ写し、Widget外xをclampする。"""
    assert seek_position_from_x(x, 100, center_ms=60_000, duration_ms=120_000) == expected


def test_seek_position_clamps_to_audio_and_rounds_half_up() -> None:
    """0とdurationへclampし、小数結果はhalf-upで丸める。"""
    assert seek_position_from_x(0, 100, center_ms=1_000, duration_ms=10_000) == 0
    assert seek_position_from_x(100, 100, center_ms=9_000, duration_ms=10_000) == 10_000
    assert (
        seek_position_from_x(
            50.5,
            100,
            center_ms=30_000,
            window_ms=100,
            duration_ms=60_000,
        )
        == 30_001
    )


@pytest.mark.parametrize(
    ("x", "width", "duration"),
    [(True, 100, 1_000), (0.0, 0, 1_000), (0.0, -1, 1_000), (0.0, 100, 0)],
)
def test_seek_position_rejects_invalid_input(x: float, width: int, duration: int) -> None:
    """bool、無効幅、未確定durationでは座標変換しない。"""
    with pytest.raises((TypeError, ValueError)):
        seek_position_from_x(x, width, center_ms=0, duration_ms=duration)


def test_default_window_is_sixty_seconds() -> None:
    """製品の固定表示範囲は前後30秒、合計60秒。"""
    assert WAVEFORM_WINDOW_MS == 60_000

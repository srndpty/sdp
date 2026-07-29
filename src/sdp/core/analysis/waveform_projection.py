"""波形bucketを中央固定の表示pixel列へ投影する純粋ロジック。"""

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from sdp.core.analysis.waveform import WaveformData

WAVEFORM_WINDOW_MS = 60_000
WAVEFORM_HALF_WINDOW_MS = WAVEFORM_WINDOW_MS // 2


@dataclass(frozen=True, slots=True)
class WaveformColumns:
    """1pixelにつき1組のmin/maxと、その列を描画できるかを表す。"""

    minimum: NDArray[np.float32]
    maximum: NDArray[np.float32]
    valid: NDArray[np.bool_]

    def __post_init__(self) -> None:
        minimum = cast(
            "NDArray[np.float32]",
            _validated_column("minimum", self.minimum, np.dtype(np.float32)),
        )
        maximum = cast(
            "NDArray[np.float32]",
            _validated_column("maximum", self.maximum, np.dtype(np.float32)),
        )
        valid = cast(
            "NDArray[np.bool_]",
            _validated_column("valid", self.valid, np.dtype(np.bool_)),
        )
        if minimum.shape != maximum.shape or minimum.shape != valid.shape:
            raise ValueError("波形表示列のshapeが一致しません")
        if not np.all(np.isfinite(minimum[valid])) or not np.all(np.isfinite(maximum[valid])):
            raise ValueError("有効な波形表示列にNaNまたはinfが含まれています")
        if np.any(minimum[valid] > maximum[valid]):
            raise ValueError("有効な波形表示列でminimumがmaximumを超えています")

        minimum = minimum.copy()
        maximum = maximum.copy()
        valid = valid.copy()
        minimum.setflags(write=False)
        maximum.setflags(write=False)
        valid.setflags(write=False)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "valid", valid)


def _validated_column(
    name: str,
    value: object,
    dtype: np.dtype[np.generic],
) -> NDArray[np.generic]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    array = cast("NDArray[np.generic]", value)
    if array.dtype != dtype:
        raise TypeError(f"{name}のdtypeが不正です: {array.dtype}")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    return array


def project_waveform(
    data: WaveformData,
    *,
    center_ms: int,
    window_ms: int = WAVEFORM_WINDOW_MS,
    pixel_width: int,
) -> WaveformColumns:
    """表示窓と交差するbucketだけをpixel単位のpeakへ再集約する。"""
    if type(center_ms) is not int:
        raise TypeError("center_msは整数である必要があります")
    if type(window_ms) is not int or window_ms <= 0:
        raise ValueError("window_msは正の整数である必要があります")
    if type(pixel_width) is not int or pixel_width < 0:
        raise ValueError("pixel_widthは0以上の整数である必要があります")

    minimum = np.zeros(pixel_width, dtype=np.float32)
    maximum = np.zeros(pixel_width, dtype=np.float32)
    valid = np.zeros(pixel_width, dtype=np.bool_)
    if pixel_width == 0 or data.minimum.size == 0 or data.duration_ms <= 0:
        return WaveformColumns(minimum, maximum, valid)

    bucket_ms = data.bucket_duration_ms
    coverage_end_ms = min(
        float(data.duration_ms),
        float(data.minimum.size) * bucket_ms,
    )
    if coverage_end_ms <= 0:
        return WaveformColumns(minimum, maximum, valid)

    window_start_ms = float(center_ms) - window_ms / 2.0
    pixel_duration_ms = window_ms / pixel_width

    # 表示窓と解析済み範囲の交差pixelだけを処理する。全track bucketは走査しない。
    first_pixel = max(0, math.floor((0.0 - window_start_ms) / pixel_duration_ms))
    last_pixel = min(
        pixel_width,
        math.ceil((coverage_end_ms - window_start_ms) / pixel_duration_ms),
    )
    if first_pixel >= last_pixel:
        return WaveformColumns(minimum, maximum, valid)

    bucket_count = int(data.minimum.size)
    for pixel in range(first_pixel, last_pixel):
        pixel_start_ms = window_start_ms + pixel * pixel_duration_ms
        pixel_end_ms = pixel_start_ms + pixel_duration_ms
        overlap_start_ms = max(0.0, pixel_start_ms)
        overlap_end_ms = min(coverage_end_ms, pixel_end_ms)
        if overlap_start_ms >= overlap_end_ms:
            continue
        first_bucket = max(0, math.floor(overlap_start_ms / bucket_ms))
        last_bucket = min(bucket_count, math.ceil(overlap_end_ms / bucket_ms))
        if first_bucket >= last_bucket:
            continue
        minimum[pixel] = np.min(data.minimum[first_bucket:last_bucket])
        maximum[pixel] = np.max(data.maximum[first_bucket:last_bucket])
        valid[pixel] = True

    return WaveformColumns(minimum, maximum, valid)


def seek_position_from_x(
    x: object,
    width: int,
    *,
    center_ms: int,
    window_ms: int = WAVEFORM_WINDOW_MS,
    duration_ms: int,
) -> int:
    """中央固定表示のx座標を0～durationのシーク位置へ変換する。"""
    if isinstance(x, bool) or not isinstance(x, int | float):
        raise TypeError("xは有限の数値である必要があります")
    if not math.isfinite(x):
        raise ValueError("xは有限の数値である必要があります")
    if type(width) is not int or width <= 0:
        raise ValueError("widthは正の整数である必要があります")
    if type(center_ms) is not int:
        raise TypeError("center_msは整数である必要があります")
    if type(window_ms) is not int or window_ms <= 0:
        raise ValueError("window_msは正の整数である必要があります")
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("duration_msは正の整数である必要があります")

    clamped_x = min(float(width), max(0.0, float(x)))
    position = center_ms - window_ms / 2.0 + clamped_x * window_ms / width
    clamped_position = min(float(duration_ms), max(0.0, position))
    return math.floor(clamped_position + 0.5)

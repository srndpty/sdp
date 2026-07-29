"""PCMを描画用の増分min/max波形へ縮約する純粋ロジック。"""

from dataclasses import dataclass
from enum import Enum
from typing import cast

import numpy as np
from numpy.typing import NDArray

WAVEFORM_BUCKET_MS = 20


class UnsupportedSampleFormatError(ValueError):
    """対応していないPCM sample formatが指定された。"""


class PcmSampleFormat(Enum):
    """Qt境界の外で扱うPCM sample format。"""

    UINT8 = "uint8"
    INT16 = "int16"
    INT32 = "int32"
    FLOAT = "float"


@dataclass(frozen=True, slots=True)
class WaveformData:
    """20ms前後のbucketごとのmono最小値・最大値。"""

    minimum: NDArray[np.float32]
    maximum: NDArray[np.float32]
    bucket_duration_ms: float
    duration_ms: int
    complete: bool

    def __post_init__(self) -> None:
        minimum = _validated_array("minimum", self.minimum)
        maximum = _validated_array("maximum", self.maximum)
        if minimum.shape != maximum.shape:
            raise ValueError("minimumとmaximumのshapeが一致しません")
        if np.any(minimum > maximum):
            raise ValueError("minimumがmaximumを超えています")
        if (
            isinstance(self.bucket_duration_ms, bool)
            or not np.isfinite(self.bucket_duration_ms)
            or self.bucket_duration_ms <= 0
        ):
            raise ValueError("bucket_duration_msは正の有限値である必要があります")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("duration_msは0以上の整数である必要があります")
        if type(self.complete) is not bool:
            raise TypeError("completeはboolである必要があります")

        # 呼び出し側の配列とメモリを共有せず、frozen dataclassの要素も不変にする。
        minimum = minimum.copy()
        maximum = maximum.copy()
        minimum.setflags(write=False)
        maximum.setflags(write=False)
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)


def _validated_array(name: str, value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    # ndarrayのdtypeは実行時に直後で検証するため、以降の型だけをfloat32へ絞る。
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    if np.any(array < -1.0) or np.any(array > 1.0):
        raise ValueError(f"{name}が-1.0～1.0の範囲外です")
    return array


def pcm_bytes_to_mono(
    data: bytes,
    sample_format: PcmSampleFormat,
    channels: int,
) -> NDArray[np.float32]:
    """interleaved PCMを[-1, 1]のmono float32へベクトル変換する。"""
    if type(channels) is not int or channels < 1:
        raise ValueError("channelsは1以上である必要があります")
    dtype: np.dtype[np.generic]
    scale: float | None
    offset = 0.0
    if sample_format is PcmSampleFormat.UINT8:
        dtype = np.dtype(np.uint8)
        scale = 128.0
        offset = 128.0
    elif sample_format is PcmSampleFormat.INT16:
        dtype = np.dtype(np.int16)
        scale = 32768.0
    elif sample_format is PcmSampleFormat.INT32:
        dtype = np.dtype(np.int32)
        scale = 2147483648.0
    elif sample_format is PcmSampleFormat.FLOAT:
        dtype = np.dtype(np.float32)
        scale = None
    else:
        raise UnsupportedSampleFormatError(f"未対応のsample formatです: {sample_format!r}")

    frame_bytes = dtype.itemsize * channels
    if len(data) % frame_bytes != 0:
        raise ValueError("PCM bytesがframe境界で終わっていません")
    if not data:
        return np.empty(0, dtype=np.float32)

    raw = np.frombuffer(data, dtype=dtype).reshape(-1, channels)
    frames = raw.astype(np.float32)
    if scale is not None:
        frames = (frames - offset) / scale
    mono = frames.mean(axis=1, dtype=np.float32)
    # 壊れたfloat PCMの非有限値を波形へ残さない。
    np.nan_to_num(mono, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
    np.clip(mono, -1.0, 1.0, out=mono)
    return mono.astype(np.float32, copy=False)


class WaveformReducer:
    """全PCMを保持せず、chunkを跨いで固定frame数ごとに縮約する。"""

    def __init__(
        self,
        sample_rate: int,
        bucket_duration_ms: int = WAVEFORM_BUCKET_MS,
    ) -> None:
        if type(sample_rate) is not int or sample_rate < 1:
            raise ValueError("sample_rateは1以上である必要があります")
        if type(bucket_duration_ms) is not int or bucket_duration_ms < 1:
            raise ValueError("bucket_duration_msは1以上である必要があります")
        self._sample_rate = sample_rate
        self._frames_per_bucket = max(1, round(sample_rate * bucket_duration_ms / 1000))
        self._bucket_duration_ms = self._frames_per_bucket * 1000.0 / sample_rate
        self._minimum: list[float] = []
        self._maximum: list[float] = []
        self._pending = np.empty(0, dtype=np.float32)
        self._total_frames = 0

    @property
    def frames_per_bucket(self) -> int:
        return self._frames_per_bucket

    @property
    def bucket_count(self) -> int:
        return len(self._minimum)

    @property
    def pending_frame_count(self) -> int:
        return int(self._pending.size)

    def append(self, mono_samples: NDArray[np.float32]) -> None:
        """新しいmono chunkを追加し、完成したbucketだけを履歴へ残す。"""
        if mono_samples.ndim != 1:
            raise ValueError("mono_samplesは1次元NumPy配列である必要があります")
        if mono_samples.size == 0:
            return
        samples = np.array(mono_samples, dtype=np.float32, copy=True)
        np.nan_to_num(samples, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(samples, -1.0, 1.0, out=samples)
        self._total_frames += int(samples.size)
        if self._pending.size:
            samples = np.concatenate((self._pending, samples))
            self._pending = np.empty(0, dtype=np.float32)

        complete_count = samples.size // self._frames_per_bucket
        complete_frames = complete_count * self._frames_per_bucket
        if complete_count:
            buckets = samples[:complete_frames].reshape(complete_count, self._frames_per_bucket)
            self._minimum.extend(buckets.min(axis=1).tolist())
            self._maximum.extend(buckets.max(axis=1).tolist())
        if complete_frames < samples.size:
            self._pending = samples[complete_frames:].copy()

    def snapshot(self, *, complete: bool) -> WaveformData:
        """現在までの独立したread-only snapshotを返す。"""
        minimum = np.asarray(self._minimum, dtype=np.float32)
        maximum = np.asarray(self._maximum, dtype=np.float32)
        if complete and self._pending.size:
            minimum = np.append(minimum, np.float32(self._pending.min()))
            maximum = np.append(maximum, np.float32(self._pending.max()))
        return WaveformData(
            minimum=minimum.astype(np.float32, copy=False),
            maximum=maximum.astype(np.float32, copy=False),
            bucket_duration_ms=self._bucket_duration_ms,
            duration_ms=round(self._total_frames * 1000 / self._sample_rate),
            complete=complete,
        )

"""固定容量のmono float32 PCMリングバッファ。

Qt非依存。書き込みは音声buffer受信（P0-C実測ではGUI thread）から、読み出しは
可視化のGUIタイマーから行う。将来PCM受信をworker threadへ移してもデータ競合を
起こさないよう、短時間のlockだけで保護する（FFT中はlockを保持しない）。
"""

import threading

import numpy as np
from numpy.typing import NDArray

PCM_RING_BUFFER_SECONDS = 2.0
"""スペクトラムのFFT長より十分に長く保持する秒数。"""

DEFAULT_PCM_SAMPLE_RATE = 48_000
"""sample rateが判明する前に使う既定値。format判明時に容量を作り直す。"""


def pcm_ring_capacity(sample_rate: int, minimum_frames: int = 0) -> int:
    """``sample_rate`` で :data:`PCM_RING_BUFFER_SECONDS` 秒を保持する容量を返す。

    最大想定sample rateへ固定して巨大化させず、format判明時にこの関数で
    作り直す。``minimum_frames`` はFFT長など、秒数によらず必要な下限。
    """
    if type(sample_rate) is not int or sample_rate < 1:
        raise ValueError("sample_rateは1以上の整数である必要があります")
    if type(minimum_frames) is not int or minimum_frames < 0:
        raise ValueError("minimum_framesは0以上の整数である必要があります")
    return max(1, minimum_frames, round(sample_rate * PCM_RING_BUFFER_SECONDS))


class PcmRingBuffer:
    """最新のmono float32サンプルだけを固定容量で保持する。

    契約:

    - dtypeはfloat32、1次元。全履歴は保持せず、満杯時は古いサンプルを上書きする。
    - 1回の ``append`` が容量を超える場合は末尾の容量分だけを保持する。
    - ``snapshot(n)`` は常に長さ ``n`` の独立したread-only配列を返す。
      保持数が ``n`` 未満のときは**左側を0で埋める**（起動直後からFFTのshapeを
      安定させるため。無効frameにはしない）。
    - NaN／infは保持しない（0と±1へ寄せる）。壊れたPCMで描画が破綻しないため。
    """

    def __init__(self, capacity: int) -> None:
        self._lock = threading.Lock()
        self._buffer = np.zeros(_validated_capacity(capacity), dtype=np.float32)
        self._write_index = 0
        self._filled = 0

    @property
    def capacity(self) -> int:
        return int(self._buffer.size)

    @property
    def available(self) -> int:
        """現在保持しているサンプル数（容量が上限）。"""
        with self._lock:
            return self._filled

    def set_capacity(self, capacity: int) -> None:
        """容量を作り直し、保持中のサンプルを破棄する。

        sample rate変更時に呼ぶ。旧formatのサンプルを新formatへ混ぜない。
        同じ容量でも内容はclearする（呼び出し側の意図は常に「作り直し」）。
        """
        buffer = np.zeros(_validated_capacity(capacity), dtype=np.float32)
        with self._lock:
            self._buffer = buffer
            self._write_index = 0
            self._filled = 0

    def append(self, samples: NDArray[np.float32]) -> None:
        """mono float32のchunkを追記する。容量超過分は古い側から捨てる。"""
        # 型注釈はNDArrayだが、呼び出し側の実行時の誤りも表面化させる。
        if not isinstance(samples, np.ndarray):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("samplesはNumPy配列である必要があります")
        if samples.dtype != np.dtype(np.float32):
            raise TypeError("samplesのdtypeはfloat32である必要があります")
        if samples.ndim != 1:
            raise ValueError("samplesは1次元である必要があります")
        if samples.size == 0:
            return

        capacity = self.capacity
        # 容量を超える分は書いた直後に上書きされるため、末尾だけを扱う。
        tail = samples[-capacity:] if samples.size > capacity else samples
        if not bool(np.all(np.isfinite(tail))):
            tail = np.nan_to_num(tail, nan=0.0, posinf=1.0, neginf=-1.0)

        count = int(tail.size)
        with self._lock:
            # 容量はlock外で読んだ値と異なりうる（set_capacityとの競合）。
            if self._buffer.size != capacity:
                return
            start = self._write_index
            first = min(count, capacity - start)
            self._buffer[start : start + first] = tail[:first]
            if count > first:
                self._buffer[: count - first] = tail[first:]
            self._write_index = (start + count) % capacity
            self._filled = min(capacity, self._filled + count)

    def snapshot(self, frame_count: int) -> NDArray[np.float32]:
        """最新 ``frame_count`` サンプルを左0 paddingでコピーして返す。"""
        if type(frame_count) is not int or frame_count < 0:
            raise ValueError("frame_countは0以上の整数である必要があります")
        result = np.zeros(frame_count, dtype=np.float32)
        if frame_count:
            with self._lock:
                take = min(frame_count, self._filled)
                if take:
                    capacity = self._buffer.size
                    start = (self._write_index - take) % capacity
                    offset = frame_count - take
                    if start + take <= capacity:
                        result[offset:] = self._buffer[start : start + take]
                    else:
                        split = capacity - start
                        result[offset : offset + split] = self._buffer[start:]
                        result[offset + split :] = self._buffer[: take - split]
        # 呼び出し側が内部bufferを書き換えたり、返り値を共有と誤解しないようにする。
        result.setflags(write=False)
        return result

    def clear(self) -> None:
        """保持中のサンプルを破棄する（容量は変えない）。"""
        with self._lock:
            self._write_index = 0
            self._filled = 0


def _validated_capacity(capacity: int) -> int:
    if type(capacity) is not int or capacity < 1:
        raise ValueError("capacityは1以上の整数である必要があります")
    return capacity

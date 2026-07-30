"""Hann窓・rFFT・対数band集約・dB変換・時間平滑化の純粋ロジック。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。
"""

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

FFT_SIZE = 4096
"""1フレームのFFT点数。48kHzで約85ms、分解能約11.7Hz。"""

SPECTRUM_BAND_COUNT = 96
SPECTRUM_MIN_HZ = 30.0
SPECTRUM_MAX_HZ = 20_000.0
SPECTRUM_DB_FLOOR = -90.0
SPECTRUM_FPS = 30
SPECTRUM_TIMER_INTERVAL_MS = 33
"""約30FPS。タイマー1回でFFTは最大1回（再入させない）。"""

SPECTRUM_ATTACK = 0.65
"""前回より大きい値へ向かう係数。大きいほど立ち上がりが速い。"""

SPECTRUM_RELEASE = 0.15
"""前回より小さい値へ向かう係数。小さいほど減衰が滑らかになる。"""

_MAGNITUDE_EPSILON = 1e-12
"""log(0) と 0 除算を避けるための下限。-240dB相当でfloorへclampされる。"""


@dataclass(frozen=True, slots=True)
class SpectrumFrame:
    """1フレーム分の帯域別レベル。read-onlyで色や描画情報を持たない。"""

    frequencies_hz: NDArray[np.float32]
    levels_db: NDArray[np.float32]

    def __post_init__(self) -> None:
        frequencies = _validated_array("frequencies_hz", self.frequencies_hz)
        levels = _validated_array("levels_db", self.levels_db)
        if frequencies.shape != levels.shape:
            raise ValueError("frequencies_hzとlevels_dbのshapeが一致しません")
        if np.any(frequencies < 0.0):
            raise ValueError("frequencies_hzに負の値が含まれています")
        if np.any(np.diff(frequencies) <= 0.0):
            raise ValueError("frequencies_hzは昇順である必要があります")
        if np.any(levels > 0.0):
            raise ValueError("levels_dbが0dBを超えています")

        # 呼び出し側の配列とメモリを共有せず、frozen dataclassの要素も不変にする。
        frequencies = frequencies.copy()
        levels = levels.copy()
        frequencies.setflags(write=False)
        levels.setflags(write=False)
        object.__setattr__(self, "frequencies_hz", frequencies)
        object.__setattr__(self, "levels_db", levels)

    @property
    def band_count(self) -> int:
        return int(self.levels_db.size)


def empty_spectrum_frame() -> SpectrumFrame:
    """有効帯域が存在しない場合の空フレーム。"""
    return SpectrumFrame(
        frequencies_hz=np.empty(0, dtype=np.float32),
        levels_db=np.empty(0, dtype=np.float32),
    )


def _validated_array(name: str, value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    # ndarrayのdtypeは直後に検証するため、以降の型だけをfloat32へ絞る。
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if not bool(np.all(np.isfinite(array))):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    return array


def effective_max_hz(sample_rate: int, max_hz: float = SPECTRUM_MAX_HZ) -> float:
    """Nyquist以下へ制限した表示上限周波数を返す。"""
    return min(max_hz, sample_rate / 2.0)


def fit_fft_input(samples: NDArray[np.float32], fft_size: int = FFT_SIZE) -> NDArray[np.float32]:
    """FFT長へ合わせる。長い場合は最新側、短い場合は左を0で埋める。

    左0 paddingにより、起動直後や曲頭でも常に同じshapeを返せる。
    """
    if fft_size < 1:
        raise ValueError("fft_sizeは1以上である必要があります")
    if samples.ndim != 1:
        raise ValueError("samplesは1次元である必要があります")
    if samples.size == fft_size:
        return samples
    if samples.size > fft_size:
        return samples[-fft_size:]
    fitted = np.zeros(fft_size, dtype=np.float32)
    if samples.size:
        fitted[fft_size - samples.size :] = samples
    return fitted


def compute_spectrum(
    samples: NDArray[np.float32],
    sample_rate: int,
    *,
    fft_size: int = FFT_SIZE,
    band_count: int = SPECTRUM_BAND_COUNT,
    min_hz: float = SPECTRUM_MIN_HZ,
    max_hz: float = SPECTRUM_MAX_HZ,
    db_floor: float = SPECTRUM_DB_FLOOR,
) -> SpectrumFrame:
    """PCMから対数band別dBのスペクトラムを求める（入力配列は変更しない）。

    手順は DC除去 → Hann窓 → rFFT → 振幅 → 窓補正 → dB → floor clamp →
    band集約。振幅補正は「0dBFSの正弦波が約0dBになる」ように正規化する
    （音響測定器としての校正精度は要求しない）。
    """
    if sample_rate < 1:
        raise ValueError("sample_rateは1以上である必要があります")
    if band_count < 1:
        raise ValueError("band_countは1以上である必要があります")
    if min_hz <= 0.0:
        raise ValueError("min_hzは正の値である必要があります")
    if db_floor >= 0.0:
        raise ValueError("db_floorは負の値である必要があります")

    limit_hz = effective_max_hz(sample_rate, max_hz)
    if limit_hz <= min_hz:
        # 低sample rateで有効帯域が無い場合は band を捏造しない。
        return empty_spectrum_frame()

    frame = fit_fft_input(samples, fft_size).astype(np.float64, copy=True)
    frame -= frame.mean()
    window = np.hanning(frame.size)
    # Hann窓のコヒーレントゲイン補正。実正弦波は正負の対を持つため係数2を掛ける。
    # fft_size 2 では窓が全0になるため、0除算を避けて補正なしとする。
    window_sum = float(window.sum())
    correction = 2.0 / window_sum if window_sum > 0.0 else 1.0
    magnitude = np.abs(np.fft.rfft(frame * window)) * correction
    np.maximum(magnitude, _MAGNITUDE_EPSILON, out=magnitude)
    bin_db = np.clip(20.0 * np.log10(magnitude), db_floor, 0.0)
    bin_hz = np.fft.rfftfreq(frame.size, d=1.0 / sample_rate)

    # np.logspace の戻り値型は PySide6 環境の numpy stub で Unknown になるため明示する。
    edges = cast(
        "NDArray[np.float64]",
        np.logspace(np.log10(min_hz), np.log10(limit_hz), band_count + 1),
    )
    # 各binの所属band。範囲外は負値またはband_count以上になる。
    band_of_bin = np.searchsorted(edges, bin_hz, side="right") - 1
    inside = (band_of_bin >= 0) & (band_of_bin < band_count)
    levels = np.full(band_count, db_floor, dtype=np.float64)
    # 視認性を優先し、band内はピークを残す最大値で集約する。
    np.maximum.at(levels, band_of_bin[inside], bin_db[inside])

    centers = np.sqrt(edges[:-1] * edges[1:])
    counts = np.bincount(band_of_bin[inside], minlength=band_count)
    empty = counts == 0
    if bool(empty.any()):
        # binより狭いband（主に低域）は、最大値の複製でピークを広げず補間する。
        levels[empty] = np.interp(centers[empty], bin_hz, bin_db)

    return SpectrumFrame(
        frequencies_hz=centers.astype(np.float32),
        levels_db=np.clip(levels, db_floor, 0.0).astype(np.float32),
    )


class SpectrumProcessor:
    """前フレームを保持し、attack／releaseで時間平滑化する。

    QWidgetへ平滑化状態を持たせないため、この状態はPanel側が所有する。
    sample rate変更・source変更・停止では :meth:`reset` で履歴を捨てる。
    """

    def __init__(
        self,
        *,
        fft_size: int = FFT_SIZE,
        band_count: int = SPECTRUM_BAND_COUNT,
        min_hz: float = SPECTRUM_MIN_HZ,
        max_hz: float = SPECTRUM_MAX_HZ,
        db_floor: float = SPECTRUM_DB_FLOOR,
        attack: float = SPECTRUM_ATTACK,
        release: float = SPECTRUM_RELEASE,
    ) -> None:
        self._fft_size = fft_size
        self._band_count = band_count
        self._min_hz = min_hz
        self._max_hz = max_hz
        self._db_floor = db_floor
        self._attack = _validated_coefficient("attack", attack)
        self._release = _validated_coefficient("release", release)
        self._smoothed: NDArray[np.float64] | None = None
        self._sample_rate: int | None = None

    @property
    def db_floor(self) -> float:
        return self._db_floor

    @property
    def fft_size(self) -> int:
        return self._fft_size

    @property
    def sample_rate(self) -> int | None:
        """直前に処理したsample rate（未処理なら ``None``）。"""
        return self._sample_rate

    def reset(self) -> None:
        """平滑化履歴とsample rateを捨てる。次フレームは生の値から始まる。"""
        self._smoothed = None
        self._sample_rate = None

    def process(self, samples: NDArray[np.float32], sample_rate: int) -> SpectrumFrame:
        """1フレームを解析し、平滑化済みのフレームを返す。"""
        if sample_rate != self._sample_rate:
            # 旧formatの平滑化状態を新formatへ混ぜない。
            self._smoothed = None
            self._sample_rate = sample_rate
        frame = compute_spectrum(
            samples,
            sample_rate,
            fft_size=self._fft_size,
            band_count=self._band_count,
            min_hz=self._min_hz,
            max_hz=self._max_hz,
            db_floor=self._db_floor,
        )
        if frame.band_count == 0:
            self._smoothed = None
            return frame

        current = frame.levels_db.astype(np.float64)
        previous = self._smoothed
        if previous is None or previous.shape != current.shape:
            self._smoothed = current
        else:
            coefficients = np.where(current > previous, self._attack, self._release)
            self._smoothed = previous + coefficients * (current - previous)
        smoothed = np.clip(self._smoothed, self._db_floor, 0.0)
        return SpectrumFrame(
            frequencies_hz=frame.frequencies_hz,
            levels_db=smoothed.astype(np.float32),
        )


def _validated_coefficient(name: str, value: float) -> float:
    if isinstance(value, bool) or not np.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"{name}は0より大きく1以下である必要があります")
    return float(value)

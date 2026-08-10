"""クロマグラム（12音のピッチクラス強度）の純粋ロジック。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。

rFFTの線形振幅（:func:`sdp.core.analysis.spectrum.magnitude_spectrum` を再利用）を
各binの周波数から最も近い12平均律のピッチクラスへ集約し、和音や調の色合いを見せる。
音楽的な正確さ（正確な音名推定）は目的にせず、視覚的な傾向表示にとどめる。
"""

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from sdp.core.analysis.spectrum import FFT_SIZE, FrequencyAnalysisFrame, analysis_for

CHROMA_CLASS_COUNT = 12
"""1オクターブのピッチクラス数（C, C#, ... B）。"""

PITCH_CLASS_NAMES: tuple[str, ...] = (
    "C",
    "C#",
    "D",
    "D#",
    "E",
    "F",
    "F#",
    "G",
    "G#",
    "A",
    "A#",
    "B",
)
"""表示用の音名。色だけに頼らず音名を併記するために使う。"""

CHROMA_MIN_HZ = 55.0
"""集約対象の下限周波数（A1）。これより低いbinは無視する。"""

CHROMA_MAX_HZ = 5_000.0
"""集約対象の上限周波数。倍音のノイズを避けるため可聴域の上を切る。"""

CHROMA_ATTACK = 0.6
"""強度が上がるときの平滑化係数（大きいほど速い）。"""

CHROMA_RELEASE = 0.2
"""強度が下がるときの平滑化係数（小さいほど滑らか）。"""

_REFERENCE_HZ = 440.0
"""A4の基準周波数。ピッチクラス割り当ての基準にする。"""

_A4_PITCH_CLASS = 9
"""A のピッチクラスindex（C=0 起点）。"""

_ENERGY_EPSILON = 1e-12
"""正規化時の0除算を避ける下限。"""


@dataclass(frozen=True, slots=True)
class ChromaFrame:
    """12ピッチクラスの正規化強度。read-onlyで色や描画情報を持たない。

    ``values`` は 0.0〜1.0 の float32、長さ12（C 起点）。全classが無エネルギーの
    ときは全0（無音表示）。
    """

    values: NDArray[np.float32]

    def __post_init__(self) -> None:
        values = _validated_values(self.values)
        values = values.copy()
        values.setflags(write=False)
        object.__setattr__(self, "values", values)


def empty_chroma_frame() -> ChromaFrame:
    """全0の無音フレーム。"""
    return ChromaFrame(values=np.zeros(CHROMA_CLASS_COUNT, dtype=np.float32))


def compute_chroma(
    samples: NDArray[np.float32],
    sample_rate: int,
    *,
    fft_size: int = FFT_SIZE,
    min_hz: float = CHROMA_MIN_HZ,
    max_hz: float = CHROMA_MAX_HZ,
    analysis: FrequencyAnalysisFrame | None = None,
) -> ChromaFrame:
    """PCMから12ピッチクラスの正規化強度を求める（入力配列は変更しない）。

    手順は 振幅スペクトラム → 帯域内のbin抽出 → 各binを最寄りのピッチクラスへ集約
    → 最大値で正規化。最大値が実質0の（無音の）場合は全0を返す。

    ``analysis`` を渡すと rFFT を再実行せずその結果を使う（同一tickで
    スペクトラム・スペクトログラムとFFTを共有するため）。
    """
    if sample_rate < 1:
        raise ValueError("sample_rateは1以上である必要があります")
    if min_hz <= 0.0:
        raise ValueError("min_hzは正の値である必要があります")
    if max_hz <= min_hz:
        raise ValueError("max_hzはmin_hzより大きい必要があります")

    frame = analysis_for(samples, sample_rate, fft_size=fft_size, analysis=analysis)
    frequencies = frame.frequencies_hz
    magnitude = frame.magnitudes
    limit_hz = min(max_hz, sample_rate / 2.0)
    inside = (frequencies >= min_hz) & (frequencies <= limit_hz)
    if not bool(inside.any()):
        return empty_chroma_frame()

    band_hz = frequencies[inside]
    band_magnitude = magnitude[inside]
    # 各binを最も近い12平均律のピッチクラスへ写す（A4=440Hzを基準）。
    semitones = 12.0 * np.log2(band_hz / _REFERENCE_HZ)
    pitch_class = (np.rint(semitones).astype(np.int64) + _A4_PITCH_CLASS) % CHROMA_CLASS_COUNT

    totals = np.zeros(CHROMA_CLASS_COUNT, dtype=np.float64)
    # 強度はエネルギー（振幅の二乗）で集約し、目立つ成分を強調する。
    np.add.at(totals, pitch_class, np.square(band_magnitude.astype(np.float64)))

    peak = float(totals.max())
    if peak <= _ENERGY_EPSILON:
        return empty_chroma_frame()
    values = (totals / peak).astype(np.float32)
    return ChromaFrame(values=values)


class ChromaProcessor:
    """クロマ強度を時間平滑化する状態クラス。

    QWidgetへ平滑化状態を持たせないため、Panel側がこのProcessorを所有する。
    source変更・停止・sample rate変更では :meth:`reset` で履歴を捨てる。
    """

    def __init__(
        self,
        *,
        fft_size: int = FFT_SIZE,
        min_hz: float = CHROMA_MIN_HZ,
        max_hz: float = CHROMA_MAX_HZ,
        attack: float = CHROMA_ATTACK,
        release: float = CHROMA_RELEASE,
    ) -> None:
        self._fft_size = fft_size
        self._min_hz = min_hz
        self._max_hz = max_hz
        self._attack = _validated_coefficient("attack", attack)
        self._release = _validated_coefficient("release", release)
        self._smoothed: NDArray[np.float64] | None = None
        self._sample_rate: int | None = None

    @property
    def fft_size(self) -> int:
        return self._fft_size

    @property
    def sample_rate(self) -> int | None:
        return self._sample_rate

    def reset(self) -> None:
        """平滑化履歴とsample rateを捨てる。次フレームは生の値から始まる。"""
        self._smoothed = None
        self._sample_rate = None

    def process(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        *,
        analysis: FrequencyAnalysisFrame | None = None,
    ) -> ChromaFrame:
        """1フレームを解析し、平滑化済みのフレームを返す。

        ``analysis`` を渡すと同一tickの他の可視化とrFFTを共有する。
        """
        if sample_rate != self._sample_rate:
            self._smoothed = None
            self._sample_rate = sample_rate
        frame = compute_chroma(
            samples,
            sample_rate,
            fft_size=self._fft_size,
            min_hz=self._min_hz,
            max_hz=self._max_hz,
            analysis=analysis,
        )
        current = frame.values.astype(np.float64)
        previous = self._smoothed
        if previous is None or previous.shape != current.shape:
            self._smoothed = current
        else:
            coefficients = np.where(current > previous, self._attack, self._release)
            self._smoothed = previous + coefficients * (current - previous)
        smoothed = np.clip(self._smoothed, 0.0, 1.0).astype(np.float32)
        return ChromaFrame(values=smoothed)


def _validated_values(value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError("valuesはNumPy配列である必要があります")
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError("valuesのdtypeはfloat32である必要があります")
    if array.ndim != 1 or array.size != CHROMA_CLASS_COUNT:
        raise ValueError(f"valuesは長さ{CHROMA_CLASS_COUNT}の1次元である必要があります")
    if not bool(np.all(np.isfinite(array))):
        raise ValueError("valuesにNaNまたはinfが含まれています")
    if bool(np.any(array < 0.0)) or bool(np.any(array > 1.0)):
        raise ValueError("valuesが0.0〜1.0の範囲外です")
    return array


def _validated_coefficient(name: str, value: float) -> float:
    if isinstance(value, bool) or not np.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"{name}は0より大きく1以下である必要があります")
    return float(value)

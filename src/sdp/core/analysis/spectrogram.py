"""スペクトログラム（時間×周波数の強度履歴）の純粋ロジック。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。

スペクトラム（:func:`sdp.core.analysis.spectrum.compute_spectrum` の対数band別dB）を
1tickごとに1列として横方向へ積み、時間の経過とともに流れる2Dの強度マップを作る。
平滑化はしない（時間軸そのものが履歴になるため）。全PCM履歴は保持せず、
固定列数のリングとして最新の履歴だけを持つ。
"""

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

from sdp.core.analysis.spectrum import (
    FFT_SIZE,
    SPECTRUM_BAND_COUNT,
    SPECTRUM_DB_FLOOR,
    SPECTRUM_MAX_HZ,
    SPECTRUM_MIN_HZ,
    compute_spectrum,
)

SPECTROGRAM_HISTORY = 256
"""保持する時間方向の列数。30FPSで約8.5秒分。"""


@dataclass(frozen=True, slots=True)
class SpectrogramFrame:
    """時間×バンドの強度履歴。read-onlyで色や座標変換情報を持たない。

    ``columns`` は shape ``(history, band_count)`` の float32 dB値で、
    行indexが大きいほど新しい列（右端が最新）。``db_floor`` は下限dB。
    履歴がまだ ``history`` に満たない場合も左側を ``db_floor`` で埋めて
    固定shapeを保つ。
    """

    columns: NDArray[np.float32]
    db_floor: float

    def __post_init__(self) -> None:
        columns = _validated_columns(self.columns)
        columns = columns.copy()
        columns.setflags(write=False)
        object.__setattr__(self, "columns", columns)

    @property
    def history(self) -> int:
        return int(self.columns.shape[0])

    @property
    def band_count(self) -> int:
        return int(self.columns.shape[1])


class SpectrogramProcessor:
    """スペクトラム列を固定列数のリングへ積む状態クラス。

    QWidgetへ履歴を持たせないため、Panel側がこのProcessorを所有する。
    source変更・停止・sample rate変更では :meth:`reset` で履歴を捨てる。
    """

    def __init__(
        self,
        *,
        history: int = SPECTROGRAM_HISTORY,
        fft_size: int = FFT_SIZE,
        band_count: int = SPECTRUM_BAND_COUNT,
        min_hz: float = SPECTRUM_MIN_HZ,
        max_hz: float = SPECTRUM_MAX_HZ,
        db_floor: float = SPECTRUM_DB_FLOOR,
    ) -> None:
        if history < 1:
            raise ValueError("historyは1以上である必要があります")
        if band_count < 1:
            raise ValueError("band_countは1以上である必要があります")
        if db_floor >= 0.0:
            raise ValueError("db_floorは負の値である必要があります")
        self._history = history
        self._fft_size = fft_size
        self._band_count = band_count
        self._min_hz = min_hz
        self._max_hz = max_hz
        self._db_floor = db_floor
        self._columns = np.full((history, band_count), db_floor, dtype=np.float32)
        self._sample_rate: int | None = None

    @property
    def db_floor(self) -> float:
        return self._db_floor

    @property
    def fft_size(self) -> int:
        return self._fft_size

    @property
    def band_count(self) -> int:
        return self._band_count

    @property
    def sample_rate(self) -> int | None:
        return self._sample_rate

    def reset(self) -> None:
        """履歴とsample rateを捨てる（全列をfloorへ戻す）。"""
        self._columns.fill(self._db_floor)
        self._sample_rate = None

    def process(self, samples: NDArray[np.float32], sample_rate: int) -> SpectrogramFrame:
        """1列を解析して履歴へ積み、履歴全体のフレームを返す。"""
        if sample_rate != self._sample_rate:
            # 旧formatの履歴を新formatへ混ぜない。
            self._columns.fill(self._db_floor)
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
        column = self._column_from_levels(frame.levels_db)
        # 1列ぶん左へずらし、右端へ最新列を書く。
        self._columns[:-1] = self._columns[1:]
        self._columns[-1] = column
        return SpectrogramFrame(columns=self._columns, db_floor=self._db_floor)

    def _column_from_levels(self, levels_db: NDArray[np.float32]) -> NDArray[np.float32]:
        """スペクトラムのdB列を、band数へ合わせた1列へ整える。

        有効帯域が無い（低sample rate等）場合はfloorで埋める。band数が想定と
        異なる場合は線形補間で ``band_count`` へ揃える。
        """
        if levels_db.size == self._band_count:
            return levels_db.astype(np.float32, copy=True)
        if levels_db.size == 0:
            return np.full(self._band_count, self._db_floor, dtype=np.float32)
        source_x = np.linspace(0.0, 1.0, levels_db.size)
        target_x = np.linspace(0.0, 1.0, self._band_count)
        return np.interp(target_x, source_x, levels_db).astype(np.float32)


def _validated_columns(value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError("columnsはNumPy配列である必要があります")
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError("columnsのdtypeはfloat32である必要があります")
    if array.ndim != 2:
        raise ValueError("columnsは2次元である必要があります")
    if array.shape[0] < 1 or array.shape[1] < 1:
        raise ValueError("columnsは各次元1以上である必要があります")
    if not bool(np.all(np.isfinite(array))):
        raise ValueError("columnsにNaNまたはinfが含まれています")
    return array

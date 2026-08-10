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
    FrequencyAnalysisFrame,
    compute_spectrum,
)

SPECTROGRAM_HISTORY = 256
"""保持する時間方向の列数。30FPSで約8.5秒分。"""

CELL_LEVEL_MAX = 255
"""セル強度の最大値。0は「描かない」を意味する（floor以下）。"""


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
        if columns.flags.writeable:
            # 呼び出し側が保持し続ける配列と履歴を共有しない。既にread-onlyな配列
            # （Processorがtickごとに作る一時配列）はそのまま使い、コピーを省く。
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
        # 次に書き込む位置（= 現時点で最も古い列）。列を左へずらす代わりに
        # ここだけを進めるため、1tickの書き込みは1列分で済む。
        self._write_index = 0
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
        self._clear_history()
        self._sample_rate = None

    def process(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        *,
        analysis: FrequencyAnalysisFrame | None = None,
    ) -> SpectrogramFrame:
        """1列を解析して履歴へ積み、履歴全体のフレームを返す。

        ``analysis`` を渡すと同一tickの他の可視化とrFFTを共有する。
        """
        if sample_rate != self._sample_rate:
            # 旧formatの履歴を新formatへ混ぜない。
            self._clear_history()
            self._sample_rate = sample_rate
        frame = compute_spectrum(
            samples,
            sample_rate,
            fft_size=self._fft_size,
            band_count=self._band_count,
            min_hz=self._min_hz,
            max_hz=self._max_hz,
            db_floor=self._db_floor,
            analysis=analysis,
        )
        column = self._column_from_levels(frame.levels_db)
        # 履歴はリングとして持ち、1tickで書くのは1列だけにする（全列のシフトをしない）。
        self._columns[self._write_index] = column
        self._write_index = (self._write_index + 1) % self._history
        return SpectrogramFrame(columns=self._ordered_columns(), db_floor=self._db_floor)

    def _clear_history(self) -> None:
        self._columns.fill(self._db_floor)
        self._write_index = 0

    def _ordered_columns(self) -> NDArray[np.float32]:
        """リングを古い順（右端が最新）へ並べ直したread-onlyの一時配列を返す。

        フレームはこの配列をコピーせずそのまま保持するため、1tickあたりの
        履歴コピーは1回で済む。Processor側のリングは以後も書き換わるが、
        この配列とはメモリを共有しない。
        """
        index = self._write_index
        ordered = np.concatenate((self._columns[index:], self._columns[:index]))
        ordered.setflags(write=False)
        return ordered

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


@dataclass(frozen=True, slots=True)
class SpectrogramCells:
    """描画用に間引いた強度セル（行=周波数、列=時間）。

    ``indices`` は shape ``(rows, row_stride)`` の uint8 で、0 は「floor以下＝描かない」、
    1〜255 は強度。色は持たず、色への写像はUI層の責務とする。
    行0が最高域（上）、列0が最も古い時刻（左）。

    各行は ``row_stride``（4の倍数）まで0で埋める。画像bufferとして
    そのまま渡せるよう、走査線を4byte境界へ揃えるため。有効な列数は
    ``columns`` で、右側の埋め草は描画対象に含めない。
    """

    indices: NDArray[np.uint8]
    columns: int
    painted_count: int

    @property
    def rows(self) -> int:
        return int(self.indices.shape[0])

    @property
    def row_stride(self) -> int:
        return int(self.indices.shape[1])


def spectrogram_cells(
    frame: SpectrogramFrame,
    *,
    column_count: int,
    row_count: int,
) -> SpectrogramCells:
    """履歴を表示解像度へ間引き、0〜255の強度セルへ写す（NumPyだけで行う）。

    Widget側でセルごとのPythonループとQColor生成を行わないための前処理。
    要求解像度が履歴・band数を超える場合は、データ以上に細かくしても情報が
    増えないため、それぞれの上限で切り詰める（呼び出し側は結果を引き伸ばす）。
    """
    if column_count < 1 or row_count < 1:
        raise ValueError("column_countとrow_countは1以上である必要があります")
    history = frame.history
    band_count = frame.band_count
    columns = min(column_count, history)
    rows = min(row_count, band_count)

    # 時間方向: 表示列ごとに履歴列を1本選ぶ（古い順のまま）。
    column_index = (np.arange(columns) * history) // columns
    selected = frame.columns[column_index]
    # 周波数方向: 高域を上へ描くため反転し、行ごとにband群の最大値を残す。
    bands = selected.T[::-1]
    starts = (np.arange(rows) * band_count) // rows
    reduced = cast("NDArray[np.float32]", np.maximum.reduceat(bands, starts, axis=0))

    span = -frame.db_floor
    stride = (columns + 3) // 4 * 4
    if span <= 0.0:
        return SpectrogramCells(
            indices=np.zeros((rows, stride), dtype=np.uint8),
            columns=columns,
            painted_count=0,
        )
    ratio = np.clip((reduced - frame.db_floor) / span, 0.0, 1.0)
    # floorちょうど（ratio 0）だけを非描画にし、わずかな信号は必ず1以上にする。
    levels = np.ceil(ratio * CELL_LEVEL_MAX).astype(np.uint8)
    indices = np.zeros((rows, stride), dtype=np.uint8)
    indices[:, :columns] = levels
    return SpectrogramCells(
        indices=indices,
        columns=columns,
        painted_count=int(np.count_nonzero(levels)),
    )


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

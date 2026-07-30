"""Peak／RMSレベルとPeak holdの純粋ロジック。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。

ここで扱うのは `QAudioBufferOutput` が渡す**デコード済みPCM**であり、
速度・ピッチ処理と音量・ミュートを適用する**前**の信号レベルである。
出力音量計ではない。LUFS、true peak、inter-sample peak、K特性は扱わない。
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

LEVEL_DB_FLOOR = -90.0
"""表示下限（dBFS）。スペクトラムの下限と揃える。"""

PEAK_HOLD_SECONDS = 1.0
"""新しいPeakへ到達してから減衰を始めるまでの保持時間（秒）。"""

PEAK_HOLD_RELEASE_DB_PER_SECOND = 24.0
"""保持時間後のPeak holdの減衰速度（dB/秒）。実時間基準で減衰させる。"""

LEVEL_WINDOW_SIZE = 4096
"""Peak／RMSを求める窓長（sample）。

FFT長と同じ4096sampleとする（48kHzで約85ms）。30FPS表示に対して十分で、
リングバッファ2秒分すべてを毎tick計算しない。
"""

_AMPLITUDE_EPSILON = 1e-12
"""log(0) を避けるための下限。-240dB相当でfloorへclampされる。"""

_ORDER_TOLERANCE_DB = 1e-6
"""RMS ≦ Peak ≦ Peak hold の検証に用いる丸め誤差の許容幅。"""


@dataclass(frozen=True, slots=True)
class StereoLevelFrame:
    """1フレーム分の左右レベル（すべてdBFS）。

    契約:

    - boolを数値として受理しない。
    - すべて有限で、:data:`LEVEL_DB_FLOOR` 以上 0dB 以下。
    - RMS ≦ Peak ≦ Peak hold（丸め誤差の範囲で検証する）。
    - QColorや時刻オブジェクト、NumPy配列は保持しない。
    """

    left_peak_db: float
    right_peak_db: float
    left_rms_db: float
    right_rms_db: float
    left_peak_hold_db: float
    right_peak_hold_db: float

    def __post_init__(self) -> None:
        for name in (
            "left_peak_db",
            "right_peak_db",
            "left_rms_db",
            "right_rms_db",
            "left_peak_hold_db",
            "right_peak_hold_db",
        ):
            object.__setattr__(self, name, _validated_db(name, getattr(self, name)))
        for side, rms, peak, hold in (
            ("左", self.left_rms_db, self.left_peak_db, self.left_peak_hold_db),
            ("右", self.right_rms_db, self.right_peak_db, self.right_peak_hold_db),
        ):
            if rms > peak + _ORDER_TOLERANCE_DB:
                raise ValueError(f"{side}チャンネルのRMSがPeakを超えています")
            if hold < peak - _ORDER_TOLERANCE_DB:
                raise ValueError(f"{side}チャンネルのPeak holdがPeakを下回っています")


def silent_level_frame(db_floor: float = LEVEL_DB_FLOOR) -> StereoLevelFrame:
    """無音（全chがfloor）のフレーム。"""
    floor = _validated_db("db_floor", db_floor)
    return StereoLevelFrame(
        left_peak_db=floor,
        right_peak_db=floor,
        left_rms_db=floor,
        right_rms_db=floor,
        left_peak_hold_db=floor,
        right_peak_hold_db=floor,
    )


def peak_amplitude(samples: NDArray[np.float32]) -> float:
    """絶対値の最大（linear）。空入力は0とする。"""
    _validate_samples(samples)
    if samples.size == 0:
        return 0.0
    return float(np.abs(samples, dtype=np.float64).max())


def rms_amplitude(samples: NDArray[np.float32]) -> float:
    """二乗平均平方根（linear）。空入力は0とする。

    float32の二乗和では精度が落ちるため float64 へ昇格して計算し、
    **入力配列は変更しない**（read-onlyのsnapshotをそのまま渡せる）。
    """
    _validate_samples(samples)
    if samples.size == 0:
        return 0.0
    squares = np.square(samples, dtype=np.float64)
    return float(np.sqrt(squares.mean()))


def amplitude_to_dbfs(amplitude: float, db_floor: float = LEVEL_DB_FLOOR) -> float:
    """linear振幅をdBFSへ変換し、floor〜0dBへclampする。

    1.0 が 0dB、0.5 が約 -6.02dB、0 は floor。
    """
    floor = _validated_db("db_floor", db_floor)
    if isinstance(amplitude, bool) or not np.isfinite(amplitude):
        raise ValueError("amplitudeは有限の数値である必要があります")
    value = 20.0 * float(np.log10(max(float(abs(amplitude)), _AMPLITUDE_EPSILON)))
    return min(0.0, max(floor, value))


class _PeakHold:
    """1チャンネル分のPeak hold。保持時間と減衰を実時間で管理する。"""

    def __init__(self, db_floor: float, hold_seconds: float, release_db_per_second: float) -> None:
        self._db_floor = db_floor
        self._hold_seconds = hold_seconds
        self._release_db_per_second = release_db_per_second
        # 「まだPeakを観測していない」状態をNoneで表す。
        self._hold_db: float | None = None
        self._elapsed_seconds = 0.0

    def reset(self) -> None:
        self._hold_db = None
        self._elapsed_seconds = 0.0

    def updated(self, peak_db: float, elapsed_seconds: float) -> float:
        """新しいPeakと経過秒からholdを更新して返す。

        現在Peakがholdより高ければ即時追従して保持時間を測り直す。保持時間を
        過ぎたぶんだけ実時間基準で減衰させ、現在Peakとfloorより下へは落とさない。
        減衰量は経過時間に比例するため、1tickで2秒進めた場合と2tickで1秒ずつ
        進めた場合が同じ結果になる（タイマーFPSの揺れに依存しない）。
        """
        previous = self._hold_db
        if previous is None or peak_db >= previous:
            self._elapsed_seconds = 0.0
            self._hold_db = peak_db
            return peak_db

        started = self._elapsed_seconds
        self._elapsed_seconds = started + elapsed_seconds
        # 今回のtickのうち、保持時間を過ぎていた秒数だけを減衰へ使う。
        decay_seconds = max(0.0, self._elapsed_seconds - max(self._hold_seconds, started))
        decayed = previous - self._release_db_per_second * decay_seconds
        if peak_db >= decayed:
            # 減衰線が現在Peakへ追いついた時点を、新しいholdの獲得として扱う。
            self._elapsed_seconds = 0.0
            self._hold_db = peak_db
            return peak_db
        held = max(self._db_floor, decayed)
        self._hold_db = held
        return held


class LevelProcessor:
    """Peak holdを実時間基準で保持・減衰させる状態クラス。

    ``QElapsedTimer`` はPanel側が持ち、ここへは経過秒だけを渡す
    （タイマーFPSの揺れで減衰速度が変わらないようにするため）。
    """

    def __init__(
        self,
        *,
        db_floor: float = LEVEL_DB_FLOOR,
        hold_seconds: float = PEAK_HOLD_SECONDS,
        release_db_per_second: float = PEAK_HOLD_RELEASE_DB_PER_SECOND,
    ) -> None:
        self._db_floor = _validated_db("db_floor", db_floor)
        if self._db_floor >= 0.0:
            raise ValueError("db_floorは負の値である必要があります")
        self._hold_seconds = _validated_positive("hold_seconds", hold_seconds, allow_zero=True)
        self._release_db_per_second = _validated_positive(
            "release_db_per_second", release_db_per_second
        )
        # holdは左右で独立に保持・減衰させる（片chのPeakが他chのholdを延命しない）。
        self._left_hold = _PeakHold(self._db_floor, self._hold_seconds, self._release_db_per_second)
        self._right_hold = _PeakHold(
            self._db_floor, self._hold_seconds, self._release_db_per_second
        )

    @property
    def db_floor(self) -> float:
        return self._db_floor

    @property
    def hold_seconds(self) -> float:
        return self._hold_seconds

    @property
    def release_db_per_second(self) -> float:
        return self._release_db_per_second

    @property
    def window_size(self) -> int:
        return LEVEL_WINDOW_SIZE

    def reset(self) -> None:
        """Peak holdと経過時間を捨てる。stop・source変更・sample rate変更で呼ぶ。"""
        self._left_hold.reset()
        self._right_hold.reset()

    def process(
        self,
        left: NDArray[np.float32],
        right: NDArray[np.float32],
        *,
        elapsed_seconds: float,
    ) -> StereoLevelFrame:
        """左右のPCMからPeak／RMS／Peak holdを求める（入力配列は変更しない）。

        ``elapsed_seconds`` は前回 ``process`` からの実経過秒。pause中は呼ばれない
        ため、Peak holdの時間も進まない。
        """
        elapsed = _validated_elapsed(elapsed_seconds)
        left_peak_db = amplitude_to_dbfs(peak_amplitude(left), self._db_floor)
        right_peak_db = amplitude_to_dbfs(peak_amplitude(right), self._db_floor)
        left_rms_db = amplitude_to_dbfs(rms_amplitude(left), self._db_floor)
        right_rms_db = amplitude_to_dbfs(rms_amplitude(right), self._db_floor)
        left_hold_db = self._left_hold.updated(left_peak_db, elapsed)
        right_hold_db = self._right_hold.updated(right_peak_db, elapsed)
        return StereoLevelFrame(
            left_peak_db=left_peak_db,
            right_peak_db=right_peak_db,
            left_rms_db=left_rms_db,
            right_rms_db=right_rms_db,
            left_peak_hold_db=left_hold_db,
            right_peak_hold_db=right_hold_db,
        )


def _validated_db(name: str, value: float) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name}にboolは使えません")
    number = float(value)
    if not np.isfinite(number):
        raise ValueError(f"{name}は有限値である必要があります")
    if not LEVEL_DB_FLOOR <= number <= 0.0:
        raise ValueError(f"{name}は{LEVEL_DB_FLOOR}dB以上0dB以下である必要があります")
    return number


def _validated_positive(name: str, value: float, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name}にboolは使えません")
    number = float(value)
    if not np.isfinite(number) or (number < 0.0 if allow_zero else number <= 0.0):
        raise ValueError(f"{name}は{'0以上' if allow_zero else '正'}の有限値である必要があります")
    return number


def _validated_elapsed(value: float) -> float:
    if isinstance(value, bool):
        raise TypeError("elapsed_secondsにboolは使えません")
    number = float(value)
    if not np.isfinite(number) or number < 0.0:
        raise ValueError("elapsed_secondsは0以上の有限値である必要があります")
    return number


def _validate_samples(samples: NDArray[np.float32]) -> None:
    # 型注釈はNDArrayだが、呼び出し側の実行時の誤りも表面化させる。
    if not isinstance(samples, np.ndarray):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("samplesはNumPy配列である必要があります")
    if samples.dtype != np.dtype(np.float32):
        raise TypeError("samplesのdtypeはfloat32である必要があります")
    if samples.ndim != 1:
        raise ValueError("samplesは1次元である必要があります")
    if samples.size and not bool(np.all(np.isfinite(samples))):
        raise ValueError("samplesにNaNまたはinfが含まれています")

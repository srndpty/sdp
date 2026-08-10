"""ステレオL／Rの関係を可視化する純粋ロジック（ベクトルスコープと位相相関）。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。

ここで扱うのは ``QAudioBufferOutput`` が渡す**デコード済みPCM**であり、
速度・ピッチ処理と音量・ミュートを適用する**前**の信号である。
DAWのゴニオメーター／フェーズメーターに相当する表示のための幾何・統計だけを担い、
QColorや描画情報は持たない。
"""

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

VECTORSCOPE_WINDOW = 2_048
"""1フレームで参照する最新sample数。48kHzで約43ms。"""

VECTORSCOPE_MAX_POINTS = 1_024
"""描画する最大点数。これを超える場合は等間隔で間引く（点が潰れないため）。"""

CORRELATION_WINDOW = 4_096
"""位相相関を求める窓長。レベルメーターと揃えて約85ms（48kHz）。"""

CORRELATION_ATTACK = 0.5
"""相関が現在値へ近づくときの平滑化係数（大きいほど速い）。"""

CORRELATION_RELEASE = 0.2
"""相関が現在値から離れるときの平滑化係数（小さいほど滑らか）。"""

_ENERGY_EPSILON = 1e-12
"""無音判定に用いるエネルギーの下限。0除算を避ける。"""


@dataclass(frozen=True, slots=True)
class VectorscopeFrame:
    """ゴニオメーター表示用の点群。read-onlyで色や座標変換情報を持たない。

    ``x`` は左右差（逆相成分）、``y`` は左右和（同相成分）を45度回転した座標で、
    いずれも -1.0〜1.0。モノラル音源では ``x`` が0付近に集まり縦線になる。
    """

    x: NDArray[np.float32]
    y: NDArray[np.float32]

    def __post_init__(self) -> None:
        x = _validated_array("x", self.x)
        y = _validated_array("y", self.y)
        if x.shape != y.shape:
            raise ValueError("xとyのshapeが一致しません")
        x = x.copy()
        y = y.copy()
        x.setflags(write=False)
        y.setflags(write=False)
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)

    @property
    def point_count(self) -> int:
        return int(self.x.size)


def empty_vectorscope_frame() -> VectorscopeFrame:
    """点を持たない空フレーム（無音・停止時）。"""
    return VectorscopeFrame(
        x=np.empty(0, dtype=np.float32),
        y=np.empty(0, dtype=np.float32),
    )


def compute_vectorscope(
    left: NDArray[np.float32],
    right: NDArray[np.float32],
    *,
    max_points: int = VECTORSCOPE_MAX_POINTS,
) -> VectorscopeFrame:
    """L／Rから45度回転したLissajousのX/Y点群を求める（入力配列は変更しない）。

    ``x = (R - L) / 2``、``y = (L + R) / 2`` とし、L／Rが-1〜1なら結果も-1〜1へ
    収まる。点数が ``max_points`` を超える場合は等間隔で間引く。
    """
    if max_points < 1:
        raise ValueError("max_pointsは1以上である必要があります")
    left = _validated_channel("left", left)
    right = _validated_channel("right", right)
    if left.shape != right.shape:
        raise ValueError("leftとrightのshapeが一致しません")
    if left.size == 0:
        return empty_vectorscope_frame()

    if left.size > max_points:
        stride = math.ceil(left.size / max_points)
        left = left[::stride]
        right = right[::stride]

    x = np.clip((right - left) * 0.5, -1.0, 1.0).astype(np.float32)
    y = np.clip((left + right) * 0.5, -1.0, 1.0).astype(np.float32)
    return VectorscopeFrame(x=x, y=y)


def phase_correlation(left: NDArray[np.float32], right: NDArray[np.float32]) -> float:
    """L／Rの位相相関係数を -1.0〜1.0 で返す（入力配列は変更しない）。

    +1 は完全同相（モノラル）、0 は無相関（広いステレオ）、-1 は完全逆相
    （モノラル再生で打ち消し合う）。片チャンネルが無音のときは相関を定義できないため
    0.0 とする。float32の二乗和は精度が落ちるため float64 へ昇格して計算する。
    """
    left = _validated_channel("left", left)
    right = _validated_channel("right", right)
    if left.shape != right.shape:
        raise ValueError("leftとrightのshapeが一致しません")
    if left.size == 0:
        return 0.0
    left64 = left.astype(np.float64)
    right64 = right.astype(np.float64)
    energy = float(np.sum(left64 * left64)) * float(np.sum(right64 * right64))
    if energy <= _ENERGY_EPSILON:
        return 0.0
    correlation = float(np.sum(left64 * right64)) / math.sqrt(energy)
    return min(1.0, max(-1.0, correlation))


class CorrelationProcessor:
    """位相相関を時間平滑化する状態クラス。

    QWidgetへ平滑化状態を持たせないため、Panel側がこのProcessorを所有する。
    source変更・停止・sample rate変更では :meth:`reset` で履歴を捨てる。
    """

    def __init__(
        self,
        *,
        attack: float = CORRELATION_ATTACK,
        release: float = CORRELATION_RELEASE,
    ) -> None:
        self._attack = _validated_coefficient("attack", attack)
        self._release = _validated_coefficient("release", release)
        self._smoothed: float | None = None

    @property
    def window_size(self) -> int:
        return CORRELATION_WINDOW

    def reset(self) -> None:
        """平滑化履歴を捨てる。次フレームは生の相関値から始まる。"""
        self._smoothed = None

    def process(self, left: NDArray[np.float32], right: NDArray[np.float32]) -> float:
        """L／Rから平滑化済みの相関値を返す。"""
        current = phase_correlation(left, right)
        previous = self._smoothed
        if previous is None:
            self._smoothed = current
        else:
            coefficient = self._attack if abs(current) > abs(previous) else self._release
            self._smoothed = previous + coefficient * (current - previous)
        return min(1.0, max(-1.0, self._smoothed))


def _validated_channel(name: str, value: NDArray[np.float32]) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"{name}はNumPy配列である必要があります")
    if value.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if value.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if value.size and not bool(np.all(np.isfinite(value))):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    return value


def _validated_array(name: str, value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if not bool(np.all(np.isfinite(array))):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    if bool(np.any(array < -1.0)) or bool(np.any(array > 1.0)):
        raise ValueError(f"{name}が-1.0〜1.0の範囲外です")
    return array


def _validated_coefficient(name: str, value: float) -> float:
    if isinstance(value, bool) or not np.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"{name}は0より大きく1以下である必要があります")
    return float(value)

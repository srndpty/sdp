"""オシロスコープ（生波形のリアルタイム表示）の純粋ロジック。

Qt非依存。GUIのタイマーから呼ばれ、音声コールバックからは呼ばれない
（[architecture.md](../../../../docs/architecture.md) §6）。

追従波形（``waveform.py`` の min/max envelope）とは異なり、こちらは
**瞬時の波形そのもの**を短い窓で表示する。周期波形が左右に流れて見えないよう、
立ち上がりゼロ交差でトリガーして表示位置を安定させる。
"""

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray

OSCILLOSCOPE_WINDOW = 2_048
"""表示する窓長（sample）。48kHzで約43ms。"""


@dataclass(frozen=True, slots=True)
class OscilloscopeFrame:
    """1フレーム分の表示波形。read-onlyで色や座標変換情報を持たない。

    ``samples`` は -1.0〜1.0 の float32。長さは要求した窓長で固定し、
    起動直後や無音でも同じshapeを返す（左0 padding）。
    """

    samples: NDArray[np.float32]

    def __post_init__(self) -> None:
        samples = _validated_array("samples", self.samples)
        samples = samples.copy()
        samples.setflags(write=False)
        object.__setattr__(self, "samples", samples)

    @property
    def sample_count(self) -> int:
        return int(self.samples.size)


def silent_oscilloscope_frame(window: int = OSCILLOSCOPE_WINDOW) -> OscilloscopeFrame:
    """全0の無音フレーム。"""
    if window < 1:
        raise ValueError("windowは1以上である必要があります")
    return OscilloscopeFrame(samples=np.zeros(window, dtype=np.float32))


def compute_oscilloscope(
    samples: NDArray[np.float32],
    *,
    window: int = OSCILLOSCOPE_WINDOW,
) -> OscilloscopeFrame:
    """立ち上がりゼロ交差でトリガーした表示波形を返す（入力配列は変更しない）。

    トリガー点が見つかればそこから ``window`` sampleを切り出し、周期波形が
    静止して見えるようにする。見つからなければ最新 ``window`` sampleを使う。
    保持数が窓長に満たない場合は左を0で埋める。
    """
    if window < 1:
        raise ValueError("windowは1以上である必要があります")
    samples = _validated_array("samples", samples)
    if samples.size == 0:
        return silent_oscilloscope_frame(window)

    trigger = _find_rising_trigger(samples, window)
    segment = samples[trigger : trigger + window]
    if segment.size == window:
        return OscilloscopeFrame(samples=segment.astype(np.float32, copy=True))

    # 窓長に満たない場合は左0 paddingで固定shapeにする。
    fitted = np.zeros(window, dtype=np.float32)
    fitted[window - segment.size :] = segment
    return OscilloscopeFrame(samples=fitted)


def _find_rising_trigger(samples: NDArray[np.float32], window: int) -> int:
    """先頭側の探索範囲から最初の立ち上がりゼロ交差indexを返す。

    ``window`` sampleを切り出せる範囲だけを探索する。見つからなければ、
    最新 ``window`` sampleの開始indexへフォールバックする。
    """
    searchable = samples.size - window
    if searchable <= 0:
        return max(0, samples.size - window)
    # 探索は前半に限る（後半で見つけると表示がほとんど動かないため）。
    limit = min(searchable, max(1, samples.size // 2))
    head = samples[: limit + 1]
    rising = (head[:-1] <= 0.0) & (head[1:] > 0.0)
    indices = np.flatnonzero(rising)
    if indices.size:
        return int(indices[0])
    return searchable


def _validated_array(name: str, value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    array = cast("NDArray[np.float32]", value)  # dtypeは直後に検証する
    if array.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if array.size and not bool(np.all(np.isfinite(array))):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    return array

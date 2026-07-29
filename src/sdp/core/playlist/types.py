"""プレイリスト再生の Qt 非依存な型。

UI からも参照するため、PlaylistPlaybackController の内部へ閉じ込めない。
表示文字列は UI 側の責務なのでここには置かない。
"""

from enum import Enum, auto


class RepeatMode(Enum):
    """繰り返し再生の種類。

    永続化しないため、数値との対応を固定する IntEnum にはしない
    （P2-C2 の時点でリピート設定は保存せず、起動のたびに ``OFF`` へ戻る）。
    """

    OFF = auto()
    ALL = auto()
    ONE = auto()


REPEAT_MODE_CYCLE: tuple[RepeatMode, ...] = (RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE)
"""ボタン操作で切り替わる順序（OFF → ALL → ONE → OFF）。"""


def next_repeat_mode(mode: RepeatMode) -> RepeatMode:
    """切替順で次のモードを返す。"""
    index = REPEAT_MODE_CYCLE.index(mode)
    return REPEAT_MODE_CYCLE[(index + 1) % len(REPEAT_MODE_CYCLE)]

"""保存ファイルの復元失敗・保存失敗をユーザーへ短く伝えるための共通部品。

対象は settings.json / playlist.json / ui-state.json の3つ。
**生の例外文とファイルパスはユーザーへ見せない**（ログだけへ残す）。
メッセージ整形はQt非依存の純粋関数とし、通知の抑制だけをQObjectで扱う。
"""

import logging
from collections.abc import Iterable, Sequence
from enum import Enum

from PySide6.QtCore import QObject, Signal, SignalInstance

_logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 60
"""ステータスバーへ収める上限の目安（全角換算で読み切れる長さ）。"""


class SaveCategory(Enum):
    """保存ファイルの種類。UI表示名だけを持ち、パスもschemaも持たない。"""

    SETTINGS = "設定"
    PLAYLIST = "プレイリスト"
    UI_STATE = "ウィンドウ状態"

    @property
    def label(self) -> str:
        return self.value


_CATEGORY_ORDER: tuple[SaveCategory, ...] = (
    SaveCategory.SETTINGS,
    SaveCategory.PLAYLIST,
    SaveCategory.UI_STATE,
)


def restore_failure_message(categories: Iterable[SaveCategory]) -> str | None:
    """復元に失敗したカテゴリをまとめた1文を返す（無ければ ``None``）。

    単純な文字列連結だと複数破損で読みにくくなるため、カテゴリ名を「と」で連ね、
    重複は取り除いて既定の順序へ揃える。
    """
    ordered = _ordered_unique(categories)
    if not ordered:
        return None
    names = "と".join(category.label for category in ordered)
    return f"{names}の復元に失敗しました。既定状態で起動します。"


def save_failure_message(category: SaveCategory) -> str:
    """保存に失敗したことだけを伝える短い通知（原因はログへ）。"""
    return f"{category.label}を保存できませんでした。"


def save_recovered_message(category: SaveCategory) -> str:
    """再試行で保存できたことを伝える短い通知。"""
    return f"{category.label}を保存しました。"


def _ordered_unique(categories: Iterable[SaveCategory]) -> Sequence[SaveCategory]:
    seen = set(categories)
    unknown = seen - set(_CATEGORY_ORDER)
    if unknown:
        raise TypeError(f"SaveCategoryを指定してください: {sorted(map(str, unknown))}")
    return [category for category in _CATEGORY_ORDER if category in seen]


class SaveStatusReporter(QObject):
    """保存の失敗・復旧を、カテゴリごとに**状態が変わったときだけ**通知する。

    デバウンス保存はタイマーごとに失敗し得るため、同じ失敗を出し続けない。
    modal dialogは出さず、ステータスバー向けの短い文字列を1本のSignalで流す。
    """

    message_requested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._failed: set[SaveCategory] = set()

    @property
    def failed_categories(self) -> frozenset[SaveCategory]:
        """現在「保存できていない」と通知済みのカテゴリ。"""
        return frozenset(self._failed)

    def watch(
        self,
        category: SaveCategory,
        save_failed: SignalInstance,
        save_recovered: SignalInstance,
    ) -> None:
        """1カテゴリ分の保存結果Signalを購読する。"""
        save_failed.connect(lambda: self.report_failure(category))
        save_recovered.connect(lambda: self.report_recovery(category))

    def report_failure(self, category: SaveCategory) -> None:
        """保存に失敗した。既に通知済みなら黙る（連続失敗で溢れさせない）。"""
        if category in self._failed:
            return
        self._failed.add(category)
        _logger.debug("%sの保存失敗を通知します", category.label)
        self.message_requested.emit(save_failure_message(category))

    def report_recovery(self, category: SaveCategory) -> None:
        """保存できた。直前に失敗を通知していた場合だけ復旧を伝える。"""
        if category not in self._failed:
            return
        self._failed.discard(category)
        self.message_requested.emit(save_recovered_message(category))

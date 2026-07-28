"""プレイリストの Qt モデル。

行データ、追加・挿入・削除・移動、欠損状態だけを扱う。
現在再生中のエントリ、再生位置、リピート、シャッフル、メタデータ、
保存先のパスは持たない（それぞれ Controller・設定・永続化の責務）。
"""

from collections.abc import Iterable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from sdp.core.playlist.entry import FileStatus, PlaylistEntry, create_entry

ENTRY_ID_ROLE = int(Qt.ItemDataRole.UserRole)
"""エントリの entry_id（``str``）を取得する role。"""

PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
"""エントリの絶対パス（``Path``）を取得する role。"""

FILE_STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 2
"""エントリのファイル状態（:class:`FileStatus`）を取得する role。"""


_ROOT = QModelIndex()
"""無効（＝ルート）を表す親インデックス。

テーブルモデルの親は常にルートであり、無効な QModelIndex は不変の値のため
モジュール変数として共有してよい（引数既定値での毎回の生成を避ける）。
"""


class Column(IntEnum):
    """列。タイトル・アーティスト等のメタデータ列は P2-D で追加する。"""

    NAME = 0
    PATH = 1


_HEADERS: dict[Column, str] = {
    Column.NAME: "ファイル名",
    Column.PATH: "パス",
}


class PlaylistModel(QAbstractTableModel):
    """プレイリストの順序付きエントリを保持する ``QAbstractTableModel``。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[PlaylistEntry] = []
        self._row_by_entry_id: dict[str, int] = {}

    # -- 読み取り -----------------------------------------------------------

    def entries(self) -> tuple[PlaylistEntry, ...]:
        """全エントリの読み取り専用スナップショット（内部リストは公開しない）。"""
        return tuple(self._entries)

    def entry_at(self, row: int) -> PlaylistEntry:
        """行のエントリを返す。範囲外は ``IndexError``（呼び出し側のバグを隠さない）。"""
        if not 0 <= row < len(self._entries):
            raise IndexError(f"行が範囲外です: {row}")
        return self._entries[row]

    def row_of_entry_id(self, entry_id: str) -> int | None:
        """entry_id の行番号。存在しなければ ``None``。"""
        return self._row_by_entry_id.get(entry_id)

    # -- QAbstractTableModel ------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _ROOT) -> int:
        return 0 if parent.isValid() else len(Column)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == ENTRY_ID_ROLE:
            return entry.entry_id
        if role == PATH_ROLE:
            return entry.path
        if role == FILE_STATUS_ROLE:
            return entry.file_status
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return str(entry.path)
        if role == int(Qt.ItemDataRole.DisplayRole):
            if index.column() == Column.NAME:
                return entry.display_name
            if index.column() == Column.PATH:
                return str(entry.path)
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        if role != int(Qt.ItemDataRole.DisplayRole):
            return None
        if orientation is Qt.Orientation.Horizontal:
            if section in tuple(Column):
                return _HEADERS[Column(section)]
            return None
        if 0 <= section < len(self._entries):
            return section + 1
        return None

    def removeRows(
        self, row: int, count: int, parent: QModelIndex | QPersistentModelIndex = _ROOT
    ) -> bool:
        if parent.isValid() or count <= 0 or row < 0 or row + count > len(self._entries):
            return False
        self.beginRemoveRows(_ROOT, row, row + count - 1)
        del self._entries[row : row + count]
        self._rebuild_index()
        self.endRemoveRows()
        return True

    def moveRows(
        self,
        sourceParent: QModelIndex | QPersistentModelIndex,
        sourceRow: int,
        count: int,
        destinationParent: QModelIndex | QPersistentModelIndex,
        destinationChild: int,
    ) -> bool:
        """連続する行を移動する。``destinationChild`` は移動前の行番号で指定する。"""
        if sourceParent.isValid() or destinationParent.isValid():
            return False
        total = len(self._entries)
        if count <= 0 or sourceRow < 0 or sourceRow + count > total:
            return False
        if not 0 <= destinationChild <= total:
            return False
        # 移動元の範囲内（および直後）への移動は何も変えないため受け付けない。
        if sourceRow <= destinationChild <= sourceRow + count:
            return False
        if not self.beginMoveRows(_ROOT, sourceRow, sourceRow + count - 1, _ROOT, destinationChild):
            return False
        moving = self._entries[sourceRow : sourceRow + count]
        del self._entries[sourceRow : sourceRow + count]
        insert_at = destinationChild if destinationChild < sourceRow else destinationChild - count
        self._entries[insert_at:insert_at] = moving
        self._rebuild_index()
        self.endMoveRows()
        return True

    # -- 変更 ---------------------------------------------------------------

    def add_paths(self, paths: Iterable[Path]) -> tuple[str, ...]:
        """パス群を末尾へ一括追加し、新しい entry_id を順に返す。

        同じパスの重複追加は許可する（PL-07）。拡張子で拒否しない。
        """
        return self.insert_paths(len(self._entries), paths)

    def insert_paths(self, row: int, paths: Iterable[Path]) -> tuple[str, ...]:
        """パス群を指定行へ一括挿入し、新しい entry_id を順に返す。"""
        entries = [create_entry(path) for path in paths]
        self.insert_entries(row, entries)
        return tuple(entry.entry_id for entry in entries)

    def add_entries(self, entries: Iterable[PlaylistEntry]) -> None:
        """既存のエントリ群を末尾へ一括追加する。"""
        self.insert_entries(len(self._entries), entries)

    def insert_entries(self, row: int, entries: Iterable[PlaylistEntry]) -> None:
        """エントリ群を指定行へ一括挿入する。

        entry_id が既存または追加分の中で重複する場合は ``ValueError``。
        同一性が壊れると現在曲の追跡が破綻するため、暗黙に採番し直さない。
        """
        new_entries = list(entries)
        if not 0 <= row <= len(self._entries):
            raise IndexError(f"挿入位置が範囲外です: {row}")
        self._reject_duplicate_entry_ids(new_entries)
        if not new_entries:
            return
        self.beginInsertRows(_ROOT, row, row + len(new_entries) - 1)
        self._entries[row:row] = new_entries
        self._rebuild_index()
        self.endInsertRows()

    def clear(self) -> None:
        """全消去。"""
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self._row_by_entry_id.clear()
        self.endResetModel()

    def replace_entries(self, entries: Sequence[PlaylistEntry]) -> None:
        """永続化から復元したエントリ群で全体を置き換える。"""
        new_entries = list(entries)
        self._reject_duplicate_entry_ids(new_entries, replacing=True)
        self.beginResetModel()
        self._entries = new_entries
        self._rebuild_index()
        self.endResetModel()

    def refresh_file_status(self) -> int:
        """全行のファイル状態を再確認し、変化した行数を返す。

        ファイルが削除・復元された場合に呼ぶ。変化した行だけ ``dataChanged`` を出す。
        """
        changed = 0
        last_column = len(Column) - 1
        for row, entry in enumerate(self._entries):
            refreshed = entry.with_refreshed_status()
            if refreshed is entry:
                continue
            self._entries[row] = refreshed
            changed += 1
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, last_column),
                [FILE_STATUS_ROLE],
            )
        return changed

    def missing_entry_ids(self) -> tuple[str, ...]:
        """欠損しているエントリの entry_id。"""
        return tuple(
            entry.entry_id for entry in self._entries if entry.file_status is FileStatus.MISSING
        )

    # -- 内部 ---------------------------------------------------------------

    def _rebuild_index(self) -> None:
        self._row_by_entry_id = {entry.entry_id: row for row, entry in enumerate(self._entries)}

    def _reject_duplicate_entry_ids(
        self, new_entries: Sequence[PlaylistEntry], *, replacing: bool = False
    ) -> None:
        seen: set[str] = set() if replacing else set(self._row_by_entry_id)
        for entry in new_entries:
            if entry.entry_id in seen:
                raise ValueError(f"entry_id が重複しています: {entry.entry_id}")
            seen.add(entry.entry_id)

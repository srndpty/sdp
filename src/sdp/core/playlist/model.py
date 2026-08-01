"""プレイリストの Qt モデル。

行データ、追加・挿入・削除・移動、欠損状態だけを扱う。
現在再生中のエントリ、再生位置、リピート、シャッフル、メタデータ、
保存先のパスは持たない（それぞれ Controller・設定・永続化の責務）。
"""

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

from sdp.core.metadata.types import (
    MetadataStatus,
    TrackMetadata,
    format_bitrate,
    format_duration_ms,
    format_file_size,
)
from sdp.core.playlist.entry import FileStatus, PlaylistEntry, create_entry

_logger = logging.getLogger(__name__)

INTERNAL_MIME_TYPE = "application/x-sdp-playlist-entry-ids"
"""内部並べ替え用の MIME 型。

中身は entry_id の JSON 配列（UTF-8）。パスや行番号ではなく entry_id を運ぶ。
パスは重複しうるし、行番号はドラッグ中に変化しうるため、
行の安定した同一性は entry_id だけだという理由による。
"""

URI_LIST_MIME_TYPE = "text/uri-list"
"""外部（Explorer など）からのファイル D&D で受け取る MIME 型。"""

ENTRY_ID_ROLE = int(Qt.ItemDataRole.UserRole)
"""エントリの entry_id（``str``）を取得する role。"""

PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1
"""エントリの絶対パス（``Path``）を取得する role。"""

FILE_STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 2
"""エントリのファイル状態（:class:`FileStatus`）を取得する role。"""

TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 3
"""メタデータのタイトル（``str | None``）。表示用のフォールバックはしない。"""

ARTIST_ROLE = int(Qt.ItemDataRole.UserRole) + 4
"""メタデータのアーティスト（``str | None``）。"""

ALBUM_ROLE = int(Qt.ItemDataRole.UserRole) + 5
"""メタデータのアルバム（``str | None``）。"""

DURATION_MS_ROLE = int(Qt.ItemDataRole.UserRole) + 6
"""メタデータの長さ（``int | None``、ミリ秒）。整形しない生の値。"""

METADATA_STATUS_ROLE = int(Qt.ItemDataRole.UserRole) + 7
"""メタデータの読み込み状態（:class:`MetadataStatus`）。"""

FILE_SIZE_BYTES_ROLE = int(Qt.ItemDataRole.UserRole) + 8
"""ファイルサイズ（``int | None``、バイト）。"""

BITRATE_BPS_ROLE = int(Qt.ItemDataRole.UserRole) + 9
"""ビットレート（``int | None``、bit/s）。"""

METADATA_FAILED_TOOLTIP = "メタデータを読み取れませんでした。"
"""読み取り失敗のツールチップ。例外文字列やトレースバックは出さない。"""

METADATA_ROLES: tuple[int, ...] = (
    TITLE_ROLE,
    ARTIST_ROLE,
    ALBUM_ROLE,
    DURATION_MS_ROLE,
    FILE_SIZE_BYTES_ROLE,
    BITRATE_BPS_ROLE,
    METADATA_STATUS_ROLE,
)
"""メタデータ由来の role。再生制御はこれだけの変化では何もしない。"""


_ROOT = QModelIndex()
"""無効（＝ルート）を表す親インデックス。

テーブルモデルの親は常にルートであり、無効な QModelIndex は不変の値のため
モジュール変数として共有してよい（引数既定値での毎回の生成を避ける）。
"""


class Column(IntEnum):
    """列。

    先頭列はメタデータのタイトルを表示し、取得できていなければファイル名へ
    フォールバックする。``NAME`` は P2-B までの別名として残す
    （同じ列を「ファイル名」と「タイトル」で二重に並べない）。
    """

    TITLE = 0
    NAME = 0
    FILE_SIZE = 1
    BITRATE = 2
    DURATION = 3
    PATH = 4


_HEADERS: dict[Column, str] = {
    Column.TITLE: "タイトル",
    Column.FILE_SIZE: "サイズ",
    Column.BITRATE: "ビットレート",
    Column.DURATION: "長さ",
    Column.PATH: "パス",
}

_METADATA_COLUMNS = (Column.TITLE, Column.FILE_SIZE, Column.BITRATE, Column.DURATION)
"""メタデータで表示が変わる列の範囲（先頭から長さまで）。"""


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

    def roleNames(self) -> dict[int, QByteArray]:
        """カスタムroleの安定名を返す。"""
        roles = super().roleNames()
        roles.update(
            {
                ENTRY_ID_ROLE: QByteArray(b"entryId"),
                PATH_ROLE: QByteArray(b"path"),
                FILE_STATUS_ROLE: QByteArray(b"fileStatus"),
                TITLE_ROLE: QByteArray(b"title"),
                ARTIST_ROLE: QByteArray(b"artist"),
                ALBUM_ROLE: QByteArray(b"album"),
                DURATION_MS_ROLE: QByteArray(b"durationMs"),
                FILE_SIZE_BYTES_ROLE: QByteArray(b"fileSizeBytes"),
                BITRATE_BPS_ROLE: QByteArray(b"bitrateBps"),
                METADATA_STATUS_ROLE: QByteArray(b"metadataStatus"),
            }
        )
        return roles

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
        if role == TITLE_ROLE:
            return None if entry.metadata is None else entry.metadata.title
        if role == ARTIST_ROLE:
            return None if entry.metadata is None else entry.metadata.artist
        if role == ALBUM_ROLE:
            return None if entry.metadata is None else entry.metadata.album
        if role == DURATION_MS_ROLE:
            return None if entry.metadata is None else entry.metadata.duration_ms
        if role == FILE_SIZE_BYTES_ROLE:
            return None if entry.metadata is None else entry.metadata.file_size_bytes
        if role == BITRATE_BPS_ROLE:
            return None if entry.metadata is None else entry.metadata.bitrate_bps
        if role == METADATA_STATUS_ROLE:
            return entry.metadata_status
        if role == int(Qt.ItemDataRole.ToolTipRole):
            if entry.metadata_status is MetadataStatus.FAILED:
                return f"{entry.path}\n{METADATA_FAILED_TOOLTIP}"
            return str(entry.path)
        if role == int(Qt.ItemDataRole.DisplayRole):
            return self._display_text(entry, index.column())
        return None

    def _display_text(self, entry: PlaylistEntry, column: int) -> str | None:
        """表示文字列。タイトルは常にファイル名へフォールバックする。"""
        if column == Column.TITLE:
            return entry.display_title
        if column == Column.PATH:
            return str(entry.path)
        metadata = entry.metadata
        if metadata is None:
            return "" if column in _METADATA_COLUMNS else None
        if column == Column.FILE_SIZE:
            return (
                ""
                if metadata.file_size_bytes is None
                else format_file_size(metadata.file_size_bytes)
            )
        if column == Column.BITRATE:
            return "" if metadata.bitrate_bps is None else format_bitrate(metadata.bitrate_bps)
        if column == Column.DURATION:
            return "" if metadata.duration_ms is None else format_duration_ms(metadata.duration_ms)
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

    # -- ドラッグ＆ドロップ -------------------------------------------------

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        """行はドラッグ可能・選択可能。ドロップはルート（行と行の間）だけで受ける。

        行自身をドロップ可能にすると「行の上へ落とす」解釈が生まれ、
        並べ替えの意味が曖昧になるため付けない。編集とチェックも許可しない。
        """
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return (
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        )

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    def supportedDropActions(self) -> Qt.DropAction:
        # 外部ドラッグ元へ Move 成功を返すと、元ファイルを削除される恐れがある。
        # 内部並べ替えも転送上は Copy とし、意味上の移動は _drop_internal で行う。
        return Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:
        return [INTERNAL_MIME_TYPE, URI_LIST_MIME_TYPE]

    def mimeData(self, indexes: Iterable[QModelIndex | QPersistentModelIndex]) -> QMimeData:
        """選択された行の entry_id を内部 MIME へ詰める。

        列ごとに index が渡るため行で重複を除き、現在の行順に並べる。
        """
        rows = sorted({index.row() for index in indexes if index.isValid()})
        entry_ids = [self._entries[row].entry_id for row in rows if 0 <= row < len(self._entries)]
        mime = QMimeData()
        mime.setData(INTERNAL_MIME_TYPE, QByteArray(json.dumps(entry_ids).encode("utf-8")))
        return mime

    def canDropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if action != Qt.DropAction.CopyAction or column not in (-1, 0):
            return False
        destination = self.drop_row(row, parent)
        if data.hasFormat(INTERNAL_MIME_TYPE):
            rows = self._parse_internal_mime(data, log_failure=False)
            if rows is None:
                return False
            # moveRows が拒否する no-op の位置ではドロップ表示も出さない。
            return not rows[0] <= destination <= rows[0] + len(rows)
        # 外部 URL はここでは軽い判定に留める。ドラッグ中に何度も呼ばれるため、
        # ディレクトリ判定などのファイルシステムアクセスは dropMimeData 側で行う。
        return data.hasUrls() and any(url.isLocalFile() for url in data.urls())

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        """ドロップを受け付ける。不正なデータでは例外を投げず ``False`` を返す。"""
        if action == Qt.DropAction.IgnoreAction:
            return True
        if action != Qt.DropAction.CopyAction or column not in (-1, 0):
            return False
        destination = self.drop_row(row, parent)
        if data.hasFormat(INTERNAL_MIME_TYPE):
            # 転送は Copy だが、内部 MIME の意味はプレイリスト行の移動。
            return self._drop_internal(data, destination)
        if data.hasUrls():
            return self._drop_urls(data, destination)
        return False

    def drop_row(self, row: int, parent: QModelIndex | QPersistentModelIndex) -> int:
        """ドロップ先の行番号を決める（決定はこの 1 か所へ集約する）。

        - 行の上（有効な parent）へのドロップ → その行の前
        - 行と行の間 → その位置
        - 最終行より下・空のプレイリスト（``row < 0``） → 末尾
        """
        if parent.isValid():
            return parent.row()
        if row < 0:
            return len(self._entries)
        return min(row, len(self._entries))

    def _parse_internal_mime(self, data: QMimeData, *, log_failure: bool) -> list[int] | None:
        """内部 MIME を現在の行番号の昇順リストへ変換する。

        壊れたデータ・未知の entry_id・非連続の選択では ``None`` を返す。
        非連続の複数行ドラッグは P2-B では対応せず、安全に拒否する
        （途中の行を巻き込まずに移動する意味を一意に決められないため）。
        """
        payload = bytes(data.data(INTERNAL_MIME_TYPE).data())
        try:
            parsed: object = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if log_failure:
                _logger.warning("内部 D&D の MIME データを解釈できません。")
            return None
        if not isinstance(parsed, list):
            if log_failure:
                _logger.warning("内部 D&D の MIME データが配列ではありません。")
            return None
        rows: list[int] = []
        for value in parsed:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(value, str):
                if log_failure:
                    _logger.warning("内部 D&D の entry_id が文字列ではありません。")
                return None
            row = self._row_by_entry_id.get(value)
            if row is None:
                if log_failure:
                    _logger.warning("内部 D&D に未知の entry_id が含まれています。")
                return None
            rows.append(row)
        if not rows:
            return None
        rows.sort()
        if rows[-1] - rows[0] + 1 != len(rows):
            if log_failure:
                _logger.warning("非連続の複数行ドラッグには対応していません。")
            return None
        return rows

    def _drop_internal(self, data: QMimeData, destination: int) -> bool:
        rows = self._parse_internal_mime(data, log_failure=True)
        if rows is None:
            return False
        return self.moveRows(_ROOT, rows[0], len(rows), _ROOT, destination)

    def _drop_urls(self, data: QMimeData, destination: int) -> bool:
        """ローカルファイルの URL だけを順序どおり挿入する。

        ディレクトリと非ローカル URL は無視する。拡張子では判定しない。
        ドロップ直前に消えていたファイルは、欠損エントリとして追加される
        （行を消さずに保持するという :mod:`sdp.core.playlist.entry` の契約に従う）。
        """
        paths: list[Path] = []
        for url in data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_dir():
                continue
            paths.append(path)
        if not paths:
            return False
        self.insert_paths(destination, paths)
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
        return sum(1 for row in range(len(self._entries)) if self._refresh_row(row))

    def refresh_entry_status(self, entry_id: str) -> bool:
        """1 件だけファイル状態を再確認し、変化したら ``True`` を返す。

        再生直前や次曲の探索で使う。全行を走査しないため 1000 件でも軽い。
        存在しない entry_id と、状態が変わらなかった場合は ``False``。
        entry_id と path は変えない。
        """
        row = self._row_by_entry_id.get(entry_id)
        if row is None:
            return False
        return self._refresh_row(row)

    # -- メタデータ ---------------------------------------------------------

    def mark_metadata_loading(self, entry_id: str) -> bool:
        """読み取り中にする。表示は変わらないため状態 role だけ通知する。"""
        return self._replace_entry(
            entry_id,
            lambda entry: entry.with_metadata_loading(),
            roles=[METADATA_STATUS_ROLE],
        )

    def apply_metadata(self, entry_id: str, metadata: TrackMetadata) -> bool:
        """読み取れたメタデータを反映する。"""
        return self._replace_entry(entry_id, lambda entry: entry.with_metadata(metadata))

    def mark_metadata_failed(self, entry_id: str) -> bool:
        """読み取り失敗にする。表示はファイル名フォールバックへ戻る。"""
        return self._replace_entry(entry_id, lambda entry: entry.with_metadata_failed())

    def clear_metadata(self, entry_id: str) -> bool:
        """メタデータを捨てて未要求へ戻す（再読み取り可能にする）。"""
        return self._replace_entry(entry_id, lambda entry: entry.without_metadata())

    def _replace_entry(
        self,
        entry_id: str,
        transform: Callable[[PlaylistEntry], PlaylistEntry],
        *,
        roles: list[int] | None = None,
    ) -> bool:
        """entry_id で 1 行だけ差し替える。行番号やパスでは更新しない。

        値が変わらなければ何もしない。Model 全体のリセットは行わない。
        """
        row = self._row_by_entry_id.get(entry_id)
        if row is None:
            return False
        entry = self._entries[row]
        updated = transform(entry)
        if updated.metadata == entry.metadata and updated.metadata_status is entry.metadata_status:
            return False
        self._entries[row] = updated
        changed_roles = (
            [*METADATA_ROLES, int(Qt.ItemDataRole.DisplayRole), int(Qt.ItemDataRole.ToolTipRole)]
            if roles is None
            else roles
        )
        self.dataChanged.emit(
            self.index(row, _METADATA_COLUMNS[0]),
            self.index(row, _METADATA_COLUMNS[-1]),
            changed_roles,
        )
        return True

    def missing_entry_ids(self) -> tuple[str, ...]:
        """欠損しているエントリの entry_id。"""
        return tuple(
            entry.entry_id for entry in self._entries if entry.file_status is FileStatus.MISSING
        )

    def unchecked_entries(self, limit: int) -> tuple[tuple[str, Path], ...]:
        """ファイル状態が未確認（``UNKNOWN``）のエントリを先頭から最大 ``limit`` 件返す。

        背景の状態確認サービスが少しずつ処理するための取り出し口。
        ここではファイルシステムへ触れない。

        **既知の制限**: 毎回先頭から走査するため、全件を ``limit`` 件ずつ処理すると
        走査量は ``N^2 / (2 * limit)`` になる。数千曲までは無視できる
        （1000曲・64件バッチで約8千回のenum比較）が、数万曲規模を扱うなら
        checker側でUNKNOWNのentry_idキューを持つ設計へ変える。
        """
        if limit <= 0:
            return ()
        found: list[tuple[str, Path]] = []
        for entry in self._entries:
            if entry.file_status is not FileStatus.UNKNOWN:
                continue
            found.append((entry.entry_id, entry.path))
            if len(found) == limit:
                break
        return tuple(found)

    def apply_file_statuses(self, statuses: Mapping[str, FileStatus]) -> int:
        """調べ済みのファイル状態をまとめて反映し、変化した行数を返す。

        ここでもファイルシステムへ触れない（呼び出し側が別スレッドで調べる）。
        変化した行だけ ``dataChanged`` を出す。
        """
        changed = 0
        for entry_id, status in statuses.items():
            row = self._row_by_entry_id.get(entry_id)
            if row is None:
                continue
            entry = self._entries[row]
            updated = entry.with_file_status(status)
            if updated is entry:
                continue
            self._entries[row] = updated
            self._emit_file_status_changed(row)
            changed += 1
        return changed

    # -- 内部 ---------------------------------------------------------------

    def _refresh_row(self, row: int) -> bool:
        """1 行のファイル状態を再確認する。変化した場合だけ ``dataChanged`` を出す。"""
        entry = self._entries[row]
        refreshed = entry.with_refreshed_status()
        if refreshed is entry:
            return False
        self._entries[row] = refreshed
        self._emit_file_status_changed(row)
        return True

    def _emit_file_status_changed(self, row: int) -> None:
        self.dataChanged.emit(
            self.index(row, Column.TITLE),
            self.index(row, Column.PATH),
            [
                FILE_STATUS_ROLE,
                *METADATA_ROLES,
                int(Qt.ItemDataRole.DisplayRole),
                int(Qt.ItemDataRole.ToolTipRole),
            ],
        )

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

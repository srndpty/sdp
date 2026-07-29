"""プレイリスト表示ウィジェット。

受け取るのは PlaylistModel だけ。PlaybackController も再生バックエンドも、
永続化（playlist.json）も知らない。プレイリストからの再生は P2-C の責務。
"""

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from sdp.core.playlist.entry import FileStatus
from sdp.core.playlist.model import FILE_STATUS_ROLE, Column, PlaylistModel

# ファイルダイアログのフィルターはユーザー補助にすぎない。拡張子で再生可否を
# 断定しないため（ADR-0001 の制約 3）、「すべてのファイル」も必ず選べるようにする。
FILE_DIALOG_FILTER = ";;".join(
    [
        "音声ファイル (*.wav *.mp3 *.ogg *.opus *.flac *.m4a *.aac)",
        "すべてのファイル (*)",
    ]
)

CLEAR_CONFIRM_TEXT = "プレイリストの全項目を削除しますか？"


class MissingEntryDelegate(QStyledItemDelegate):
    """欠損エントリの行をグレーで描く。

    色は現在の QPalette の Disabled/Text を使い、固定 RGB を埋め込まない
    （ライト / ダークどちらのテーマでも読めるようにするため）。
    グレー表示は「見た目」だけで、選択・削除・並べ替えは通常どおり行える。
    再生可否とは別の話であり、欠損行を disabled item にはしない。
    """

    def initStyleOption(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        if index.data(FILE_STATUS_ROLE) is not FileStatus.MISSING:
            return
        disabled_text = option.palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        option.palette.setColor(QPalette.ColorRole.Text, disabled_text)
        option.palette.setColor(QPalette.ColorRole.HighlightedText, disabled_text)


class PlaylistTableView(QTableView):
    """並べ替え時に View が行を自動削除しないようにした QTableView。

    Qt の既定では、`MoveAction` で終わったドラッグの後に View 自身が
    選択行を削除する（``QAbstractItemViewPrivate::clearOrRemove``）。
    本アプリの内部並べ替えは Model の ``moveRows`` で完結させるため、
    ドラッグを CopyAction として実行して View 側の削除を起こさせない。
    Model は内部 MIME を受け取った時点で常に移動として扱う。
    """

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        del supportedActions
        super().startDrag(Qt.DropAction.CopyAction)


class PlaylistView(QWidget):
    """プレイリストの表示と、追加・削除・並べ替えの操作。"""

    message_requested = Signal(str)
    """ステータス表示してほしい短いメッセージ。表示先は MainWindow が決める。"""

    def __init__(self, model: PlaylistModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = model

        self._table = PlaylistTableView()
        self._table.setObjectName("playlistTable")
        self._table.setModel(model)
        self._table.setItemDelegate(MissingEntryDelegate(self._table))
        self._configure_table()

        self._add_button = QPushButton("ファイルを追加...")
        self._add_button.setObjectName("addFilesButton")
        self._remove_button = QPushButton("選択項目を削除")
        self._remove_button.setObjectName("removeSelectedButton")
        self._clear_button = QPushButton("すべて消去")
        self._clear_button.setObjectName("clearPlaylistButton")
        self._count_label = QLabel()
        self._count_label.setObjectName("playlistCountLabel")

        self._build_layout()

        self._add_button.clicked.connect(self.add_files)
        self._remove_button.clicked.connect(self.remove_selected)
        self._clear_button.clicked.connect(self.clear_playlist)

        model.rowsInserted.connect(self._update_state)
        model.rowsRemoved.connect(self._update_state)
        model.modelReset.connect(self._update_state)
        self._table.selectionModel().selectionChanged.connect(self._update_state)

        self._update_state()

    # -- 構築 ---------------------------------------------------------------

    def _configure_table(self) -> None:
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setDragEnabled(True)
        self._table.setAcceptDrops(True)
        self._table.setDropIndicatorShown(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self._table.setDefaultDropAction(Qt.DropAction.CopyAction)
        # 行の「上書き」ではなく行と行の「間」へ落とす並べ替えにする。
        self._table.setDragDropOverwriteMode(False)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setVisible(True)
        header.setSectionResizeMode(Column.NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(Column.PATH, QHeaderView.ResizeMode.Stretch)
        # 列ヘッダーのクリックで並べ替えない。プレイリストの順序と表示順が
        # ずれると「次の曲」の意味が壊れるため（QSortFilterProxyModel も使わない）。
        self._table.setSortingEnabled(False)

    def _build_layout(self) -> None:
        buttons = QHBoxLayout()
        buttons.addWidget(self._add_button)
        buttons.addWidget(self._remove_button)
        buttons.addWidget(self._clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self._count_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table, stretch=1)
        layout.addLayout(buttons)

    # -- 操作 ---------------------------------------------------------------

    def add_files(self) -> None:
        """ファイルダイアログで選んだ複数ファイルを、選択順のまま一括追加する。"""
        selected, _ = QFileDialog.getOpenFileNames(
            self, "プレイリストに追加するファイル", "", FILE_DIALOG_FILTER
        )
        if not selected:
            return
        # 1 ファイルずつ追加せず、1 回の一括追加にする（rowsInserted も 1 回）。
        paths = [Path(name) for name in selected]
        self._model.add_paths(paths)
        self.message_requested.emit(f"{len(paths)}曲を追加しました。")

    def remove_selected(self) -> None:
        """選択行を削除する。非連続の選択にも対応する。"""
        rows = self._selected_rows()
        if not rows:
            return
        removed = 0
        # 連続範囲へまとめ、下側から削除して行番号のずれを避ける。
        for start, count in reversed(_contiguous_ranges(rows)):
            if self._model.removeRows(start, count):
                removed += count
        if removed:
            self._select_row_after_removal(rows[0])
            self.message_requested.emit(f"{removed}項目を削除しました。")

    def clear_playlist(self) -> None:
        """確認のうえ全消去する。ディスク上のファイルは削除しない。"""
        if self._model.rowCount() == 0:
            return
        answer = QMessageBox.question(
            self,
            "プレイリストの消去",
            CLEAR_CONFIRM_TEXT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._model.clear()
        self.message_requested.emit("プレイリストを消去しました。")

    # -- 表示状態 -----------------------------------------------------------

    def _update_state(self) -> None:
        """件数表示とボタンの活性をまとめて更新する（唯一の更新経路）。"""
        count = self._model.rowCount()
        self._count_label.setText(f"{count}曲")
        self._remove_button.setEnabled(bool(self._selected_rows()))
        self._clear_button.setEnabled(count > 0)

    def _selected_rows(self) -> list[int]:
        return sorted(index.row() for index in self._table.selectionModel().selectedRows())

    def _select_row_after_removal(self, first_removed_row: int) -> None:
        """削除後は次の行、末尾を削除した場合は新しい末尾を選ぶ。"""
        count = self._model.rowCount()
        if count == 0:
            return
        row = min(first_removed_row, count - 1)
        self._table.selectRow(row)


def _contiguous_ranges(rows: list[int]) -> list[tuple[int, int]]:
    """昇順の行番号を ``(開始行, 行数)`` の連続範囲へまとめる。"""
    ranges: list[tuple[int, int]] = []
    for row in rows:
        if ranges and ranges[-1][0] + ranges[-1][1] == row:
            start, count = ranges[-1]
            ranges[-1] = (start, count + 1)
        else:
            ranges.append((row, 1))
    return ranges

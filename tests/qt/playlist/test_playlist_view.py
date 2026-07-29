"""PlaylistView の契約を PlaylistModel だけで検証する。

FakeBackend も PlaybackController も使わない（View は再生を知らない）。
ネイティブのファイルダイアログと確認ダイアログは差し替える。
"""

import gc
import inspect
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPushButton,
    QStyleOptionViewItem,
    QTableView,
)
from pytestqt.qtbot import QtBot

from sdp.core.playlist.model import Column, PlaylistModel
from sdp.ui import playlist_view as playlist_view_module
from sdp.ui.playlist_view import MissingEntryDelegate, PlaylistView


@pytest.fixture
def model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def view(model: PlaylistModel, qtbot: QtBot) -> Iterator[PlaylistView]:
    widget = PlaylistView(model)
    qtbot.addWidget(widget)
    yield widget


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(5):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def table_of(view: PlaylistView) -> QTableView:
    table = view.findChild(QTableView, "playlistTable")
    assert table is not None
    return table


def button(view: PlaylistView, name: str) -> QPushButton:
    widget = view.findChild(QPushButton, name)
    assert widget is not None, name
    return widget


def count_text(view: PlaylistView) -> str:
    label = view.findChild(QLabel, "playlistCountLabel")
    assert label is not None
    return label.text()


def stub_open_files(selected: list[str]) -> Callable[..., tuple[list[str], str]]:
    def _dialog(*args: object, **kwargs: object) -> tuple[list[str], str]:
        del args, kwargs
        return (selected, "")

    return _dialog


def stub_question(answer: object) -> Callable[..., object]:
    def _question(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return answer

    return _question


def select_rows(view: PlaylistView, rows: list[int]) -> None:
    table = table_of(view)
    selection = table.selectionModel()
    assert selection is not None
    table_model = table.model()
    selection.clearSelection()
    for row in rows:
        selection.select(
            table_model.index(row, Column.NAME),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )


def record_row_counts(model: PlaylistModel, sink: list[int]) -> None:
    """rowsInserted の 1 回あたりの行数を記録する。"""

    def _on_rows_inserted(parent: QModelIndex, first: int, last: int) -> None:
        del parent
        sink.append(last - first + 1)

    model.rowsInserted.connect(_on_rows_inserted)


# -- 依存の向き -------------------------------------------------------------


def test_playlist_view_only_needs_a_model() -> None:
    """PlaylistView が受け取るのは PlaylistModel（と親）だけ。"""
    parameters = list(inspect.signature(PlaylistView.__init__).parameters)
    assert parameters == ["self", "model", "parent"]


def test_playlist_view_module_does_not_know_playback_or_persistence() -> None:
    """再生も永続化も参照しない。"""
    for forbidden in (
        "PlaybackController",
        "QtMultimediaBackend",
        "QMediaPlayer",
        "save_playlist",
        "load_playlist",
        "PlaylistSession",
    ):
        assert not hasattr(playlist_view_module, forbidden), forbidden


def test_view_has_no_playback_operations(view: PlaylistView) -> None:
    """再生操作を持たない（プレイリストからの再生は P2-C）。"""
    for forbidden in ("play", "pause", "stop", "play_selected", "controller"):
        assert not hasattr(view, forbidden), forbidden


# -- テーブルの設定 ---------------------------------------------------------


def test_table_uses_the_given_model(view: PlaylistView, model: PlaylistModel) -> None:
    """同じモデルが設定される。"""
    assert table_of(view).model() is model


def test_table_selection_and_edit_settings(view: PlaylistView) -> None:
    """行単位・複数選択で、編集はできない。"""
    table = table_of(view)

    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    # ウィンドウを表示していない状態でも設定そのものを確認できる isHidden を使う。
    assert not table.horizontalHeader().isHidden()
    assert not table.isSortingEnabled()


def test_table_drag_and_drop_settings(view: PlaylistView) -> None:
    """並べ替えドラッグと外部ドロップを受け付け、行の間に落とす。"""
    table = table_of(view)

    assert table.dragEnabled()
    assert table.acceptDrops()
    assert table.showDropIndicator()
    assert table.dragDropMode() == QAbstractItemView.DragDropMode.DragDrop
    assert not table.dragDropOverwriteMode()
    assert table.defaultDropAction() == Qt.DropAction.CopyAction


def test_internal_drag_runs_as_copy_action(
    view: PlaylistView, monkeypatch: pytest.MonkeyPatch
) -> None:
    """並べ替えドラッグは CopyAction として実行する。

    MoveAction のまま実行すると、Model の moveRows で移動した後に View が
    元の行を削除してしまう（行が消える）。移動は Model に一本化する。
    """
    recorded: list[Qt.DropAction] = []

    def fake_start_drag(self: QTableView, supported_actions: Qt.DropAction) -> None:
        del self
        recorded.append(supported_actions)

    monkeypatch.setattr(QTableView, "startDrag", fake_start_drag)

    table_of(view).startDrag(Qt.DropAction.MoveAction)

    assert recorded == [Qt.DropAction.CopyAction]


def test_initial_count_is_zero(view: PlaylistView) -> None:
    """初期件数は 0。"""
    assert count_text(view) == "0曲"


# -- ファイル追加 -----------------------------------------------------------


def test_cancelled_dialog_does_not_change_the_model(
    view: PlaylistView, model: PlaylistModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイル選択のキャンセルでは何もしない。"""
    monkeypatch.setattr(playlist_view_module.QFileDialog, "getOpenFileNames", stub_open_files([]))

    view.add_files()

    assert model.rowCount() == 0


def test_multiple_files_are_added_in_one_batch(
    view: PlaylistView,
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """複数選択の順序を保ち、1 回の一括追加で反映する。"""
    monkeypatch.setattr(
        playlist_view_module.QFileDialog,
        "getOpenFileNames",
        stub_open_files([str(path) for path in audio_files]),
    )
    inserted: list[int] = []
    record_row_counts(model, inserted)
    messages: list[str] = []
    view.message_requested.connect(messages.append)

    view.add_files()

    assert [entry.path for entry in model.entries()] == [path.resolve() for path in audio_files]
    assert inserted == [len(audio_files)]
    assert messages == [f"{len(audio_files)}曲を追加しました。"]
    assert count_text(view) == "5曲"


def test_same_file_can_be_added_twice(
    view: PlaylistView,
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じパスを何度でも追加できる。"""
    monkeypatch.setattr(
        playlist_view_module.QFileDialog,
        "getOpenFileNames",
        stub_open_files([str(audio_files[0])]),
    )

    view.add_files()
    view.add_files()

    assert model.rowCount() == 2
    assert model.entry_at(0).entry_id != model.entry_at(1).entry_id


def test_all_files_filter_is_available() -> None:
    """拡張子で再生可否を断定しないため「すべてのファイル」を選べる。"""
    assert "すべてのファイル (*)" in playlist_view_module.FILE_DIALOG_FILTER


# -- 削除 -------------------------------------------------------------------


def test_remove_button_requires_a_selection(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """選択が無ければ削除ボタンは無効。"""
    model.add_paths(audio_files)
    assert not button(view, "removeSelectedButton").isEnabled()

    select_rows(view, [1])

    assert button(view, "removeSelectedButton").isEnabled()


def test_remove_without_selection_is_a_no_op(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """選択が無ければ何もしない。"""
    model.add_paths(audio_files)

    view.remove_selected()

    assert model.rowCount() == len(audio_files)


def test_remove_non_contiguous_selection(
    view: PlaylistView, model: PlaylistModel, tmp_path: Path
) -> None:
    """非連続の選択でも、正しい行だけを削除する。"""
    paths = [tmp_path / f"曲 {index}.wav" for index in range(9)]
    for path in paths:
        path.write_bytes(b"x")
    model.add_paths(paths)
    kept_ids = [model.entry_at(row).entry_id for row in (0, 4, 5, 6)]
    select_rows(view, [1, 2, 3, 7, 8])

    view.remove_selected()

    assert [entry.entry_id for entry in model.entries()] == kept_ids
    assert count_text(view) == "4曲"


def test_remove_reports_the_number_of_removed_rows(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """削除件数をメッセージで知らせる。"""
    model.add_paths(audio_files)
    select_rows(view, [0, 1])
    messages: list[str] = []
    view.message_requested.connect(messages.append)

    view.remove_selected()

    assert messages == ["2項目を削除しました。"]


def test_selection_moves_to_the_next_row_after_removal(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """削除後は次の行、末尾を削除した場合は新しい末尾を選ぶ。"""
    model.add_paths(audio_files)
    select_rows(view, [1])
    view.remove_selected()
    selection = table_of(view).selectionModel()
    assert selection is not None
    assert [index.row() for index in selection.selectedRows()] == [1]

    select_rows(view, [model.rowCount() - 1])
    view.remove_selected()

    assert [index.row() for index in selection.selectedRows()] == [model.rowCount() - 1]


# -- 全消去 -----------------------------------------------------------------


def test_clear_button_is_disabled_when_empty(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """空のときは全消去ボタンが無効。"""
    assert not button(view, "clearPlaylistButton").isEnabled()

    model.add_paths(audio_files[:1])

    assert button(view, "clearPlaylistButton").isEnabled()


def test_clear_is_cancelled(
    view: PlaylistView,
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認をキャンセルしたら消さない。"""
    model.add_paths(audio_files)
    monkeypatch.setattr(
        playlist_view_module.QMessageBox,
        "question",
        stub_question(playlist_view_module.QMessageBox.StandardButton.No),
    )

    view.clear_playlist()

    assert model.rowCount() == len(audio_files)


def test_clear_is_confirmed(
    view: PlaylistView,
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """確認したら全消去する（ディスク上のファイルは消さない）。"""
    model.add_paths(audio_files)
    monkeypatch.setattr(
        playlist_view_module.QMessageBox,
        "question",
        stub_question(playlist_view_module.QMessageBox.StandardButton.Yes),
    )

    messages: list[str] = []
    view.message_requested.connect(messages.append)

    view.clear_playlist()

    assert model.rowCount() == 0
    assert count_text(view) == "0曲"
    assert messages == ["プレイリストを消去しました。"]
    assert all(path.exists() for path in audio_files)


def test_clear_on_empty_playlist_does_not_ask(
    view: PlaylistView, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空のときは確認ダイアログも出さない。"""
    asked: list[str] = []

    def _question(*args: object, **kwargs: object) -> object:
        del args, kwargs
        asked.append("asked")
        return playlist_view_module.QMessageBox.StandardButton.No

    monkeypatch.setattr(playlist_view_module.QMessageBox, "question", _question)

    view.clear_playlist()

    assert asked == []


# -- 欠損行の表示 -----------------------------------------------------------


def test_missing_row_is_greyed_out(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], tmp_path: Path
) -> None:
    """欠損行は Disabled/Text の色で描かれ、利用可能な行とは異なる。"""
    model.add_paths([audio_files[0], tmp_path / "ない曲.wav"])
    delegate = table_of(view).itemDelegate()
    assert isinstance(delegate, MissingEntryDelegate)

    available_option = QStyleOptionViewItem()
    delegate.initStyleOption(available_option, model.index(0, Column.NAME))
    missing_option = QStyleOptionViewItem()
    delegate.initStyleOption(missing_option, model.index(1, Column.NAME))

    disabled_text = missing_option.palette.color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
    )
    assert missing_option.palette.color(QPalette.ColorRole.Text) == disabled_text
    assert available_option.palette.color(QPalette.ColorRole.Text) != disabled_text


def test_missing_row_stays_selectable_and_removable(
    view: PlaylistView, model: PlaylistModel, tmp_path: Path
) -> None:
    """欠損行も選択・削除でき、Model からは消えない。"""
    model.add_paths([tmp_path / "ない曲.wav"])
    assert model.entry_at(0).is_missing

    select_rows(view, [0])
    assert button(view, "removeSelectedButton").isEnabled()

    view.remove_selected()
    assert model.rowCount() == 0


def test_missing_row_keeps_its_tooltip(model: PlaylistModel, tmp_path: Path) -> None:
    """欠損行でもツールチップでパスを確認できる。"""
    path = tmp_path / "ない曲.wav"
    model.add_paths([path])

    tooltip = model.data(model.index(0, Column.NAME), 3)  # Qt.ToolTipRole

    assert tooltip == str(path.resolve())


# -- 大量データ -------------------------------------------------------------


def test_view_handles_1000_entries(
    view: PlaylistView, model: PlaylistModel, tmp_path: Path, qtbot: QtBot
) -> None:
    """1000 件の一括追加で rowsInserted は 1 回、件数表示も追随する。"""
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")
    inserted: list[int] = []
    record_row_counts(model, inserted)

    model.add_paths(paths)

    assert inserted == [1000]
    assert count_text(view) == "1000曲"
    assert table_of(view).model() is model
    # UI イベント処理が継続できる。
    qtbot.waitUntil(lambda: count_text(view) == "1000曲", timeout=5000)


# -- 寿命 -------------------------------------------------------------------


def test_view_is_released_after_deletion(qtbot: QtBot) -> None:
    """PlaylistView を破棄したあと参照が残らない。"""
    del qtbot
    model = PlaylistModel()
    widget = PlaylistView(model)
    reference = weakref.ref(widget)

    del widget
    gc.collect()

    assert reference() is None
    # 破棄後のモデル変更でクラッシュしないこと。
    model.add_paths([])
    assert model.rowCount() == 0


def test_delegate_ignores_invalid_index(view: PlaylistView) -> None:
    """無効な index でも例外にならない。"""
    delegate = table_of(view).itemDelegate()
    assert isinstance(delegate, MissingEntryDelegate)
    option = QStyleOptionViewItem()

    delegate.initStyleOption(option, QModelIndex())

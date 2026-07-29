"""PlaylistModel の契約を検証する。

モデルの妥当性は `QAbstractItemModelTester` で常時検証する。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QAbstractItemModelTester, QSignalSpy
from pytestqt.qtbot import QtBot

from sdp.core.playlist.entry import FileStatus, create_entry
from sdp.core.playlist.model import (
    ENTRY_ID_ROLE,
    FILE_STATUS_ROLE,
    METADATA_ROLES,
    PATH_ROLE,
    Column,
    PlaylistModel,
)
from sdp.core.playlist.persistence import load_playlist, save_playlist


@pytest.fixture
def model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    """毎回 QAbstractItemModelTester を取り付けたモデル。"""
    del qtbot
    instance = PlaylistModel()
    QAbstractItemModelTester(instance, QAbstractItemModelTester.FailureReportingMode.Fatal)
    yield instance


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(5):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def names(model: PlaylistModel) -> list[str]:
    return [entry.display_name for entry in model.entries()]


# -- 追加 -------------------------------------------------------------------


def test_empty_model_has_no_rows(model: PlaylistModel) -> None:
    """初期状態は空。列は定義済み。"""
    assert model.rowCount() == 0
    assert model.columnCount() == len(Column)
    assert model.entries() == ()


def test_add_paths_appends_in_order(model: PlaylistModel, audio_files: list[Path]) -> None:
    """パス群を受け取った順に末尾へ追加する。"""
    entry_ids = model.add_paths(audio_files)

    assert model.rowCount() == len(audio_files)
    assert names(model) == [path.name for path in audio_files]
    assert list(entry_ids) == [entry.entry_id for entry in model.entries()]


def test_add_paths_normalizes_to_absolute(
    model: PlaylistModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相対パスも絶対パスとして保持する。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "相対 曲.wav").write_bytes(b"x")

    model.add_paths([Path("相対 曲.wav")])

    assert model.entry_at(0).path == (tmp_path / "相対 曲.wav").resolve()


def test_duplicate_paths_are_allowed(model: PlaylistModel, audio_files: list[Path]) -> None:
    """同じパスの重複追加を許可し、行は entry_id で区別する（PL-07）。"""
    model.add_paths([audio_files[0], audio_files[0]])

    first, second = model.entries()
    assert first.path == second.path
    assert first.entry_id != second.entry_id
    assert model.row_of_entry_id(first.entry_id) == 0
    assert model.row_of_entry_id(second.entry_id) == 1


def test_insert_paths_at_position(model: PlaylistModel, audio_files: list[Path]) -> None:
    """指定行へ一括挿入できる。"""
    model.add_paths(audio_files[:2])

    model.insert_paths(1, audio_files[2:4])

    assert names(model) == [
        audio_files[0].name,
        audio_files[2].name,
        audio_files[3].name,
        audio_files[1].name,
    ]


def test_insert_entries_accepts_existing_entries(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """既存の PlaylistEntry 群を追加できる。"""
    entries = [create_entry(path) for path in audio_files[:3]]

    model.add_entries(entries)

    assert [entry.entry_id for entry in model.entries()] == [entry.entry_id for entry in entries]


def test_insert_at_invalid_row_raises(model: PlaylistModel, audio_files: list[Path]) -> None:
    """範囲外への挿入は IndexError（呼び出し側のバグを隠さない）。"""
    with pytest.raises(IndexError):
        model.insert_paths(1, audio_files[:1])


def test_duplicate_entry_id_is_rejected(model: PlaylistModel, audio_files: list[Path]) -> None:
    """entry_id の重複は拒否する（暗黙の採番し直しをしない）。"""
    entry = create_entry(audio_files[0])
    model.add_entries([entry])

    with pytest.raises(ValueError):
        model.add_entries([entry])
    with pytest.raises(ValueError):
        model.add_entries(
            [create_entry(audio_files[1], entry_id="x"), create_entry(audio_files[2], entry_id="x")]
        )
    assert model.rowCount() == 1


def test_adding_nothing_does_not_signal(model: PlaylistModel, qtbot: QtBot) -> None:
    """空の追加では行を挿入しない。"""
    with qtbot.assertNotEmitted(model.rowsInserted):
        model.add_paths([])

    assert model.rowCount() == 0


# -- 読み取り ---------------------------------------------------------------


def test_entry_at_and_row_lookup(model: PlaylistModel, audio_files: list[Path]) -> None:
    """行からエントリ、entry_id から行を引ける。"""
    model.add_paths(audio_files)

    for row, path in enumerate(audio_files):
        entry = model.entry_at(row)
        assert entry.path == path.resolve()
        assert model.row_of_entry_id(entry.entry_id) == row


def test_entry_at_out_of_range_raises(model: PlaylistModel) -> None:
    """範囲外の行は IndexError。"""
    with pytest.raises(IndexError):
        model.entry_at(0)


def test_row_of_unknown_entry_id_is_none(model: PlaylistModel) -> None:
    """未知の entry_id は None。"""
    assert model.row_of_entry_id("unknown") is None


def test_entries_snapshot_is_not_the_internal_list(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """スナップショットへの変更がモデルへ波及しない。"""
    model.add_paths(audio_files[:2])
    snapshot = model.entries()

    assert isinstance(snapshot, tuple)
    model.add_paths(audio_files[2:3])
    assert len(snapshot) == 2


def test_data_roles(model: PlaylistModel, audio_files: list[Path]) -> None:
    """表示・ツールチップ・entry_id・パス・ファイル状態を role で取得できる。"""
    model.add_paths(audio_files[:1])
    entry = model.entry_at(0)
    name_index = model.index(0, Column.NAME)
    path_index = model.index(0, Column.PATH)

    assert model.data(name_index, Qt.ItemDataRole.DisplayRole) == entry.display_name
    assert model.data(path_index, Qt.ItemDataRole.DisplayRole) == str(entry.path)
    assert model.data(name_index, Qt.ItemDataRole.ToolTipRole) == str(entry.path)
    assert model.data(name_index, ENTRY_ID_ROLE) == entry.entry_id
    assert model.data(name_index, PATH_ROLE) == entry.path
    assert model.data(name_index, FILE_STATUS_ROLE) is FileStatus.AVAILABLE


def test_custom_role_names_are_stable(model: PlaylistModel) -> None:
    """カスタムroleを名前でも識別できる。"""
    role_names = model.roleNames()

    assert role_names[ENTRY_ID_ROLE].data() == b"entryId"
    assert role_names[PATH_ROLE].data() == b"path"
    assert role_names[FILE_STATUS_ROLE].data() == b"fileStatus"


def test_data_for_invalid_index_is_none(model: PlaylistModel) -> None:
    """無効なインデックスでは None。"""
    assert model.data(QModelIndex(), Qt.ItemDataRole.DisplayRole) is None


def test_data_for_unhandled_role_or_column_is_none(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """扱わない role では None を返す（勝手な既定値を作らない）。"""
    model.add_paths(audio_files[:1])

    assert model.data(model.index(0, Column.NAME), Qt.ItemDataRole.DecorationRole) is None
    assert model.data(model.index(0, Column.NAME), Qt.ItemDataRole.EditRole) is None


def test_header_data_for_other_roles_and_sections_is_none(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """表示 role 以外と範囲外の section では None を返す。"""
    model.add_paths(audio_files[:1])

    assert (
        model.headerData(Column.NAME, Qt.Orientation.Horizontal, Qt.ItemDataRole.ToolTipRole)
        is None
    )
    assert model.headerData(99, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) is None
    assert model.headerData(99, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) is None


def test_header_data(model: PlaylistModel, audio_files: list[Path]) -> None:
    """水平ヘッダーは列名、垂直ヘッダーは 1 起点の行番号。"""
    model.add_paths(audio_files[:1])

    assert (
        model.headerData(Column.TITLE, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        == "タイトル"
    )
    assert (
        model.headerData(Column.PATH, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        == "パス"
    )
    assert model.headerData(0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole) == 1


# -- 削除・全消去 -----------------------------------------------------------


def test_remove_rows(model: PlaylistModel, audio_files: list[Path]) -> None:
    """連続行を削除し、entry_id の索引も更新する。"""
    model.add_paths(audio_files)
    removed_id = model.entry_at(1).entry_id

    assert model.removeRows(1, 2) is True

    assert names(model) == [audio_files[0].name, audio_files[3].name, audio_files[4].name]
    assert model.row_of_entry_id(removed_id) is None
    assert model.row_of_entry_id(model.entry_at(1).entry_id) == 1


@pytest.mark.parametrize(("row", "count"), [(-1, 1), (0, 0), (0, 6), (5, 1)])
def test_invalid_remove_is_rejected(
    model: PlaylistModel, audio_files: list[Path], row: int, count: int
) -> None:
    """範囲外の削除は False を返し、内容を変えない。"""
    model.add_paths(audio_files)

    assert model.removeRows(row, count) is False
    assert model.rowCount() == len(audio_files)


def test_clear(model: PlaylistModel, audio_files: list[Path]) -> None:
    """全消去で行も索引も空になる。"""
    model.add_paths(audio_files)
    entry_id = model.entry_at(0).entry_id

    model.clear()

    assert model.rowCount() == 0
    assert model.entries() == ()
    assert model.row_of_entry_id(entry_id) is None


def test_clear_on_empty_model_does_not_reset(model: PlaylistModel, qtbot: QtBot) -> None:
    """空のモデルの全消去はリセット通知を出さない。"""
    with qtbot.assertNotEmitted(model.modelReset):
        model.clear()


# -- 移動 -------------------------------------------------------------------


def test_move_rows_down(model: PlaylistModel, audio_files: list[Path]) -> None:
    """後ろへの移動。"""
    model.add_paths(audio_files)

    assert model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 3) is True

    assert names(model) == [
        audio_files[1].name,
        audio_files[2].name,
        audio_files[0].name,
        audio_files[3].name,
        audio_files[4].name,
    ]


def test_move_rows_up(model: PlaylistModel, audio_files: list[Path]) -> None:
    """前への移動。"""
    model.add_paths(audio_files)

    assert model.moveRows(QModelIndex(), 3, 1, QModelIndex(), 1) is True

    assert names(model) == [
        audio_files[0].name,
        audio_files[3].name,
        audio_files[1].name,
        audio_files[2].name,
        audio_files[4].name,
    ]


def test_move_multiple_rows_keeps_their_order(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """複数行の移動で相対順序が保たれ、entry_id の索引も追随する。"""
    model.add_paths(audio_files)
    moved_ids = [model.entry_at(row).entry_id for row in (0, 1)]

    assert model.moveRows(QModelIndex(), 0, 2, QModelIndex(), 5) is True

    assert names(model) == [
        audio_files[2].name,
        audio_files[3].name,
        audio_files[4].name,
        audio_files[0].name,
        audio_files[1].name,
    ]
    assert [model.row_of_entry_id(entry_id) for entry_id in moved_ids] == [3, 4]


@pytest.mark.parametrize(
    ("source_row", "count", "destination"),
    [(0, 0, 3), (-1, 1, 3), (4, 2, 0), (0, 1, 0), (0, 1, 1), (0, 2, 6), (0, 1, -1)],
)
def test_invalid_move_is_rejected(
    model: PlaylistModel,
    audio_files: list[Path],
    source_row: int,
    count: int,
    destination: int,
) -> None:
    """範囲外や意味のない移動は False を返し、内容を変えない。"""
    model.add_paths(audio_files)
    before = names(model)

    assert model.moveRows(QModelIndex(), source_row, count, QModelIndex(), destination) is False
    assert names(model) == before


def test_move_with_valid_parent_is_rejected(model: PlaylistModel, audio_files: list[Path]) -> None:
    """テーブルモデルなので子を持つ移動は受け付けない。"""
    model.add_paths(audio_files)
    parent = model.index(0, 0)

    assert model.moveRows(parent, 0, 1, QModelIndex(), 3) is False
    assert model.moveRows(QModelIndex(), 0, 1, parent, 3) is False


# -- 欠損状態 ---------------------------------------------------------------


def test_missing_file_is_detected_on_add(model: PlaylistModel, tmp_path: Path) -> None:
    """追加時に存在チェックを行う。"""
    model.add_paths([tmp_path / "ない曲.wav"])

    assert model.entry_at(0).file_status is FileStatus.MISSING
    assert model.missing_entry_ids() == (model.entry_at(0).entry_id,)


def test_refresh_file_status_updates_changed_rows(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """削除されたファイルを再確認で欠損にし、変化した行だけ dataChanged を出す。"""
    model.add_paths(audio_files[:3])
    audio_files[1].unlink()
    changes: list[tuple[int, int, tuple[int, ...]]] = []

    def record(top_left: QModelIndex, bottom_right: QModelIndex, roles: list[int]) -> None:
        changes.append((top_left.row(), bottom_right.row(), tuple(roles)))

    model.dataChanged.connect(record)

    changed = model.refresh_file_status()

    assert changed == 1
    assert model.entry_at(1).file_status is FileStatus.MISSING
    assert model.entry_at(0).file_status is FileStatus.AVAILABLE
    assert len(changes) == 1
    first_row, last_row, roles = changes[0]
    assert (first_row, last_row) == (1, 1)
    assert set(roles) == {
        FILE_STATUS_ROLE,
        *METADATA_ROLES,
        int(Qt.ItemDataRole.DisplayRole),
        int(Qt.ItemDataRole.ToolTipRole),
    }


def test_refresh_file_status_detects_restored_file(model: PlaylistModel, tmp_path: Path) -> None:
    """復元されたファイルは利用可能へ戻る。"""
    path = tmp_path / "戻る曲.wav"
    model.add_paths([path])
    path.write_bytes(b"x")

    assert model.refresh_file_status() == 1
    assert model.entry_at(0).file_status is FileStatus.AVAILABLE


def test_refresh_without_changes_is_silent(
    model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """変化が無ければ通知しない。"""
    model.add_paths(audio_files)

    with qtbot.assertNotEmitted(model.dataChanged):
        assert model.refresh_file_status() == 0


# -- 復元 -------------------------------------------------------------------


def test_replace_entries_from_persistence(
    model: PlaylistModel, audio_files: list[Path], tmp_path: Path
) -> None:
    """永続化から復元したエントリ群で置き換える。"""
    model.add_paths(audio_files[:2])
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, model.entries())
    saved_ids = [entry.entry_id for entry in model.entries()]
    model.clear()

    model.replace_entries(load_playlist(file_path))

    assert [entry.entry_id for entry in model.entries()] == saved_ids
    assert model.row_of_entry_id(saved_ids[1]) == 1


def test_replace_entries_rejects_duplicate_ids(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """復元データの entry_id 重複は拒否する。"""
    entry = create_entry(audio_files[0])

    with pytest.raises(ValueError):
        model.replace_entries([entry, entry])


# -- 責務の境界 -------------------------------------------------------------


def test_model_does_not_own_playback_or_persistence_state(model: PlaylistModel) -> None:
    """現在再生中・リピート・シャッフル・保存先などを持たない。"""
    for forbidden in (
        "current_entry_id",
        "current_row",
        "repeat_mode",
        "shuffle",
        "controller",
        "save",
        "load",
        "file_path",
    ):
        assert not hasattr(model, forbidden), forbidden


# -- 大量データ -------------------------------------------------------------


def test_bulk_operations_with_1000_entries(model: PlaylistModel, tmp_path: Path) -> None:
    """1000 件の一括追加・移動・削除・保存・復元が破綻しない。"""
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")

    rows_inserted_spy = QSignalSpy(model.rowsInserted)
    entry_ids = model.add_paths(paths)
    assert model.rowCount() == 1000
    assert rows_inserted_spy.count() == 1
    parent, first, last = rows_inserted_spy.at(0)
    assert not parent.isValid()
    assert first == 0
    assert last == 999
    assert len(set(entry_ids)) == 1000
    assert model.row_of_entry_id(entry_ids[999]) == 999

    assert model.moveRows(QModelIndex(), 0, 100, QModelIndex(), 1000) is True
    assert model.row_of_entry_id(entry_ids[0]) == 900

    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, model.entries())
    restored = load_playlist(file_path)
    assert [entry.entry_id for entry in restored] == [entry.entry_id for entry in model.entries()]

    assert model.removeRows(0, 500) is True
    assert model.rowCount() == 500
    assert model.row_of_entry_id(model.entry_at(499).entry_id) == 499

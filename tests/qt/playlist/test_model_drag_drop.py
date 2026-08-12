"""PlaylistModel のドラッグ＆ドロップ契約を検証する。

外部からのファイル D&D は `text/uri-list`、内部の並べ替えは entry_id を運ぶ
専用 MIME を使う。すべて `QAbstractItemModelTester` を取り付けた状態で検証する。
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QMimeData, QModelIndex, Qt, QUrl
from PySide6.QtTest import QAbstractItemModelTester
from pytestqt.qtbot import QtBot

from sdp.core.playlist.model import (
    INTERNAL_MIME_TYPE,
    URI_LIST_MIME_TYPE,
    Column,
    PlaylistModel,
)


@pytest.fixture
def model(qtbot: QtBot) -> Iterator[PlaylistModel]:
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


def url_mime(paths: list[Path]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def internal_mime(entry_ids: list[str]) -> QMimeData:
    mime = QMimeData()
    mime.setData(INTERNAL_MIME_TYPE, QByteArray(json.dumps(entry_ids).encode("utf-8")))
    return mime


def names(model: PlaylistModel) -> list[str]:
    return [entry.display_name for entry in model.entries()]


_ROOT = QModelIndex()


def drop(
    model: PlaylistModel,
    mime: QMimeData,
    row: int,
    parent: QModelIndex = _ROOT,
    *,
    action: Qt.DropAction = Qt.DropAction.CopyAction,
    column: int = 0,
) -> bool:
    return model.dropMimeData(mime, action, row, column, parent)


# -- flags と対応アクション --------------------------------------------------


def test_row_flags_allow_drag_and_selection(model: PlaylistModel, audio_files: list[Path]) -> None:
    """通常の行は選択・ドラッグ可能で、編集もチェックもできない。"""
    model.add_paths(audio_files[:1])
    flags = model.flags(model.index(0, Column.NAME))

    assert flags & Qt.ItemFlag.ItemIsEnabled
    assert flags & Qt.ItemFlag.ItemIsSelectable
    assert flags & Qt.ItemFlag.ItemIsDragEnabled
    assert not (flags & Qt.ItemFlag.ItemIsEditable)
    assert not (flags & Qt.ItemFlag.ItemIsUserCheckable)


def test_root_flags_allow_drop(model: PlaylistModel) -> None:
    """ドロップはルート（行と行の間）で受ける。"""
    assert model.flags(QModelIndex()) & Qt.ItemFlag.ItemIsDropEnabled


def test_supported_actions(model: PlaylistModel) -> None:
    """D&D転送は複製だけを使い、内部MIMEの意味だけを行移動とする。"""
    assert model.supportedDragActions() == Qt.DropAction.CopyAction
    assert model.supportedDropActions() == Qt.DropAction.CopyAction


def test_mime_types(model: PlaylistModel) -> None:
    """内部 MIME と text/uri-list を受け付ける。"""
    assert INTERNAL_MIME_TYPE in model.mimeTypes()
    assert URI_LIST_MIME_TYPE in model.mimeTypes()


# -- mimeData ---------------------------------------------------------------


def test_mime_data_uses_entry_ids_without_row_duplication(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """複数列の index から、行ごとに 1 つずつ entry_id を詰める。"""
    model.add_paths(audio_files[:3])
    indexes = [model.index(row, column) for row in (0, 1) for column in Column]

    mime = model.mimeData(indexes)

    payload = json.loads(bytes(mime.data(INTERNAL_MIME_TYPE).data()).decode("utf-8"))
    assert payload == [model.entry_at(0).entry_id, model.entry_at(1).entry_id]


def test_mime_data_keeps_current_row_order(model: PlaylistModel, audio_files: list[Path]) -> None:
    """index の順序によらず、現在の行順で entry_id を並べる。"""
    model.add_paths(audio_files[:3])
    indexes = [model.index(row, Column.NAME) for row in (2, 0, 1)]

    mime = model.mimeData(indexes)

    payload = json.loads(bytes(mime.data(INTERNAL_MIME_TYPE).data()).decode("utf-8"))
    assert payload == [model.entry_at(row).entry_id for row in (0, 1, 2)]


def test_mime_data_does_not_leak_paths(model: PlaylistModel, audio_files: list[Path]) -> None:
    """内部 MIME にはパスも行番号も入れない（重複パスと行のずれに耐えるため）。"""
    model.add_paths(audio_files[:1])

    mime = model.mimeData([model.index(0, Column.NAME)])

    assert audio_files[0].name not in bytes(mime.data(INTERNAL_MIME_TYPE).data()).decode("utf-8")


# -- 外部 URL のドロップ ----------------------------------------------------


def test_external_urls_are_appended(model: PlaylistModel, audio_files: list[Path]) -> None:
    """Copyによる末尾ドロップで順序どおり追加し、元ファイルを残す。"""
    assert drop(model, url_mime(audio_files[:3]), -1) is True

    assert names(model) == [path.name for path in audio_files[:3]]
    assert all(path.exists() for path in audio_files[:3])


@pytest.mark.parametrize("action", [Qt.DropAction.MoveAction, Qt.DropAction.LinkAction])
def test_external_urls_reject_non_copy_actions(
    model: PlaylistModel, audio_files: list[Path], action: Qt.DropAction
) -> None:
    """Move・Linkとして届いた外部URLは追加せず、元ファイルも残す。"""
    path = audio_files[0]

    assert drop(model, url_mime([path]), -1, action=action) is False

    assert model.rowCount() == 0
    assert path.exists()


def test_external_urls_are_inserted_at_row(model: PlaylistModel, audio_files: list[Path]) -> None:
    """行と行の間へのドロップでその位置へ挿入する。"""
    model.add_paths(audio_files[:2])

    assert drop(model, url_mime(audio_files[2:4]), 1) is True

    assert names(model) == [
        audio_files[0].name,
        audio_files[2].name,
        audio_files[3].name,
        audio_files[1].name,
    ]


def test_external_drop_on_a_row_inserts_before_it(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """有効な parent 付きのドロップはその行の前へ挿入する。"""
    model.add_paths(audio_files[:2])

    assert drop(model, url_mime(audio_files[2:3]), -1, model.index(1, Column.NAME)) is True

    assert names(model)[1] == audio_files[2].name


def test_external_drop_keeps_url_order(model: PlaylistModel, audio_files: list[Path]) -> None:
    """URL の並びをそのまま表示順にする。"""
    reversed_paths = list(reversed(audio_files))

    assert drop(model, url_mime(reversed_paths), -1) is True

    assert names(model) == [path.name for path in reversed_paths]


def test_external_drop_allows_duplicate_paths(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じパスを何度でも追加できる。"""
    assert drop(model, url_mime([audio_files[0], audio_files[0]]), -1) is True

    assert model.rowCount() == 2
    assert model.entry_at(0).entry_id != model.entry_at(1).entry_id


def test_external_drop_keeps_japanese_and_space_paths(model: PlaylistModel, tmp_path: Path) -> None:
    """日本語・空白を含むパスをそのまま扱う。"""
    directory = tmp_path / "日本語 ディレクトリ"
    directory.mkdir()
    path = directory / "テスト 音源 440Hz.wav"
    path.write_bytes(b"x")

    assert drop(model, url_mime([path]), -1) is True

    assert model.entry_at(0).path == path


def test_external_drop_ignores_directories(
    model: PlaylistModel, tmp_path: Path, audio_files: list[Path]
) -> None:
    """ディレクトリは追加しない（再帰追加もしない）。"""
    assert drop(model, url_mime([tmp_path, audio_files[0]]), -1) is True

    assert model.rowCount() == 1
    assert model.entry_at(0).path == audio_files[0]


def test_external_drop_ignores_non_local_urls(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """非ローカル URL は追加しない。"""
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.com/song.mp3"), QUrl.fromLocalFile(str(audio_files[0]))])

    assert drop(model, mime, -1) is True

    assert model.rowCount() == 1


def test_external_drop_without_valid_files_is_rejected(
    model: PlaylistModel, tmp_path: Path
) -> None:
    """有効なファイルが 0 件ならドロップを拒否する。"""
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.com/song.mp3"), QUrl.fromLocalFile(str(tmp_path))])

    assert drop(model, mime, -1) is False
    assert model.rowCount() == 0


def test_external_drop_does_not_reject_unknown_extensions(
    model: PlaylistModel, tmp_path: Path
) -> None:
    """拡張子では判定しない。"""
    path = tmp_path / "拡張子なし"
    path.write_bytes(b"x")

    assert drop(model, url_mime([path]), -1) is True


def test_missing_file_dropped_becomes_a_missing_entry(model: PlaylistModel, tmp_path: Path) -> None:
    """ドロップ直前に消えていたファイルは欠損エントリとして追加する。"""
    path = tmp_path / "消えた曲.wav"

    assert drop(model, url_mime([path]), -1) is True
    model.refresh_file_status()

    assert model.entry_at(0).is_missing


def test_can_drop_accepts_local_urls(model: PlaylistModel, audio_files: list[Path]) -> None:
    """ローカルファイルの URL はドロップ候補として受け入れる。"""
    assert (
        model.canDropMimeData(
            url_mime(audio_files[:1]), Qt.DropAction.CopyAction, -1, 0, QModelIndex()
        )
        is True
    )


@pytest.mark.parametrize(
    "action",
    [Qt.DropAction.MoveAction, Qt.DropAction.LinkAction],
)
def test_can_drop_rejects_unsupported_action(
    model: PlaylistModel,
    audio_files: list[Path],
    action: Qt.DropAction,
) -> None:
    """実際のdropで拒否するactionにはドロップ可能表示を出さない。"""
    assert model.canDropMimeData(url_mime(audio_files[:1]), action, -1, 0, _ROOT) is False


@pytest.mark.parametrize("column", [column.value for column in Column])
def test_drop_is_accepted_over_any_column(
    model: PlaylistModel, audio_files: list[Path], column: int
) -> None:
    """ドロップは行に対する操作であり、カーソル下の列で可否を変えない。"""
    mime = url_mime(audio_files[:1])
    assert model.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, column, _ROOT) is True
    assert drop(model, url_mime(audio_files[:1]), -1, column=column) is True


def test_can_drop_rejects_unrelated_mime(model: PlaylistModel) -> None:
    """関係のない MIME は受け付けない。"""
    mime = QMimeData()
    mime.setText("ただのテキスト")

    assert model.canDropMimeData(mime, Qt.DropAction.CopyAction, -1, 0, QModelIndex()) is False


def test_unrelated_mime_drop_returns_false(model: PlaylistModel) -> None:
    """関係のない MIME のドロップは False。"""
    mime = QMimeData()
    mime.setText("ただのテキスト")

    assert drop(model, mime, -1) is False


def test_ignore_action_is_a_no_op(model: PlaylistModel, audio_files: list[Path]) -> None:
    """IgnoreAction では何もしない（Qt の慣例どおり True を返す）。"""
    assert (
        model.dropMimeData(
            url_mime(audio_files[:1]), Qt.DropAction.IgnoreAction, -1, 0, QModelIndex()
        )
        is True
    )
    assert model.rowCount() == 0


# -- 内部並べ替え -----------------------------------------------------------


def test_internal_move_single_row_down(model: PlaylistModel, audio_files: list[Path]) -> None:
    """単一行を下方向へ移動する。"""
    model.add_paths(audio_files)
    moved_id = model.entry_at(0).entry_id

    assert drop(model, internal_mime([moved_id]), 3) is True

    assert names(model) == [
        audio_files[1].name,
        audio_files[2].name,
        audio_files[0].name,
        audio_files[3].name,
        audio_files[4].name,
    ]
    assert model.row_of_entry_id(moved_id) == 2


def test_internal_move_single_row_up(model: PlaylistModel, audio_files: list[Path]) -> None:
    """単一行を上方向へ移動する。"""
    model.add_paths(audio_files)
    moved_id = model.entry_at(3).entry_id

    assert drop(model, internal_mime([moved_id]), 1) is True

    assert model.row_of_entry_id(moved_id) == 1


def test_internal_move_to_top_and_bottom(model: PlaylistModel, audio_files: list[Path]) -> None:
    """先頭と末尾への移動。"""
    model.add_paths(audio_files)
    last_id = model.entry_at(4).entry_id

    assert drop(model, internal_mime([last_id]), 0) is True
    assert model.row_of_entry_id(last_id) == 0

    assert drop(model, internal_mime([last_id]), model.rowCount()) is True
    assert model.row_of_entry_id(last_id) == 4


def test_internal_move_contiguous_rows_keeps_order(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """連続した複数行を、相対順序を保って移動する。"""
    model.add_paths(audio_files)
    moved_ids = [model.entry_at(row).entry_id for row in (0, 1)]

    assert drop(model, internal_mime(moved_ids), 5) is True

    assert [model.row_of_entry_id(entry_id) for entry_id in moved_ids] == [3, 4]
    assert names(model) == [
        audio_files[2].name,
        audio_files[3].name,
        audio_files[4].name,
        audio_files[0].name,
        audio_files[1].name,
    ]


def test_internal_move_keeps_entry_ids_and_row_count(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """移動しても entry_id の集合と行数が変わらない（重複も消失もしない）。"""
    model.add_paths(audio_files)
    before = {entry.entry_id for entry in model.entries()}

    assert drop(model, internal_mime([model.entry_at(1).entry_id]), 4) is True

    after = [entry.entry_id for entry in model.entries()]
    assert set(after) == before
    assert len(after) == len(set(after)) == 5


def test_internal_move_with_duplicate_paths_moves_the_right_row(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じパスの行が複数あっても、entry_id で正しい行を移動する。"""
    model.add_paths([audio_files[0], audio_files[0], audio_files[1]])
    second_id = model.entry_at(1).entry_id

    assert drop(model, internal_mime([second_id]), 0) is True

    assert model.row_of_entry_id(second_id) == 0


def test_internal_drop_onto_itself_is_rejected(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """自分自身の位置へのドロップは何も変えない。"""
    model.add_paths(audio_files)
    entry_id = model.entry_at(2).entry_id
    before = names(model)

    assert drop(model, internal_mime([entry_id]), 2) is False
    assert drop(model, internal_mime([entry_id]), 3) is False
    assert names(model) == before


def test_internal_drop_inside_moved_range_is_rejected(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """移動範囲の内部へのドロップは受け付けない。"""
    model.add_paths(audio_files)
    moved_ids = [model.entry_at(row).entry_id for row in (1, 2, 3)]
    before = names(model)

    assert drop(model, internal_mime(moved_ids), 2) is False
    assert names(model) == before


def test_non_contiguous_internal_drag_is_rejected_without_probe_logs(
    model: PlaylistModel,
    audio_files: list[Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非連続選択の可否照会は静かに拒否し、確定drop時だけ警告する。"""
    model.add_paths(audio_files)
    moved_ids = [model.entry_at(row).entry_id for row in (0, 2)]
    before = names(model)
    mime = internal_mime(moved_ids)

    for _ in range(5):
        assert model.canDropMimeData(mime, Qt.DropAction.CopyAction, 4, 0, _ROOT) is False
    assert caplog.text == ""

    assert drop(model, mime, 4) is False
    assert caplog.text.count("非連続の複数行ドラッグ") == 1
    assert names(model) == before


def test_internal_drop_with_unknown_entry_id_is_rejected(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """未知の entry_id は受け付けない。"""
    model.add_paths(audio_files)

    assert drop(model, internal_mime(["unknown-id"]), 0) is False
    assert model.rowCount() == len(audio_files)


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"{}", b'["a", 1]', b"[]", b"\xff\xfe invalid utf-8"],
)
def test_broken_internal_mime_is_rejected_without_exception(
    model: PlaylistModel, audio_files: list[Path], payload: bytes
) -> None:
    """壊れた内部 MIME では例外を投げず False を返す。"""
    model.add_paths(audio_files)
    mime = QMimeData()
    mime.setData(INTERNAL_MIME_TYPE, QByteArray(payload))

    assert model.canDropMimeData(mime, Qt.DropAction.CopyAction, 0, 0, QModelIndex()) is False
    assert drop(model, mime, 0) is False
    assert model.rowCount() == len(audio_files)


def test_internal_drop_as_copy_action_still_moves(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """内部 MIME は CopyAction で届いても移動として扱う。

    View 側の自動行削除を避けるためドラッグを CopyAction で実行するため。
    """
    model.add_paths(audio_files)
    moved_id = model.entry_at(0).entry_id

    assert (
        model.dropMimeData(internal_mime([moved_id]), Qt.DropAction.CopyAction, 3, 0, QModelIndex())
        is True
    )

    assert model.rowCount() == len(audio_files)
    assert model.row_of_entry_id(moved_id) == 2


def test_can_drop_rejects_internal_no_op_destination(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """移動元の範囲内と直後にはドロップ可能表示を出さない。"""
    model.add_paths(audio_files)
    mime = internal_mime([model.entry_at(row).entry_id for row in (1, 2)])

    for destination in (1, 2, 3):
        assert (
            model.canDropMimeData(mime, Qt.DropAction.CopyAction, destination, 0, QModelIndex())
            is False
        )


# -- ドロップ位置 -----------------------------------------------------------


@pytest.mark.parametrize(("row", "expected"), [(-1, 3), (0, 0), (2, 2), (3, 3), (99, 3)])
def test_drop_row_resolution(
    model: PlaylistModel, audio_files: list[Path], row: int, expected: int
) -> None:
    """ドロップ先の行番号の決定。"""
    model.add_paths(audio_files[:3])

    assert model.drop_row(row, QModelIndex()) == expected


def test_drop_row_on_empty_playlist_is_zero(model: PlaylistModel) -> None:
    """空のプレイリストへのドロップは 0 行目。"""
    assert model.drop_row(-1, QModelIndex()) == 0


def test_drop_row_with_valid_parent(model: PlaylistModel, audio_files: list[Path]) -> None:
    """有効な parent 付きならその行番号。"""
    model.add_paths(audio_files)

    assert model.drop_row(-1, model.index(2, Column.NAME)) == 2

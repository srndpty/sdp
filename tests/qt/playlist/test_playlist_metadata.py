"""PlaylistModel のメタデータ列・role・更新 API を検証する。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QAbstractItemModelTester
from pytestqt.qtbot import QtBot

from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playlist.model import (
    ALBUM_ROLE,
    ARTIST_ROLE,
    BITRATE_BPS_ROLE,
    DURATION_MS_ROLE,
    FILE_SIZE_BYTES_ROLE,
    FILE_STATUS_ROLE,
    METADATA_FAILED_TOOLTIP,
    METADATA_STATUS_ROLE,
    TITLE_ROLE,
    Column,
    PlaylistModel,
)
from sdp.core.playlist.persistence import load_playlist, save_playlist

SAMPLE = TrackMetadata(
    title="曲名",
    artist="奏者",
    album="盤",
    duration_ms=65_000,
    file_size_bytes=1_572_864,
    bitrate_bps=320_000,
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
    for index in range(3):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def display(model: PlaylistModel, row: int, column: Column) -> object:
    return model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)


def record_changes(model: PlaylistModel) -> list[tuple[int, int, tuple[int, ...]]]:
    """(先頭列, 末尾列, roles) を記録する。"""
    changes: list[tuple[int, int, tuple[int, ...]]] = []

    def on_changed(top_left: QModelIndex, bottom_right: QModelIndex, roles: list[int]) -> None:
        changes.append((top_left.column(), bottom_right.column(), tuple(roles)))

    model.dataChanged.connect(on_changed)
    return changes


# -- 初期状態と列 -----------------------------------------------------------


def test_columns_are_title_size_bitrate_duration_path(model: PlaylistModel) -> None:
    """列はタイトル・サイズ・ビットレート・長さ・パス。"""
    headers = [
        model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for column in Column
    ]

    assert headers == ["タイトル", "サイズ", "ビットレート", "長さ", "パス"]
    assert Column.NAME is Column.TITLE


def test_new_entries_start_unrequested(model: PlaylistModel, audio_files: list[Path]) -> None:
    """追加直後はメタデータ未要求。"""
    model.add_paths(audio_files)

    entry = model.entry_at(0)
    assert entry.metadata is None
    assert entry.metadata_status is MetadataStatus.NOT_REQUESTED
    assert model.data(model.index(0, Column.TITLE), METADATA_STATUS_ROLE) is (
        MetadataStatus.NOT_REQUESTED
    )


def test_title_falls_back_to_the_file_name(model: PlaylistModel, audio_files: list[Path]) -> None:
    """タイトル未取得ではファイル名を表示する。"""
    model.add_paths(audio_files)

    assert display(model, 0, Column.TITLE) == audio_files[0].name
    assert display(model, 0, Column.FILE_SIZE) == ""
    assert display(model, 0, Column.BITRATE) == ""
    assert display(model, 0, Column.DURATION) == ""
    assert display(model, 0, Column.PATH) == str(audio_files[0])


# -- 状態遷移 ---------------------------------------------------------------


def test_loading_keeps_the_file_name(model: PlaylistModel, audio_files: list[Path]) -> None:
    """読み取り中でもタイトルはファイル名のまま（「読み込み中...」にしない）。"""
    entry_ids = model.add_paths(audio_files)

    assert model.mark_metadata_loading(entry_ids[0]) is True

    assert model.entry_at(0).metadata_status is MetadataStatus.LOADING
    assert display(model, 0, Column.TITLE) == audio_files[0].name


def test_loaded_metadata_is_displayed(model: PlaylistModel, audio_files: list[Path]) -> None:
    """取得できたら各列へ反映する。"""
    entry_ids = model.add_paths(audio_files)
    model.mark_metadata_loading(entry_ids[0])

    assert model.apply_metadata(entry_ids[0], SAMPLE) is True

    assert model.entry_at(0).metadata_status is MetadataStatus.LOADED
    assert model.entry_at(0).metadata == SAMPLE
    assert display(model, 0, Column.TITLE) == "曲名"
    assert display(model, 0, Column.FILE_SIZE) == "1.5 MiB"
    assert display(model, 0, Column.BITRATE) == "320 kbps"
    assert display(model, 0, Column.DURATION) == "1:05"


def test_failed_metadata_falls_back(model: PlaylistModel, audio_files: list[Path]) -> None:
    """失敗してもファイル名を表示し、行は残る。"""
    entry_ids = model.add_paths(audio_files)
    model.mark_metadata_loading(entry_ids[0])

    assert model.mark_metadata_failed(entry_ids[0]) is True

    assert model.entry_at(0).metadata_status is MetadataStatus.FAILED
    assert model.entry_at(0).metadata is None
    assert display(model, 0, Column.TITLE) == audio_files[0].name
    assert display(model, 0, Column.FILE_SIZE) == ""
    assert model.rowCount() == len(audio_files)


def test_failed_metadata_has_a_short_tooltip(model: PlaylistModel, audio_files: list[Path]) -> None:
    """失敗のツールチップは短い文言だけ（例外文字列を出さない）。"""
    entry_ids = model.add_paths(audio_files)
    model.mark_metadata_failed(entry_ids[0])

    tooltip = model.data(model.index(0, Column.TITLE), Qt.ItemDataRole.ToolTipRole)

    assert isinstance(tooltip, str)
    assert METADATA_FAILED_TOOLTIP in tooltip
    assert "Traceback" not in tooltip


def test_clear_metadata_returns_to_not_requested(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """解除すると未要求へ戻り、再読み取りできる。"""
    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)

    assert model.clear_metadata(entry_ids[0]) is True

    assert model.entry_at(0).metadata_status is MetadataStatus.NOT_REQUESTED
    assert model.entry_at(0).metadata is None


def test_metadata_invariants(model: PlaylistModel, audio_files: list[Path]) -> None:
    """LOADED のときだけ値を持ち、それ以外は None。"""
    entry_ids = model.add_paths(audio_files)

    model.apply_metadata(entry_ids[0], SAMPLE)
    assert model.entry_at(0).metadata is not None

    for transition in (model.mark_metadata_loading, model.mark_metadata_failed):
        model.apply_metadata(entry_ids[0], SAMPLE)
        transition(entry_ids[0])
        assert model.entry_at(0).metadata is None
        assert model.entry_at(0).metadata_status is not MetadataStatus.LOADED


def test_metadata_update_keeps_identity(model: PlaylistModel, audio_files: list[Path]) -> None:
    """メタデータ更新で entry_id・path・file_status を変えない。"""
    entry_ids = model.add_paths(audio_files)
    before = model.entry_at(0)

    model.apply_metadata(entry_ids[0], SAMPLE)

    after = model.entry_at(0)
    assert (after.entry_id, after.path, after.file_status) == (
        before.entry_id,
        before.path,
        before.file_status,
    )


def test_metadata_transitions_do_not_probe_the_filesystem(
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """メタデータだけの不変更新ではGUIスレッドからファイル状態を再調査しない。"""
    entry_ids = model.add_paths(audio_files[:1])

    def unexpected_probe(path: Path) -> object:
        raise AssertionError(f"不要なファイル状態調査: {path}")

    monkeypatch.setattr("sdp.core.playlist.entry.probe_file_status", unexpected_probe)

    assert model.mark_metadata_loading(entry_ids[0]) is True
    assert model.apply_metadata(entry_ids[0], SAMPLE) is True
    assert model.mark_metadata_failed(entry_ids[0]) is True
    assert model.clear_metadata(entry_ids[0]) is True


# -- role -------------------------------------------------------------------


def test_metadata_roles_return_semantic_values(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """role は表示文字列ではなく意味上の値を返す。"""
    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)
    index = model.index(0, Column.TITLE)

    assert model.data(index, TITLE_ROLE) == "曲名"
    assert model.data(index, ARTIST_ROLE) == "奏者"
    assert model.data(index, ALBUM_ROLE) == "盤"
    assert model.data(index, DURATION_MS_ROLE) == 65_000
    assert model.data(index, FILE_SIZE_BYTES_ROLE) == 1_572_864
    assert model.data(index, BITRATE_BPS_ROLE) == 320_000
    assert model.data(index, METADATA_STATUS_ROLE) is MetadataStatus.LOADED


def test_metadata_roles_are_none_before_loading(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """未取得では role は None（表示だけがフォールバックする）。"""
    model.add_paths(audio_files)
    index = model.index(0, Column.TITLE)

    assert model.data(index, TITLE_ROLE) is None
    assert model.data(index, DURATION_MS_ROLE) is None


def test_role_names_include_metadata(model: PlaylistModel) -> None:
    """roleNames にメタデータ role を含める。"""
    role_names = model.roleNames()

    assert role_names[TITLE_ROLE].data() == b"title"
    assert role_names[ARTIST_ROLE].data() == b"artist"
    assert role_names[ALBUM_ROLE].data() == b"album"
    assert role_names[DURATION_MS_ROLE].data() == b"durationMs"
    assert role_names[FILE_SIZE_BYTES_ROLE].data() == b"fileSizeBytes"
    assert role_names[BITRATE_BPS_ROLE].data() == b"bitrateBps"
    assert role_names[METADATA_STATUS_ROLE].data() == b"metadataStatus"


def test_unknown_duration_is_blank(model: PlaylistModel, audio_files: list[Path]) -> None:
    """長さ不明は空欄（0:00 と偽らない）。"""
    entry_ids = model.add_paths(audio_files)

    model.apply_metadata(entry_ids[0], TrackMetadata(title="曲", duration_ms=None))

    assert display(model, 0, Column.DURATION) == ""


# -- entry_id 単位の更新 -----------------------------------------------------


def test_duplicate_paths_are_updated_separately(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じパスの 2 行を別々に更新できる。"""
    entry_ids = model.add_paths([audio_files[0], audio_files[0]])

    model.apply_metadata(entry_ids[1], SAMPLE)

    assert model.entry_at(0).metadata is None
    assert model.entry_at(1).metadata == SAMPLE


def test_update_follows_the_entry_after_a_move(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """行を移動しても entry_id で正しい行を更新する。"""
    entry_ids = model.add_paths(audio_files)
    assert model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 3) is True

    model.apply_metadata(entry_ids[0], SAMPLE)

    assert model.row_of_entry_id(entry_ids[0]) == 2
    assert model.entry_at(2).metadata == SAMPLE
    assert model.entry_at(0).metadata is None


def test_update_of_a_removed_entry_is_a_no_op(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """削除済み entry への更新は何もしない。"""
    entry_ids = model.add_paths(audio_files)
    model.removeRows(0, 1)

    assert model.apply_metadata(entry_ids[0], SAMPLE) is False
    assert model.mark_metadata_loading(entry_ids[0]) is False
    assert model.mark_metadata_failed(entry_ids[0]) is False
    assert model.clear_metadata(entry_ids[0]) is False


def test_same_value_does_not_emit(
    model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """値が変わらなければ dataChanged を出さない。"""
    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)

    with qtbot.assertNotEmitted(model.dataChanged):
        assert model.apply_metadata(entry_ids[0], SAMPLE) is False


# -- dataChanged の範囲 -----------------------------------------------------


def test_loading_notifies_only_the_status_role(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """読み取り中は表示が変わらないので状態 role だけ通知する。"""
    entry_ids = model.add_paths(audio_files)
    changes = record_changes(model)

    model.mark_metadata_loading(entry_ids[0])

    assert changes == [(Column.TITLE, Column.DURATION, (METADATA_STATUS_ROLE,))]


@pytest.mark.parametrize("action", ["loaded", "failed"])
def test_loaded_and_failed_notify_display_and_roles(
    model: PlaylistModel, audio_files: list[Path], action: str
) -> None:
    """完了・失敗ではタイトルから長さまでを表示 role とともに通知する。"""
    entry_ids = model.add_paths(audio_files)
    model.mark_metadata_loading(entry_ids[0])
    changes = record_changes(model)

    if action == "loaded":
        model.apply_metadata(entry_ids[0], SAMPLE)
    else:
        model.mark_metadata_failed(entry_ids[0])

    assert len(changes) == 1
    first_column, last_column, roles = changes[0]
    assert (first_column, last_column) == (Column.TITLE, Column.DURATION)
    assert int(Qt.ItemDataRole.DisplayRole) in roles
    assert {
        TITLE_ROLE,
        ARTIST_ROLE,
        ALBUM_ROLE,
        DURATION_MS_ROLE,
        FILE_SIZE_BYTES_ROLE,
        BITRATE_BPS_ROLE,
        METADATA_STATUS_ROLE,
    } <= set(roles)


def test_metadata_update_does_not_reset_the_model(
    model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """結果ごとに Model をリセットしない。"""
    entry_ids = model.add_paths(audio_files)

    with qtbot.assertNotEmitted(model.modelReset):
        for entry_id in entry_ids:
            model.mark_metadata_loading(entry_id)
            model.apply_metadata(entry_id, SAMPLE)


# -- ファイル状態との関係 ---------------------------------------------------


def test_metadata_is_dropped_when_the_file_disappears(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """欠損になったらメタデータを捨てて未要求へ戻す。"""
    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)
    audio_files[0].unlink()

    assert model.refresh_entry_status(entry_ids[0]) is True

    entry = model.entry_at(0)
    assert entry.is_missing
    assert entry.metadata is None
    assert entry.metadata_status is MetadataStatus.NOT_REQUESTED
    assert display(model, 0, Column.TITLE) == audio_files[0].name


def test_file_status_change_notifies_metadata_and_display_roles(
    model: PlaylistModel, audio_files: list[Path]
) -> None:
    """欠損化で破棄されるメタデータを、全関連roleと列範囲で通知する。"""
    entry_ids = model.add_paths(audio_files[:1])
    model.apply_metadata(entry_ids[0], SAMPLE)
    changes = record_changes(model)
    audio_files[0].unlink()

    assert model.refresh_entry_status(entry_ids[0]) is True

    assert len(changes) == 1
    first_column, last_column, roles = changes[0]
    assert (first_column, last_column) == (Column.TITLE, Column.PATH)
    assert {
        FILE_STATUS_ROLE,
        TITLE_ROLE,
        ARTIST_ROLE,
        ALBUM_ROLE,
        DURATION_MS_ROLE,
        FILE_SIZE_BYTES_ROLE,
        BITRATE_BPS_ROLE,
        METADATA_STATUS_ROLE,
        int(Qt.ItemDataRole.DisplayRole),
        int(Qt.ItemDataRole.ToolTipRole),
    } <= set(roles)


def test_restored_file_becomes_requestable_again(model: PlaylistModel, tmp_path: Path) -> None:
    """欠損から復活したら未要求へ戻る（再読み取りできる）。"""
    path = tmp_path / "戻る曲.wav"
    entry_ids = model.add_paths([path])
    model.refresh_file_status()
    model.mark_metadata_failed(entry_ids[0])
    path.write_bytes(b"x")

    assert model.refresh_entry_status(entry_ids[0]) is True

    assert model.entry_at(0).metadata_status is MetadataStatus.NOT_REQUESTED


# -- 永続化 -----------------------------------------------------------------


def test_replace_entries_starts_without_metadata(
    model: PlaylistModel, audio_files: list[Path], tmp_path: Path
) -> None:
    """復元したエントリはメタデータ未取得から始まる。"""
    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, model.entries())

    model.replace_entries(load_playlist(file_path))

    assert all(entry.metadata is None for entry in model.entries())
    assert all(entry.metadata_status is MetadataStatus.NOT_REQUESTED for entry in model.entries())


def test_metadata_is_not_persisted(
    model: PlaylistModel, audio_files: list[Path], tmp_path: Path
) -> None:
    """playlist.json のスキーマは変わらない。"""
    import json

    entry_ids = model.add_paths(audio_files)
    model.apply_metadata(entry_ids[0], SAMPLE)
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, model.entries())

    document = json.loads(file_path.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "entries"}
    for entry in document["entries"]:
        assert set(entry) == {"entry_id", "path"}

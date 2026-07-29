"""プレイリストの復元・保存ライフサイクルを検証する。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from sdp.core.playlist.entry import create_entry
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import load_playlist, save_playlist
from sdp.services.playlist_session import (
    RESTORE_FAILED_MESSAGE,
    PlaylistSession,
    default_playlist_path,
)


@pytest.fixture
def model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for name in ("曲 A.wav", "テスト 音源.mp3", "曲 C.flac"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


# -- 保存先 -----------------------------------------------------------------


def test_default_path_uses_local_app_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """既定の保存先は %LOCALAPPDATA%\\sdp\\playlist.json。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_playlist_path() == tmp_path / "sdp" / "playlist.json"


def test_default_path_falls_back_without_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALAPPDATA が無い環境でも決まった場所を返す。"""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    path = default_playlist_path()

    assert path.name == "playlist.json"
    assert path.parent.name == "sdp"


# -- 復元 -------------------------------------------------------------------


def test_missing_file_starts_empty(tmp_path: Path, model: PlaylistModel) -> None:
    """保存ファイルが無ければ初回起動として空で始め、保存は有効なまま。"""
    session = PlaylistSession(tmp_path / "playlist.json")

    message = session.load_into(model)

    assert message is None
    assert model.rowCount() == 0
    assert session.is_save_enabled


def test_empty_saved_playlist_replaces_existing_model(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """正常な空プレイリストを復元すると、既存の行をすべて消去する。"""
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, [])
    model.add_paths(audio_files)
    session = PlaylistSession(file_path)

    message = session.load_into(model)

    assert message is None
    assert model.rowCount() == 0
    assert session.is_save_enabled


def test_restore_preserves_order_ids_and_duplicates(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """順序・entry_id・重複行・日本語パスを維持して復元する。"""
    saved = [
        create_entry(audio_files[0]),
        create_entry(audio_files[1]),
        create_entry(audio_files[1]),
    ]
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, saved)
    session = PlaylistSession(file_path)

    message = session.load_into(model)

    assert [entry.entry_id for entry in model.entries()] == [entry.entry_id for entry in saved]
    assert [entry.path for entry in model.entries()] == [entry.path for entry in saved]
    assert model.entry_at(1).path.name == "テスト 音源.mp3"
    assert message == "プレイリストを復元しました（3件）。"


def test_restore_reevaluates_missing_files(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """欠損状態は復元時に再評価する。行は消さない。"""
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, [create_entry(audio_files[0]), create_entry(audio_files[1])])
    audio_files[1].unlink()

    PlaylistSession(file_path).load_into(model)

    assert model.rowCount() == 2
    assert not model.entry_at(0).is_missing
    assert model.entry_at(1).is_missing


# -- 破損ファイル -----------------------------------------------------------


def test_corrupted_file_starts_empty_and_disables_saving(
    tmp_path: Path, model: PlaylistModel, caplog: pytest.LogCaptureFixture
) -> None:
    """破損時は空で起動し、その起動中の保存を無効にする。"""
    file_path = tmp_path / "playlist.json"
    file_path.write_text("{壊れた", encoding="utf-8")
    session = PlaylistSession(file_path)

    with caplog.at_level(logging.ERROR):
        message = session.load_into(model)

    assert message == RESTORE_FAILED_MESSAGE
    assert model.rowCount() == 0
    assert not session.is_save_enabled
    assert "復元に失敗" in caplog.text


def test_corrupted_file_is_not_overwritten_on_save(tmp_path: Path, model: PlaylistModel) -> None:
    """復元に失敗した起動では、空のモデルで既存ファイルを上書きしない。"""
    file_path = tmp_path / "playlist.json"
    original = '{"schema_version": 1, "entries": [{"entry_id": "a"}]}'
    file_path.write_text(original, encoding="utf-8")
    session = PlaylistSession(file_path)
    session.load_into(model)

    assert session.save_from(model) is False
    assert file_path.read_text(encoding="utf-8") == original


def test_non_utf8_file_disables_saving_and_keeps_original_bytes(
    tmp_path: Path, model: PlaylistModel
) -> None:
    """文字コードとして壊れたファイルも復元失敗として保護する。"""
    file_path = tmp_path / "playlist.json"
    original = b"\x80\x81\xff"
    file_path.write_bytes(original)
    session = PlaylistSession(file_path)

    message = session.load_into(model)

    assert message == RESTORE_FAILED_MESSAGE
    assert model.rowCount() == 0
    assert not session.is_save_enabled
    assert session.save_from(model) is False
    assert file_path.read_bytes() == original


def test_read_error_is_logged_and_disables_saving(
    tmp_path: Path, model: PlaylistModel, caplog: pytest.LogCaptureFixture
) -> None:
    """読み込み I/O エラーもログへ残し、保存を無効にする。"""
    # ディレクトリを読もうとすると OSError になる。
    directory = tmp_path / "playlist.json"
    directory.mkdir()
    session = PlaylistSession(directory)

    with caplog.at_level(logging.ERROR):
        message = session.load_into(model)

    assert message == RESTORE_FAILED_MESSAGE
    assert not session.is_save_enabled
    assert "復元に失敗" in caplog.text


# -- 保存 -------------------------------------------------------------------


def test_save_writes_current_order(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """現在の並びを保存する。"""
    model.add_paths(audio_files)
    file_path = tmp_path / "playlist.json"

    assert PlaylistSession(file_path).save_from(model) is True

    assert [entry.entry_id for entry in load_playlist(file_path)] == [
        entry.entry_id for entry in model.entries()
    ]


def test_save_reflects_reordering_and_removal(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """並べ替えと削除の結果が保存される。"""
    model.add_paths(audio_files)
    file_path = tmp_path / "playlist.json"
    session = PlaylistSession(file_path)
    model.moveRows(model.index(0, 0).parent(), 0, 1, model.index(0, 0).parent(), 3)
    model.removeRows(0, 1)

    session.save_from(model)

    assert [entry.entry_id for entry in load_playlist(file_path)] == [
        entry.entry_id for entry in model.entries()
    ]


def test_save_of_cleared_playlist_writes_empty_entries(
    tmp_path: Path, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """正常な起動での全消去は、空のプレイリストとして保存する。"""
    file_path = tmp_path / "playlist.json"
    model.add_paths(audio_files)
    session = PlaylistSession(file_path)
    session.save_from(model)

    model.clear()
    assert session.save_from(model) is True

    assert load_playlist(file_path) == []


def test_save_error_is_logged_without_raising(
    tmp_path: Path,
    model: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """保存 I/O エラーはログへ残し、例外を投げない。"""
    model.add_paths(audio_files)
    session = PlaylistSession(tmp_path / "playlist.json")

    def failing_save(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("書き込みに失敗")

    monkeypatch.setattr("sdp.services.playlist_session.save_playlist", failing_save)

    with caplog.at_level(logging.ERROR):
        assert session.save_from(model) is False

    assert "保存に失敗" in caplog.text

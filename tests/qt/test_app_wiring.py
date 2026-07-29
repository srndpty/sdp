"""app.py の組み立てとプレイリスト永続化の統合を検証する。

イベントループは起動しない（無期限に待つテストを作らない）。
本番配線の確認に音声再生は不要。
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QPushButton
from pytestqt.qtbot import QtBot

from sdp import app as app_module
from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playlist.entry import create_entry
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import load_playlist, save_playlist
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services.playlist_session import RESTORE_FAILED_MESSAGE, PlaylistSession
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView


@pytest.fixture
def playlist_file(tmp_path: Path) -> Path:
    return tmp_path / "playlist.json"


@pytest.fixture
def composition(playlist_file: Path, qtbot: QtBot) -> Iterator[app_module.PlayerComposition]:
    built = app_module.build_player(playlist_file)
    qtbot.addWidget(built.window)
    yield built


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for name in ("曲 A.wav", "テスト 音源.mp3", "曲 C.flac"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


# -- 組み立て ---------------------------------------------------------------


def test_build_player_creates_every_layer(
    composition: app_module.PlayerComposition, playlist_file: Path
) -> None:
    """Backend → Controller → PlaylistModel → 永続化サービス → MainWindow。"""
    assert isinstance(composition.backend, QtMultimediaBackend)
    assert isinstance(composition.backend, PlaybackBackend)
    assert isinstance(composition.controller, PlaybackController)
    assert isinstance(composition.playlist_model, PlaylistModel)
    assert isinstance(composition.playlist_playback, PlaylistPlaybackController)
    assert isinstance(composition.playlist_session, PlaylistSession)
    assert isinstance(composition.window, MainWindow)
    assert composition.playlist_session.file_path == playlist_file


def test_window_uses_the_wired_objects(composition: app_module.PlayerComposition) -> None:
    """MainWindow の子ウィジェットが配線済みの Controller と Model を使う。"""
    controls = composition.window.findChild(PlayerControls)
    playlist_view = composition.window.findChild(PlaylistView)
    assert controls is not None
    assert playlist_view is not None

    composition.controller.set_volume(0.5)
    assert composition.backend.volume == pytest.approx(0.5, abs=1e-6)

    composition.playlist_model.add_paths([])
    assert composition.playlist_model.rowCount() == 0


def test_composition_does_not_let_the_window_own_the_backend(
    composition: app_module.PlayerComposition,
) -> None:
    """MainWindow は Backend を所有も参照もしない。"""
    assert composition.backend.parent() is None
    assert composition.backend not in composition.window.findChildren(QtMultimediaBackend)
    exposed = [name for name in dir(composition.window) if not name.startswith("_")]
    for name in exposed:
        assert not isinstance(getattr(composition.window, name), PlaybackBackend), name


def test_create_application_sets_metadata(qtbot: QtBot) -> None:
    """QApplication のメタ情報が設定される（既存インスタンスへ適用される）。"""
    del qtbot
    app = app_module.create_application([])

    assert app.applicationName() == "sdp"
    assert app.applicationDisplayName() == "sdp"
    assert app.organizationName() == "sdp"


def test_entry_point_delegates_to_app_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m sdp` は app.run を呼ぶだけで、組み立てを重複させない。"""
    from sdp import __main__ as main_module

    calls: list[str] = []
    monkeypatch.setattr(app_module, "run", lambda: calls.append("run") or 0)

    assert main_module.main() == 0
    assert calls == ["run"]


# -- 起動時復元 -------------------------------------------------------------


def test_starts_empty_without_a_saved_playlist(
    composition: app_module.PlayerComposition,
) -> None:
    """保存ファイルが無ければ空のプレイリストで起動する。"""
    assert composition.playlist_model.rowCount() == 0
    assert composition.playlist_session.is_save_enabled


def test_restores_saved_playlist(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """順序・entry_id・重複行・日本語パス・欠損行を維持して復元する。"""
    missing = audio_files[0].parent / "ない曲.wav"
    saved = [
        create_entry(audio_files[0]),
        create_entry(audio_files[1]),
        create_entry(audio_files[1]),
        create_entry(missing),
    ]
    save_playlist(playlist_file, saved)

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    entries = composition.playlist_model.entries()
    assert [entry.entry_id for entry in entries] == [entry.entry_id for entry in saved]
    assert [entry.path for entry in entries] == [entry.path for entry in saved]
    assert entries[1].path == entries[2].path
    assert entries[1].path.name == "テスト 音源.mp3"
    assert entries[3].is_missing
    assert composition.window.statusBar().currentMessage() == "プレイリストを復元しました（4件）。"


# -- 終了時保存 -------------------------------------------------------------


def test_saves_current_order_on_shutdown(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """終了処理で現在の並びが保存される。"""
    model = composition.playlist_model
    model.add_paths(audio_files)

    assert composition.playlist_session.save_from(model) is True

    assert [entry.entry_id for entry in load_playlist(playlist_file)] == [
        entry.entry_id for entry in model.entries()
    ]


def test_saves_state_after_reordering_and_removal(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """並べ替えと削除の結果が保存される。"""
    model = composition.playlist_model
    model.add_paths(audio_files)
    root = model.index(0, 0).parent()
    model.moveRows(root, 0, 1, root, 3)
    model.removeRows(0, 1)

    composition.playlist_session.save_from(model)

    assert [entry.entry_id for entry in load_playlist(playlist_file)] == [
        entry.entry_id for entry in model.entries()
    ]


def test_saves_empty_playlist_after_clear(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """正常な起動での全消去は、空のプレイリストとして保存される。"""
    model = composition.playlist_model
    model.add_paths(audio_files)
    composition.playlist_session.save_from(model)

    model.clear()
    composition.playlist_session.save_from(model)

    assert load_playlist(playlist_file) == []


# -- 破損ファイル -----------------------------------------------------------


def test_corrupted_playlist_does_not_crash_startup(
    playlist_file: Path, qtbot: QtBot, caplog: pytest.LogCaptureFixture
) -> None:
    """破損した playlist.json でもクラッシュせず、空で起動して通知する。"""
    playlist_file.write_text("{壊れた", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_model.rowCount() == 0
    assert composition.window.statusBar().currentMessage() == RESTORE_FAILED_MESSAGE
    assert "復元に失敗" in caplog.text


def test_corrupted_playlist_is_not_overwritten_on_shutdown(
    playlist_file: Path, qtbot: QtBot
) -> None:
    """復元に失敗した起動では、終了時保存で既存ファイルを上書きしない。"""
    original = '{"schema_version": 1, "entries": [{"entry_id": "a"}]}'
    playlist_file.write_text(original, encoding="utf-8")
    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_session.is_save_enabled is False
    assert composition.playlist_session.save_from(composition.playlist_model) is False
    assert playlist_file.read_text(encoding="utf-8") == original


def test_non_utf8_playlist_does_not_crash_or_get_overwritten(
    playlist_file: Path, qtbot: QtBot
) -> None:
    """非UTF-8の保存ファイルでも空で起動し、元のバイト列を保護する。"""
    original = b"\x80\x81\xff"
    playlist_file.write_bytes(original)

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_model.rowCount() == 0
    assert composition.window.statusBar().currentMessage() == RESTORE_FAILED_MESSAGE
    assert not composition.playlist_session.is_save_enabled
    assert composition.playlist_session.save_from(composition.playlist_model) is False
    assert playlist_file.read_bytes() == original


# -- プレイリスト再生の配線 --------------------------------------------------


def test_playlist_playback_shares_the_wired_model_and_controller(
    composition: app_module.PlayerComposition, audio_files: list[Path]
) -> None:
    """全層が同じ PlaylistModel と PlaybackController を共有している。"""
    entry_ids = composition.playlist_model.add_paths(audio_files)

    assert composition.playlist_playback.play_entry(entry_ids[0]) is True

    assert composition.playlist_playback.current_entry_id == entry_ids[0]
    assert composition.controller.source == audio_files[0].resolve()


def test_current_entry_is_not_persisted(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """playlist.json へ current_entry_id や再生位置を保存しない。"""
    entry_ids = composition.playlist_model.add_paths(audio_files)
    composition.playlist_playback.play_entry(entry_ids[1])

    composition.playlist_session.save_from(composition.playlist_model)

    document = json.loads(playlist_file.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "entries"}
    for entry in document["entries"]:
        assert set(entry) == {"entry_id", "path"}


def test_restored_playlist_has_no_current_entry(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """再起動しても現在 entry は復元されない。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_model.rowCount() == len(audio_files)
    assert composition.playlist_playback.current_entry_id is None


def test_restored_playlist_initializes_navigation_buttons(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """復元通知がWindow構築前でも前後ボタンへ現在値を反映する。"""
    saved = [create_entry(path) for path in audio_files]
    save_playlist(playlist_file, saved)

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)
    previous = composition.window.findChild(QPushButton, "previousTrackButton")
    next_ = composition.window.findChild(QPushButton, "nextTrackButton")
    assert previous is not None
    assert next_ is not None

    assert composition.playlist_playback.current_entry_id is None
    assert previous.isEnabled()
    assert next_.isEnabled()

    next_.click()
    assert composition.playlist_playback.current_entry_id == saved[0].entry_id

"""app.py の組み立てとプレイリスト永続化の統合を検証する。

イベントループは起動しない（無期限に待つテストを作らない）。
本番配線の確認に音声再生は不要。
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QDoubleSpinBox, QPushButton
from pytestqt.qtbot import QtBot

from sdp import app as app_module
from sdp.core.metadata.reader import MetadataReader
from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playlist.entry import create_entry
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import load_playlist, save_playlist
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.services.playlist_session import RESTORE_FAILED_MESSAGE, PlaylistSession
from sdp.services.settings import (
    RESTORE_FAILED_MESSAGE as SETTINGS_RESTORE_FAILED_MESSAGE,
)
from sdp.services.settings import (
    AppSettings,
    SettingsSession,
    save_settings,
)
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView
from sdp.ui.speed_panel import SpeedPanel


@pytest.fixture
def playlist_file(tmp_path: Path) -> Path:
    return tmp_path / "playlist.json"


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.fixture(autouse=True)
def isolate_default_settings_path(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストが実ユーザーの設定を読み書きしないよう保存先を隔離する。"""
    monkeypatch.setattr(app_module, "default_settings_path", lambda: settings_file)


@pytest.fixture
def composition(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> Iterator[app_module.PlayerComposition]:
    built = app_module.build_player(playlist_file, settings_file)
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
    composition: app_module.PlayerComposition, playlist_file: Path, settings_file: Path
) -> None:
    """Backend → Controller → PlaylistModel → 永続化サービス → MainWindow。"""
    assert isinstance(composition.backend, QtMultimediaBackend)
    assert isinstance(composition.backend, PlaybackBackend)
    assert isinstance(composition.controller, PlaybackController)
    assert isinstance(composition.playlist_model, PlaylistModel)
    assert isinstance(composition.playlist_playback, PlaylistPlaybackController)
    assert isinstance(composition.playlist_session, PlaylistSession)
    assert isinstance(composition.settings_session, SettingsSession)
    assert isinstance(composition.window, MainWindow)
    assert composition.playlist_session.file_path == playlist_file
    assert composition.settings_session.file_path == settings_file


def test_window_uses_the_wired_objects(composition: app_module.PlayerComposition) -> None:
    """MainWindow の子ウィジェットが配線済みの Controller と Model を使う。"""
    controls = composition.window.findChild(PlayerControls)
    speed_panel = composition.window.findChild(SpeedPanel)
    playlist_view = composition.window.findChild(PlaylistView)
    assert controls is not None
    assert speed_panel is not None
    assert playlist_view is not None

    composition.controller.set_volume(0.5)
    assert composition.backend.volume == pytest.approx(0.5, abs=1e-6)

    composition.playlist_model.add_paths([])
    assert composition.playlist_model.rowCount() == 0


def test_build_player_reflects_controller_speed_and_pitch(
    composition: app_module.PlayerComposition,
) -> None:
    """本番配線のSpeedPanelは同じControllerの状態を表示する。"""
    spin_box = composition.window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    pitch = composition.window.findChild(QCheckBox, "pitchCompensationCheckBox")
    assert spin_box is not None
    assert pitch is not None

    composition.controller.set_playback_rate(1.25)
    composition.controller.set_pitch_compensation(False)

    assert spin_box.value() == 1.25
    assert pitch.isChecked() is False
    assert composition.backend.playback_rate == pytest.approx(1.25, rel=1e-6)
    assert composition.backend.pitch_compensation is False


def test_build_player_restores_settings_before_building_speed_panel(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """保存値をControllerへ適用してから、その真値でSpeedPanelを構築する。"""
    save_settings(settings_file, AppSettings(1.35, False))

    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)
    spin_box = composition.window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    pitch = composition.window.findChild(QCheckBox, "pitchCompensationCheckBox")
    assert spin_box is not None
    assert pitch is not None

    assert composition.controller.playback_rate == pytest.approx(1.35)
    assert composition.controller.pitch_compensation is False
    assert spin_box.value() == pytest.approx(1.35)
    assert pitch.isChecked() is False
    assert composition.settings_session.is_running is False


def test_settings_round_trip_does_not_restore_unscoped_playback_state(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """速度・ピッチ以外の音量、mute、repeat、shuffle、現在曲は復元しない。"""
    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)
    composition.settings_session.start()
    composition.controller.set_playback_rate(1.4)
    composition.controller.set_pitch_compensation(False)
    composition.controller.set_volume(0.2)
    composition.controller.set_muted(True)
    composition.playlist_playback.set_repeat_mode(RepeatMode.ALL)
    composition.playlist_playback.set_shuffle_enabled(True)
    assert composition.settings_session.flush() is True
    composition.settings_session.stop()

    restored = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(restored.window)

    assert restored.controller.playback_rate == pytest.approx(1.4)
    assert restored.controller.pitch_compensation is False
    assert restored.controller.volume == pytest.approx(1.0)
    assert restored.controller.muted is False
    assert restored.playlist_playback.repeat_mode is RepeatMode.OFF
    assert restored.playlist_playback.shuffle_enabled is False
    assert restored.playlist_playback.current_entry_id is None


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


def test_run_starts_flushes_and_stops_settings_session(
    playlist_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """イベントループ前に監視を開始し、正常終了時にflushしてから停止する。"""
    del qtbot
    events: list[str] = []
    original_start = SettingsSession.start
    original_flush = SettingsSession.flush
    original_stop = SettingsSession.stop

    def record_start(session: SettingsSession) -> None:
        events.append("start")
        original_start(session)

    def record_flush(session: SettingsSession) -> bool:
        events.append("flush")
        return original_flush(session)

    def record_stop(session: SettingsSession) -> None:
        events.append("stop")
        original_stop(session)

    def ignore_logging_setup() -> None:
        return None

    def injected_playlist_path() -> Path:
        return playlist_file

    class ImmediateApplication:
        def exec(self) -> int:
            events.append("exec")
            return 0

    def create_immediate_application(argv: list[str]) -> QApplication:
        del argv
        return cast(QApplication, ImmediateApplication())

    monkeypatch.setattr(SettingsSession, "start", record_start)
    monkeypatch.setattr(SettingsSession, "flush", record_flush)
    monkeypatch.setattr(SettingsSession, "stop", record_stop)
    monkeypatch.setattr(app_module, "default_playlist_path", injected_playlist_path)
    monkeypatch.setattr(app_module, "create_application", create_immediate_application)
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", ignore_logging_setup)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", ignore_logging_setup)
    assert app_module.run([]) == 0
    assert events == ["start", "exec", "flush", "stop"]


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


def test_corrupted_settings_uses_defaults_and_is_not_overwritten(
    playlist_file: Path,
    settings_file: Path,
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """破損設定では既定値で起動し、終了相当のflushでも元ファイルを保護する。"""
    original = "{壊れた"
    settings_file.write_text(original, encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)

    assert composition.controller.playback_rate == pytest.approx(1.0)
    assert composition.controller.pitch_compensation is True
    assert composition.window.statusBar().currentMessage() == SETTINGS_RESTORE_FAILED_MESSAGE
    assert composition.settings_session.is_save_enabled is False
    composition.settings_session.start()
    composition.controller.set_playback_rate(1.25)
    assert composition.settings_session.flush() is False
    assert settings_file.read_text(encoding="utf-8") == original
    assert "設定の復元に失敗" in caplog.text


def test_corrupted_settings_does_not_disable_playlist_saving(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    settings_file: Path,
    audio_files: list[Path],
) -> None:
    """設定失敗とプレイリスト保存失敗は独立させる。"""
    # fixture構築後なので、破損状態を独立したsessionへ読み込ませる。
    settings_file.write_text("{壊れた", encoding="utf-8")
    failed_session = SettingsSession(settings_file, composition.controller)
    assert failed_session.load() == SETTINGS_RESTORE_FAILED_MESSAGE

    composition.playlist_model.add_paths(audio_files)
    assert composition.playlist_session.save_from(composition.playlist_model) is True
    assert len(load_playlist(playlist_file)) == len(audio_files)


def test_corrupted_playlist_does_not_disable_settings_saving(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """プレイリスト復元失敗後も速度・ピッチ設定は保存できる。"""
    playlist_file.write_text("{壊れた", encoding="utf-8")
    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)
    composition.settings_session.start()

    composition.controller.set_playback_rate(1.2)
    composition.controller.set_pitch_compensation(False)

    assert composition.playlist_session.is_save_enabled is False
    assert composition.settings_session.flush() is True
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "playback_rate": 1.2,
        "pitch_compensation": False,
    }


def test_both_corrupted_files_report_and_preserve_both_failures(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """同時破損では両方を通知し、終了相当の保存でも両ファイルを保護する。"""
    playlist_original = "{壊れたプレイリスト"
    settings_original = "{壊れた設定"
    playlist_file.write_text(playlist_original, encoding="utf-8")
    settings_file.write_text(settings_original, encoding="utf-8")

    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)
    message = composition.window.statusBar().currentMessage()

    assert composition.playlist_session.is_save_enabled is False
    assert composition.settings_session.is_save_enabled is False
    assert "プレイリスト" in message
    assert "設定" in message
    assert composition.settings_session.flush() is False
    assert composition.playlist_session.save_from(composition.playlist_model) is False
    assert playlist_file.read_text(encoding="utf-8") == playlist_original
    assert settings_file.read_text(encoding="utf-8") == settings_original


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


# -- リピート・シャッフル ---------------------------------------------------


def test_production_wiring_uses_a_non_seeded_rng(
    composition: app_module.PlayerComposition, audio_files: list[Path]
) -> None:
    """本番構成は固定 seed を使わない（初期状態は OFF / シャッフルなし）。"""
    assert composition.playlist_playback.repeat_mode is RepeatMode.OFF
    assert composition.playlist_playback.shuffle_enabled is False
    del audio_files


def test_repeat_and_shuffle_are_not_persisted(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """playlist.json へ repeat / shuffle / history を書かない。"""
    entry_ids = composition.playlist_model.add_paths(audio_files)
    composition.playlist_playback.set_repeat_mode(RepeatMode.ALL)
    composition.playlist_playback.set_shuffle_enabled(True)
    composition.playlist_playback.play_entry(entry_ids[0])

    composition.playlist_session.save_from(composition.playlist_model)

    document = json.loads(playlist_file.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "entries"}
    for entry in document["entries"]:
        assert set(entry) == {"entry_id", "path"}


def test_repeat_and_shuffle_reset_on_restart(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """再起動で repeat は OFF、shuffle は False へ戻る。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_model.rowCount() == len(audio_files)
    assert composition.playlist_playback.repeat_mode is RepeatMode.OFF
    assert composition.playlist_playback.shuffle_enabled is False
    assert composition.playlist_playback.current_entry_id is None


# -- メタデータ -------------------------------------------------------------


def test_composition_holds_the_metadata_reader(
    composition: app_module.PlayerComposition,
) -> None:
    """MetadataReader を composition が保持し、build だけでは動き出さない。"""
    assert isinstance(composition.metadata_reader, MetadataReader)
    assert composition.metadata_reader.is_running is False


def test_build_player_does_not_start_background_work(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """build_player だけでは読み取りを始めない（既存テストを不安定にしない）。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])

    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)

    assert all(
        entry.metadata_status is MetadataStatus.NOT_REQUESTED
        for entry in composition.playlist_model.entries()
    )


def test_started_reader_reads_restored_and_added_entries(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """start 後は復元済みエントリも追加分も読み取る。"""
    save_playlist(playlist_file, [create_entry(audio_files[0])])
    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)
    model = composition.playlist_model

    composition.metadata_reader.start()

    qtbot.waitUntil(
        lambda: model.entry_at(0).metadata_status in (MetadataStatus.LOADED, MetadataStatus.FAILED),
        timeout=10_000,
    )
    model.add_paths([audio_files[1]])
    qtbot.waitUntil(
        lambda: model.entry_at(1).metadata_status in (MetadataStatus.LOADED, MetadataStatus.FAILED),
        timeout=10_000,
    )
    composition.metadata_reader.shutdown(timeout_ms=2_000)
    assert composition.metadata_reader.is_running is False


def test_metadata_is_not_persisted_and_is_reread_after_restart(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """メタデータは保存されず、再起動後は未取得から始まる。"""
    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)
    entry_ids = composition.playlist_model.add_paths(audio_files)
    composition.playlist_model.apply_metadata(
        entry_ids[0], TrackMetadata(title="保存されない", duration_ms=1000)
    )
    composition.playlist_session.save_from(composition.playlist_model)

    document = json.loads(playlist_file.read_text(encoding="utf-8"))
    assert set(document) == {"schema_version", "entries"}
    for entry in document["entries"]:
        assert set(entry) == {"entry_id", "path"}

    restored = app_module.build_player(playlist_file)
    qtbot.addWidget(restored.window)
    assert all(
        entry.metadata is None and entry.metadata_status is MetadataStatus.NOT_REQUESTED
        for entry in restored.playlist_model.entries()
    )


def test_metadata_failure_does_not_disable_saving(
    composition: app_module.PlayerComposition, playlist_file: Path, audio_files: list[Path]
) -> None:
    """メタデータ失敗でプレイリスト保存は無効にならない。"""
    entry_ids = composition.playlist_model.add_paths(audio_files)
    composition.playlist_model.mark_metadata_failed(entry_ids[0])

    assert composition.playlist_session.is_save_enabled is True
    assert composition.playlist_session.save_from(composition.playlist_model) is True
    assert len(load_playlist(playlist_file)) == len(audio_files)


def test_ui_layer_does_not_import_mutagen() -> None:
    """UI 層は Mutagen も MetadataReader も知らない。"""
    from sdp.ui import main_window as main_window_module
    from sdp.ui import playlist_view as playlist_view_module

    for module in (main_window_module, playlist_view_module):
        for forbidden in ("mutagen", "MetadataReader", "read_track_metadata"):
            assert not hasattr(module, forbidden), f"{module.__name__}: {forbidden}"

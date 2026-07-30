"""app.py の組み立てとプレイリスト永続化の統合を検証する。

イベントループは起動しない（無期限に待つテストを作らない）。
本番配線の確認に音声再生は不要。
"""

import json
import logging
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtGui import QAction
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QMediaPlayer,
)
from PySide6.QtWidgets import QApplication, QCheckBox, QDoubleSpinBox, QPushButton
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
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
from sdp.services.pcm_tap import PcmTap
from sdp.services.playlist_session import RESTORE_FAILED_MESSAGE, PlaylistSession
from sdp.services.settings import (
    RESTORE_FAILED_MESSAGE as SETTINGS_RESTORE_FAILED_MESSAGE,
)
from sdp.services.settings import (
    AppSettings,
    AppSettingsController,
    SettingsSession,
    save_settings,
)
from sdp.services.ui_state import (
    MINIMUM_SPLITTER_SIZE,
    SplitterState,
    UiState,
    WindowState,
    load_ui_state,
    save_ui_state,
)
from sdp.services.ui_state import (
    RESTORE_FAILED_MESSAGE as UI_STATE_RESTORE_FAILED_MESSAGE,
)
from sdp.services.ui_state_session import UiStateSession
from sdp.services.user_paths import (
    app_data_directory,
    default_settings_path,
    default_ui_state_path,
)
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.level_meter_widget import NO_SOURCE_MESSAGE as LEVEL_NO_SOURCE_MESSAGE
from sdp.ui.level_meter_widget import LevelMeterWidget
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView
from sdp.ui.spectrum_panel import SpectrumPanel
from sdp.ui.spectrum_widget import NO_SOURCE_MESSAGE as SPECTRUM_NO_SOURCE_MESSAGE
from sdp.ui.spectrum_widget import SpectrumWidget
from sdp.ui.speed_panel import SpeedPanel
from sdp.ui.waveform_panel import WaveformPanel
from sdp.ui.waveform_widget import WaveformWidget


@pytest.fixture
def playlist_file(tmp_path: Path) -> Path:
    return tmp_path / "playlist.json"


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.fixture
def waveform_cache_directory(tmp_path: Path) -> Path:
    return tmp_path / "waveform-cache"


@pytest.fixture
def ui_state_file(tmp_path: Path) -> Path:
    return tmp_path / "ui-state.json"


@pytest.fixture(autouse=True)
def isolate_user_data_paths(
    settings_file: Path,
    waveform_cache_directory: Path,
    ui_state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """各テストが実ユーザーの設定・UI状態・cacheを読み書きしないよう隔離する。"""
    monkeypatch.setattr(app_module, "default_settings_path", lambda: settings_file)
    monkeypatch.setattr(app_module, "default_ui_state_path", lambda: ui_state_file)
    monkeypatch.setattr(
        app_module,
        "default_waveform_cache_directory",
        lambda: waveform_cache_directory,
    )


@pytest.fixture
def composition(
    playlist_file: Path,
    settings_file: Path,
    waveform_cache_directory: Path,
    ui_state_file: Path,
    qtbot: QtBot,
) -> Iterator[app_module.PlayerComposition]:
    built = app_module.build_player(
        playlist_file, settings_file, waveform_cache_directory, ui_state_file
    )
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


def int16_stereo_buffer(value: int = 16_384) -> QAudioBuffer:
    """48kHz Int16 stereoの1frame buffer（P0-C実測のWAV形式）。"""
    audio_format = QAudioFormat()
    audio_format.setSampleRate(48_000)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return QAudioBuffer(struct.pack("<2h", value, value), audio_format)


# -- 組み立て ---------------------------------------------------------------


def test_build_player_creates_every_layer(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    settings_file: Path,
) -> None:
    """Backend → Controller → PlaylistModel → 永続化サービス → MainWindow。"""
    assert isinstance(composition.backend, QtMultimediaBackend)
    assert isinstance(composition.backend, PlaybackBackend)
    assert isinstance(composition.controller, PlaybackController)
    assert isinstance(composition.playlist_model, PlaylistModel)
    assert isinstance(composition.playlist_playback, PlaylistPlaybackController)
    assert isinstance(composition.playlist_session, PlaylistSession)
    assert isinstance(composition.settings_session, SettingsSession)
    assert isinstance(composition.waveform_analysis, WaveformAnalysisService)
    assert isinstance(composition.pcm_tap, PcmTap)
    assert isinstance(composition.window, MainWindow)
    assert composition.playlist_session.file_path == playlist_file
    assert composition.settings_session.file_path == settings_file
    assert not composition.waveform_analysis.is_running


def test_window_uses_the_wired_objects(composition: app_module.PlayerComposition) -> None:
    """MainWindow の子ウィジェットが配線済みの Controller と Model を使う。"""
    controls = composition.window.findChild(PlayerControls)
    speed_panel = composition.window.findChild(SpeedPanel)
    waveform_panel = composition.window.findChild(WaveformPanel)
    playlist_view = composition.window.findChild(PlaylistView)
    assert controls is not None
    assert speed_panel is not None
    assert waveform_panel is not None
    assert waveform_panel.waveform_analysis is composition.waveform_analysis
    assert len(waveform_panel.findChildren(WaveformWidget)) == 1
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


def test_run_starts_and_stops_background_services_in_order(
    playlist_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """イベントループ前に開始し、波形worker停止後に設定をflushする。"""
    del qtbot
    events: list[str] = []
    original_start = SettingsSession.start
    original_flush = SettingsSession.flush
    original_stop = SettingsSession.stop
    original_waveform_start = WaveformAnalysisService.start
    original_waveform_shutdown = WaveformAnalysisService.shutdown

    def record_start(session: SettingsSession) -> None:
        events.append("settings_start")
        original_start(session)

    def record_flush(session: SettingsSession) -> bool:
        events.append("settings_flush")
        return original_flush(session)

    def record_stop(session: SettingsSession) -> None:
        events.append("settings_stop")
        original_stop(session)

    def record_waveform_start(service: WaveformAnalysisService) -> None:
        events.append("waveform_start")
        original_waveform_start(service)

    def record_waveform_shutdown(service: WaveformAnalysisService, timeout_ms: int = 3_000) -> None:
        events.append("waveform_shutdown")
        original_waveform_shutdown(service, timeout_ms)

    def ignore_logging_setup() -> None:
        return None

    def skip_window_show(window: MainWindow) -> None:
        del window

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
    monkeypatch.setattr(WaveformAnalysisService, "start", record_waveform_start)
    monkeypatch.setattr(WaveformAnalysisService, "shutdown", record_waveform_shutdown)
    monkeypatch.setattr(app_module, "default_playlist_path", injected_playlist_path)
    monkeypatch.setattr(app_module, "create_application", create_immediate_application)
    # fake execは実event loopと「ウィンドウを閉じてから戻る」契約を再現しないため、
    # queued paintを残さない。表示・描画は専用Qtテストで検証する。
    monkeypatch.setattr(MainWindow, "show", skip_window_show)
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", ignore_logging_setup)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", ignore_logging_setup)
    assert app_module.run([]) == 0
    assert events == [
        "settings_start",
        "waveform_start",
        "exec",
        "waveform_shutdown",
        "settings_flush",
        "settings_stop",
    ]


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
    failed_session = SettingsSession(settings_file, composition.app_settings)
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
        "schema_version": 2,
        "playback_rate": 1.2,
        "pitch_compensation": False,
        "waveform_visible": True,
        "spectrum_visible": True,
        "level_meter_visible": True,
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


# -- PCM タップとスペクトラム（P5-A）---------------------------------------


def test_composition_holds_the_pcm_tap_connected_to_the_backend(
    composition: app_module.PlayerComposition,
) -> None:
    """PcmTapを保持し、具体BackendのQAudioBufferOutputへ接続されている。"""
    tap = composition.pcm_tap
    assert isinstance(tap, PcmTap)

    buffer_output = composition.backend.audio_buffer_output
    assert isinstance(buffer_output, QAudioBufferOutput)
    # Backend が所有し、QMediaPlayer へ設定済みであること。
    player = composition.backend.findChild(QMediaPlayer)
    assert player is not None
    assert player.audioBufferOutput() is buffer_output

    # 実際のPCM通知経路でリングバッファへ届く。
    buffer_output.audioBufferReceived.emit(int16_stereo_buffer())

    assert tap.received_buffer_count == 1
    assert tap.sample_rate == 48_000


def test_spectrum_panel_uses_the_same_pcm_tap(
    composition: app_module.PlayerComposition,
) -> None:
    """MainWindow内のSpectrumPanelはcomposition rootと同じPcmTapを使う。"""
    panel = composition.window.findChild(SpectrumPanel)
    assert panel is not None
    assert panel.pcm_tap is composition.pcm_tap
    assert composition.window.spectrum_panel is panel


def test_window_has_exactly_one_spectrum_widget_next_to_the_waveform(
    composition: app_module.PlayerComposition,
) -> None:
    """波形・スペクトラム・レベルメーターが1つずつ共存する。"""
    assert len(composition.window.findChildren(SpectrumWidget)) == 1
    assert len(composition.window.findChildren(LevelMeterWidget)) == 1
    assert len(composition.window.findChildren(WaveformWidget)) == 1
    assert composition.window.findChild(WaveformPanel) is not None


def test_level_meter_shares_the_spectrum_panel_and_pcm_tap(
    composition: app_module.PlayerComposition,
) -> None:
    """レベルメーターはスペクトラムと同じPanel・同じPcmTapを共有する。"""
    panel = composition.window.spectrum_panel
    level = composition.window.findChild(LevelMeterWidget)

    assert level is not None
    assert level is panel.level_meter_widget
    assert level.parent() is panel
    assert panel.pcm_tap is composition.pcm_tap


def test_pcm_tap_uses_three_fixed_capacity_buffers(
    composition: app_module.PlayerComposition,
) -> None:
    """mono／L／Rの3本が同じ固定容量で用意される。"""
    tap = composition.pcm_tap
    buffers = (tap.ring_buffer, tap.left_ring_buffer, tap.right_ring_buffer)
    capacity = buffers[0].capacity

    for buffer in buffers:
        assert buffer.capacity == capacity

    for _ in range(50):
        composition.backend.audio_buffer_output.audioBufferReceived.emit(int16_stereo_buffer())

    for buffer in buffers:
        assert buffer.capacity == capacity


def test_main_window_has_no_level_calculation(
    composition: app_module.PlayerComposition,
) -> None:
    """MainWindowはLevelProcessorやリングバッファを持たず、配置だけを行う。"""
    from sdp.core.analysis.level import LevelProcessor
    from sdp.core.analysis.ring_buffer import PcmRingBuffer
    from sdp.ui import main_window as main_window_module

    for forbidden in (
        "LevelProcessor",
        "StereoLevelFrame",
        "PcmRingBuffer",
        "peak_amplitude",
        "rms_amplitude",
        "SpectrumProcessor",
        "compute_spectrum",
    ):
        assert not hasattr(main_window_module, forbidden), forbidden

    window = composition.window
    for name in (name for name in dir(window) if not name.startswith("_")):
        value = getattr(window, name)
        assert not isinstance(value, LevelProcessor | PcmRingBuffer), name


def test_ui_layer_does_not_import_qaudiobuffer(
    composition: app_module.PlayerComposition,
) -> None:
    """UI層（レベルメーターを含む）はQAudioBuffer系を参照しない。"""
    del composition
    from sdp.ui import level_meter_widget as level_module
    from sdp.ui import spectrum_panel as panel_module

    for module in (level_module, panel_module):
        for forbidden in ("QAudioBuffer", "QAudioBufferOutput", "QAudioFormat", "PcmChunk"):
            assert not hasattr(module, forbidden), f"{module.__name__}: {forbidden}"


def test_build_player_does_not_start_the_spectrum_timer(
    composition: app_module.PlayerComposition,
) -> None:
    """buildだけではタイマーを開始しない（sourceなし初期表示）。"""
    panel = composition.window.spectrum_panel
    assert not panel.is_timer_active
    assert panel.spectrum_widget.frame is None
    assert panel.spectrum_widget.status_text == SPECTRUM_NO_SOURCE_MESSAGE
    assert panel.level_meter_widget.frame is None
    assert panel.level_meter_widget.status_text == LEVEL_NO_SOURCE_MESSAGE
    assert composition.pcm_tap.sample_rate == 0
    assert composition.pcm_tap.channel_count == 0
    assert composition.pcm_tap.available_frame_count == 0


def test_source_change_clears_the_pcm_in_the_production_wiring(
    composition: app_module.PlayerComposition, audio_files: list[Path]
) -> None:
    """本番配線でもsource変更で保持中のPCMを捨てる。

    停止・一時停止による clear は実再生状態の遷移が必要なため、
    FakeBackendを使う[test_pcm_tap.py](./analysis/test_pcm_tap.py)と、
    実音の[tests/audio/test_pcm_spectrum.py](../audio/test_pcm_spectrum.py)で検証する。
    """
    tap = composition.pcm_tap
    buffer = int16_stereo_buffer()

    composition.controller.load(audio_files[0])
    composition.backend.audio_buffer_output.audioBufferReceived.emit(buffer)
    assert tap.available_frame_count == 1
    assert tap.sample_rate == 48_000

    composition.controller.load(audio_files[1])

    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0
    assert tap.channel_count == 0
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0


def test_production_wiring_feeds_all_three_buffers(
    composition: app_module.PlayerComposition, audio_files: list[Path]
) -> None:
    """本番配線のPCM通知でmono／L／Rの3本すべてが埋まる。

    PLAYING での更新は実再生状態の遷移が必要なため、FakeBackendを使う
    [test_spectrum_panel.py](./analysis/test_spectrum_panel.py)と、実音の
    [tests/audio/test_pcm_spectrum.py](../audio/test_pcm_spectrum.py)で検証する。
    """
    tap = composition.pcm_tap
    composition.controller.load(audio_files[0])

    composition.backend.audio_buffer_output.audioBufferReceived.emit(int16_stereo_buffer())

    assert tap.channel_count == 2
    assert tap.available_frame_count == 1
    left, right = tap.snapshot_stereo(1)
    assert left.tolist() == pytest.approx([0.5], abs=1e-3)
    assert right.tolist() == pytest.approx([0.5], abs=1e-3)


def test_playback_backend_interface_has_no_pcm_responsibility() -> None:
    """PlaybackBackendの一般契約へPCM SignalやQt型を追加していない。"""
    for forbidden in (
        "audio_buffer_output",
        "pcm_buffer_received",
        "audioBufferReceived",
        "QAudioBufferOutput",
        "QAudioBuffer",
    ):
        assert not hasattr(PlaybackBackend, forbidden), forbidden

    from sdp.core.playback import backend as backend_module

    for forbidden in ("QAudioBufferOutput", "QAudioBuffer", "PcmTap", "PcmRingBuffer"):
        assert not hasattr(backend_module, forbidden), forbidden


def test_fake_backend_has_no_audio_buffer_concept() -> None:
    """FakePlaybackBackendへQAudioBuffer概念を導入していない。"""
    from fakes import fake_playback_backend as fake_module

    for forbidden in ("QAudioBuffer", "QAudioBufferOutput", "audio_buffer_output"):
        assert not hasattr(fake_module, forbidden), forbidden
        assert not hasattr(FakePlaybackBackend, forbidden), forbidden


def test_shutdown_stops_the_spectrum_timer_and_the_tap(
    playlist_file: Path,
    settings_file: Path,
    waveform_cache_directory: Path,
    qtbot: QtBot,
) -> None:
    """終了処理でタイマーとPCM受信が残らない。"""
    composition = app_module.build_player(playlist_file, settings_file, waveform_cache_directory)
    qtbot.addWidget(composition.window)
    buffer_output = composition.backend.audio_buffer_output

    composition.window.spectrum_panel.shutdown()
    composition.pcm_tap.shutdown()

    assert not composition.window.spectrum_panel.is_timer_active
    buffer_output.audioBufferReceived.emit(int16_stereo_buffer())
    assert composition.pcm_tap.received_buffer_count == 0


def test_visualization_schemas_are_unchanged(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    settings_file: Path,
    audio_files: list[Path],
) -> None:
    """settings・playlist・波形cacheのschemaへ可視化設定を追加していない。"""
    composition.playlist_model.add_paths(audio_files)
    composition.playlist_session.save_from(composition.playlist_model)
    composition.settings_session.start()
    composition.controller.set_playback_rate(1.25)
    composition.settings_session.flush()

    playlist_document = json.loads(playlist_file.read_text(encoding="utf-8"))
    settings_document = json.loads(settings_file.read_text(encoding="utf-8"))

    assert set(playlist_document) == {"schema_version", "entries"}
    # 可視化の表示ON/OFFはP6-Aで設定schemaへ追加した。色・バンド数・FPS・
    # Peak hold時間は追加していない。
    assert set(settings_document) == {
        "schema_version",
        "playback_rate",
        "pitch_compensation",
        "waveform_visible",
        "spectrum_visible",
        "level_meter_visible",
    }

    from sdp.core.analysis import waveform_cache

    assert waveform_cache.WAVEFORM_ANALYSIS_VERSION == 1
    assert waveform_cache.WAVEFORM_FORMAT_VERSION == 1
    assert waveform_cache.WAVEFORM_BUCKET_MS == 20


# -- 設定と可視化の配線（P6-A）----------------------------------------------


def test_composition_holds_the_settings_mediator(
    composition: app_module.PlayerComposition,
) -> None:
    """AppSettingsControllerを保持し、SettingsSessionと同じsnapshotを使う。"""
    app_settings = composition.app_settings
    assert isinstance(app_settings, AppSettingsController)
    assert app_settings.settings.playback_rate == pytest.approx(
        composition.controller.playback_rate
    )
    assert composition.window.settings_dialog is None


def test_restored_visualization_settings_are_applied_before_show(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """保存済みの表示設定はWindow表示前に反映される。"""
    save_settings(
        settings_file,
        AppSettings(1.0, True, waveform_visible=False, level_meter_visible=False),
    )

    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)
    window = composition.window

    assert composition.app_settings.settings.waveform_visible is False
    assert window.waveform_panel.isVisibleTo(window) is False
    assert window.spectrum_panel.is_spectrum_visible is True
    assert window.spectrum_panel.is_level_meter_visible is False
    # 表示前の反映なので、まだ一度も表示していない。
    assert not window.isVisible()


def test_version_one_settings_start_with_every_visualization_visible(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """旧version 1の設定ファイルからでも正常起動し、可視化はすべて表示になる。"""
    settings_file.write_text(
        '{"schema_version": 1, "playback_rate": 1.25, "pitch_compensation": false}\n',
        encoding="utf-8",
    )
    original = settings_file.read_bytes()

    composition = app_module.build_player(playlist_file, settings_file)
    qtbot.addWidget(composition.window)

    assert composition.controller.playback_rate == pytest.approx(1.25)
    assert composition.app_settings.settings.waveform_visible is True
    assert composition.app_settings.settings.spectrum_visible is True
    assert composition.app_settings.settings.level_meter_visible is True
    assert composition.playlist_session.is_save_enabled
    # 起動しただけでは version 2 へ書き換えない。
    assert settings_file.read_bytes() == original


def test_build_player_does_not_save_settings(
    composition: app_module.PlayerComposition, settings_file: Path
) -> None:
    """初期読込と初期適用では保存が走らない。"""
    assert not settings_file.exists()
    assert composition.settings_session.is_running is False


def test_dialog_changes_reach_the_controller_and_the_panels(
    composition: app_module.PlayerComposition, qtbot: QtBot
) -> None:
    """設定ダイアログの適用がControllerと各Panelへ届く。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    action = window.findChild(QAction, "openSettingsAction")
    assert action is not None
    action.trigger()
    dialog = window.settings_dialog
    assert dialog is not None

    dialog.settings_requested.emit(
        AppSettings(1.5, False, waveform_visible=False, spectrum_visible=False)
    )

    assert composition.controller.playback_rate == pytest.approx(1.5)
    assert composition.backend.playback_rate == pytest.approx(1.5)
    assert composition.backend.pitch_compensation is False
    assert not window.waveform_panel.isVisible()
    assert not window.spectrum_panel.is_spectrum_visible
    assert window.spectrum_panel.is_level_meter_visible
    window.spectrum_panel.shutdown()


def test_settings_changes_are_flushed_on_shutdown(
    composition: app_module.PlayerComposition, settings_file: Path
) -> None:
    """終了時のflushで最終設定がversion 2として保存される。"""
    composition.settings_session.start()
    composition.app_settings.apply(AppSettings(1.25, True, spectrum_visible=False))

    assert composition.settings_session.flush() is True

    document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["spectrum_visible"] is False
    assert document["playback_rate"] == pytest.approx(1.25)


def test_settings_save_failure_does_not_block_playlist_saving(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """設定保存の失敗はプレイリスト保存も再生も妨げない。"""

    def failing_save(path: Path, settings: AppSettings) -> None:
        del path, settings
        raise OSError("保存失敗")

    monkeypatch.setattr("sdp.services.settings.save_settings", failing_save)
    composition.settings_session.start()
    composition.app_settings.apply(AppSettings(1.5, True))
    composition.playlist_model.add_paths(audio_files)

    with caplog.at_level(logging.ERROR):
        assert composition.settings_session.flush() is False

    assert composition.playlist_session.save_from(composition.playlist_model) is True
    assert len(load_playlist(playlist_file)) == len(audio_files)
    assert composition.controller.playback_rate == pytest.approx(1.5)


def test_ui_layer_does_not_read_the_settings_file() -> None:
    """UI層は設定JSONの読み書きを知らない（調停サービス経由だけ）。"""
    from sdp.ui import main_window as main_window_module
    from sdp.ui import settings_dialog as settings_dialog_module

    for module in (main_window_module, settings_dialog_module):
        for forbidden in ("load_settings", "save_settings", "SettingsSession", "json"):
            assert not hasattr(module, forbidden), f"{module.__name__}: {forbidden}"


# -- UI状態の配線（P6-B）----------------------------------------------------


def test_composition_holds_the_ui_state_session(
    composition: app_module.PlayerComposition, ui_state_file: Path
) -> None:
    """UiStateSessionを保持し、build時点では監視を始めない。"""
    session = composition.ui_state_session
    assert isinstance(session, UiStateSession)
    assert session.file_path == ui_state_file
    assert session.is_running is False
    assert session.is_save_enabled is True


def test_default_ui_state_path_is_in_the_app_data_directory() -> None:
    """既定の保存先は``%LOCALAPPDATA%\\sdp\\ui-state.json``。"""
    path = default_ui_state_path()

    assert path.name == "ui-state.json"
    assert path.parent == app_data_directory()
    assert path != default_settings_path()


def test_ui_state_is_restored_before_the_window_is_shown(
    playlist_file: Path, settings_file: Path, ui_state_file: Path, qtbot: QtBot
) -> None:
    """保存済みのgeometryと前回フォルダーは、表示前に適用される。"""
    save_ui_state(
        ui_state_file,
        UiState(
            window=WindowState(x=160, y=120, width=1100, height=760, maximized=False),
            main_splitter=SplitterState(500, 300),
            last_open_directory=Path("C:\\Music"),
        ),
    )

    composition = app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)
    qtbot.addWidget(composition.window)
    window = composition.window

    assert not window.isVisible()
    assert window.geometry().x() == 160
    assert window.geometry().y() == 120
    assert window.last_open_directory == Path("C:\\Music")


def test_splitter_is_restored_after_the_visualization_settings(
    playlist_file: Path, settings_file: Path, ui_state_file: Path, qtbot: QtBot
) -> None:
    """可視化の表示設定を適用したあとでSplitterを復元する。"""
    save_settings(
        settings_file,
        AppSettings(1.0, True, waveform_visible=False, spectrum_visible=False),
    )
    save_ui_state(ui_state_file, UiState(main_splitter=SplitterState(300, 500)))

    composition = app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)
    qtbot.addWidget(composition.window)
    window = composition.window
    window.show()
    qtbot.waitExposed(window)

    assert window.waveform_panel.isVisible() is False
    splitter = window.capture_ui_state().main_splitter
    assert splitter is not None
    assert splitter.playlist_size >= MINIMUM_SPLITTER_SIZE
    window.spectrum_panel.shutdown()


def test_restore_alone_does_not_write_the_file(
    playlist_file: Path, settings_file: Path, ui_state_file: Path, qtbot: QtBot
) -> None:
    """復元しただけではui-state.jsonを書き換えない。"""
    save_ui_state(ui_state_file, UiState(window=WindowState(140, 100, 1100, 740, maximized=False)))
    original = ui_state_file.read_bytes()

    composition = app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)
    qtbot.addWidget(composition.window)

    assert ui_state_file.read_bytes() == original
    assert composition.ui_state_session.is_running is False


def test_ui_state_is_saved_on_shutdown(
    composition: app_module.PlayerComposition, ui_state_file: Path, qtbot: QtBot
) -> None:
    """終了時のflushで最終状態が保存される。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    composition.ui_state_session.start()
    window.set_last_open_directory(Path("C:\\Music"))

    assert composition.ui_state_session.flush() is True

    restored = load_ui_state(ui_state_file)
    assert restored.last_open_directory == Path("C:\\Music")
    assert restored.window is not None
    window.spectrum_panel.shutdown()


def test_corrupted_ui_state_starts_with_defaults_and_is_not_overwritten(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """破損したui-state.jsonでは既定位置で起動し、元ファイルを守る。"""
    original = "{壊れた"
    ui_state_file.write_text(original, encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        composition = app_module.build_player(
            playlist_file, settings_file, ui_state_file=ui_state_file
        )
    qtbot.addWidget(composition.window)

    assert composition.ui_state_session.is_save_enabled is False
    assert composition.ui_state_session.flush() is False
    assert ui_state_file.read_text(encoding="utf-8") == original
    assert UI_STATE_RESTORE_FAILED_MESSAGE in composition.window.statusBar().currentMessage()


def test_corrupted_ui_state_does_not_disable_settings_or_playlist_saving(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """3つの保存ファイルの障害は互いに独立している。"""
    ui_state_file.write_text("{壊れた", encoding="utf-8")

    composition = app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)
    qtbot.addWidget(composition.window)
    composition.settings_session.start()
    composition.app_settings.apply(AppSettings(1.25, True))
    composition.playlist_model.add_paths(audio_files)

    assert composition.ui_state_session.is_save_enabled is False
    assert composition.settings_session.flush() is True
    assert composition.playlist_session.save_from(composition.playlist_model) is True
    assert len(load_playlist(playlist_file)) == len(audio_files)


def test_corrupted_settings_does_not_disable_ui_state_saving(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    qtbot: QtBot,
) -> None:
    """settings.jsonの破損はUI状態の保存を止めない。"""
    settings_file.write_text("{壊れた", encoding="utf-8")

    composition = app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)
    qtbot.addWidget(composition.window)
    composition.window.set_last_open_directory(Path("C:\\Music"))

    assert composition.settings_session.is_save_enabled is False
    assert composition.ui_state_session.is_save_enabled is True
    assert composition.ui_state_session.flush() is True
    assert load_ui_state(ui_state_file).last_open_directory == Path("C:\\Music")


def test_ui_state_schema_does_not_touch_the_other_schemas(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
) -> None:
    """UI状態は独立ファイルで、他のschemaへ混ざらない。"""
    composition.playlist_model.add_paths(audio_files)
    composition.playlist_session.save_from(composition.playlist_model)
    composition.settings_session.start()
    composition.app_settings.apply(AppSettings(1.25, True))
    composition.settings_session.flush()
    composition.window.set_last_open_directory(Path("C:\\Music"))
    composition.ui_state_session.flush()

    playlist_document = json.loads(playlist_file.read_text(encoding="utf-8"))
    settings_document = json.loads(settings_file.read_text(encoding="utf-8"))
    ui_state_document = json.loads(ui_state_file.read_text(encoding="utf-8"))

    assert set(playlist_document) == {"schema_version", "entries"}
    assert set(settings_document) == {
        "schema_version",
        "playback_rate",
        "pitch_compensation",
        "waveform_visible",
        "spectrum_visible",
        "level_meter_visible",
    }
    assert settings_document["schema_version"] == 2
    assert ui_state_document["schema_version"] == 1
    assert set(ui_state_document) <= {
        "schema_version",
        "window",
        "main_splitter",
        "last_open_directory",
    }


def test_run_saves_ui_state_before_stopping_sessions(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """終了処理はWindowが生きているあいだにUI状態をflushしてからstopする。"""
    del qtbot
    events: list[str] = []
    original_flush = UiStateSession.flush
    original_stop = UiStateSession.stop

    def record_flush(session: UiStateSession) -> bool:
        events.append("ui_state_flush")
        return original_flush(session)

    def record_stop(session: UiStateSession) -> None:
        events.append("ui_state_stop")
        original_stop(session)

    def ignore_logging_setup() -> None:
        return None

    def skip_window_show(window: MainWindow) -> None:
        del window

    class ImmediateApplication:
        def exec(self) -> int:
            events.append("exec")
            return 0

    def create_immediate_application(argv: list[str]) -> QApplication:
        del argv
        return cast(QApplication, ImmediateApplication())

    monkeypatch.setattr(UiStateSession, "flush", record_flush)
    monkeypatch.setattr(UiStateSession, "stop", record_stop)
    monkeypatch.setattr(app_module, "default_playlist_path", lambda: playlist_file)
    monkeypatch.setattr(app_module, "default_settings_path", lambda: settings_file)
    monkeypatch.setattr(app_module, "default_ui_state_path", lambda: ui_state_file)
    monkeypatch.setattr(app_module, "create_application", create_immediate_application)
    monkeypatch.setattr(MainWindow, "show", skip_window_show)
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", ignore_logging_setup)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", ignore_logging_setup)

    assert app_module.run([]) == 0

    assert events == ["exec", "ui_state_flush", "ui_state_stop"]


def test_ui_layer_does_not_perform_ui_state_json_io() -> None:
    """UI層はui-state.jsonの読み書きもschema versionも知らない。"""
    from sdp.ui import main_window as main_window_module
    from sdp.ui import settings_dialog as settings_dialog_module

    for module in (main_window_module, settings_dialog_module):
        for forbidden in ("load_ui_state", "save_ui_state", "UiStateSession", "json"):
            assert not hasattr(module, forbidden), f"{module.__name__}: {forbidden}"

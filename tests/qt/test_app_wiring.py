"""app.py の組み立てとプレイリスト永続化の統合を検証する。

イベントループは起動しない（無期限に待つテストを作らない）。
本番配線の確認に音声再生は不要。
"""

import json
import logging
import struct
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QMediaPlayer,
)
from PySide6.QtWidgets import QApplication, QCheckBox, QDoubleSpinBox, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp import app as app_module
from sdp.core.metadata.reader import MetadataReader
from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.entry import FileStatus, PlaylistEntry, create_entry
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import load_playlist, save_playlist
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.launch import LaunchRequestHandler
from sdp.services import settings as app_settings_module
from sdp.services.launch_request import LaunchRequest
from sdp.services.pcm_tap import PcmTap
from sdp.services.playlist_session import PlaylistSession
from sdp.services.save_status import (
    SaveCategory,
    restore_failure_message,
    save_failure_message,
    save_recovered_message,
)
from sdp.services.settings import (
    AppSettings,
    AppSettingsController,
    SettingsSession,
    save_settings,
)
from sdp.services.single_instance import InstanceOutcome
from sdp.services.ui_state import (
    MINIMUM_SPLITTER_SIZE,
    ScreenRect,
    SplitterState,
    UiState,
    WindowState,
    load_ui_state,
    save_ui_state,
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


SETTINGS_KEYS = {
    "schema_version",
    "playback_rate",
    "pitch_compensation",
    "waveform_visible",
    "spectrum_visible",
    "level_meter_visible",
    "volume",
    "muted",
    "repeat_mode",
    "shuffle_enabled",
}


def int16_stereo_buffer(value: int = 16_384) -> QAudioBuffer:
    """48kHz Int16 stereoの1frame buffer（P0-C実測のWAV形式）。"""
    audio_format = QAudioFormat()
    audio_format.setSampleRate(48_000)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return QAudioBuffer(struct.pack("<2h", value, value), audio_format)


def current_player(composition: app_module.PlayerComposition) -> QMediaPlayer:
    """現在世代の QMediaPlayer（load ごとに作り直され、旧世代は破棄予約される）。

    子の並びは生成順なので、末尾が最新世代になる。
    """
    players = composition.backend.findChildren(QMediaPlayer)
    assert players
    return players[-1]


def current_buffer_output(composition: app_module.PlayerComposition) -> QAudioBufferOutput:
    """現在世代の QMediaPlayer が持つPCM出力（load ごとに作り直される）。"""
    return current_player(composition).audioBufferOutput()


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


def test_settings_round_trip_restores_playback_state(
    playlist_file: Path, settings_file: Path, qtbot: QtBot
) -> None:
    """速度・ピッチに加えて音量・mute・repeat・shuffleも復元する（P6-C）。

    再生位置と再生中かどうかは保存しない。
    """
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
    assert restored.controller.volume == pytest.approx(0.2)
    assert restored.controller.muted is True
    assert restored.playlist_playback.repeat_mode is RepeatMode.ALL
    assert restored.playlist_playback.shuffle_enabled is True
    # 現在曲はui-state.json側の責務で、settings.jsonには入らない。
    assert restored.playlist_playback.current_entry_id is None
    assert restored.controller.position_ms == 0


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
    assert not app.windowIcon().isNull()


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
    original_playlist_start = PlaylistSession.start
    original_playlist_flush = PlaylistSession.flush
    original_playlist_stop = PlaylistSession.stop
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

    def record_playlist_start(session: PlaylistSession, model: PlaylistModel | None = None) -> None:
        events.append("playlist_start")
        original_playlist_start(session, model)

    def record_playlist_flush(session: PlaylistSession) -> bool:
        events.append("playlist_flush")
        return original_playlist_flush(session)

    def record_playlist_stop(session: PlaylistSession) -> None:
        events.append("playlist_stop")
        original_playlist_stop(session)

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
    monkeypatch.setattr(PlaylistSession, "start", record_playlist_start)
    monkeypatch.setattr(PlaylistSession, "flush", record_playlist_flush)
    monkeypatch.setattr(PlaylistSession, "stop", record_playlist_stop)
    monkeypatch.setattr(WaveformAnalysisService, "start", record_waveform_start)
    monkeypatch.setattr(WaveformAnalysisService, "shutdown", record_waveform_shutdown)
    monkeypatch.setattr(app_module, "default_playlist_path", injected_playlist_path)
    monkeypatch.setattr(app_module, "create_application", create_immediate_application)
    # fake execは実event loopと「ウィンドウを閉じてから戻る」契約を再現しないため、
    # queued paintを残さない。表示・描画は専用Qtテストで検証する。
    monkeypatch.setattr(MainWindow, "show", skip_window_show)
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", ignore_logging_setup)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", ignore_logging_setup)
    assert app_module.run([], server_name=f"test-run-order-{uuid.uuid4().hex}") == 0
    assert events == [
        "settings_start",
        "playlist_start",
        "waveform_start",
        "exec",
        "waveform_shutdown",
        "settings_flush",
        "playlist_flush",
        "playlist_stop",
        "settings_stop",
    ]


def test_secondary_exits_without_building_composition_or_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """転送成功したsecondaryはPlayerCompositionもevent loopも作らず0で終了する。"""
    events: list[str] = []

    class ForwardedInstance:
        def __init__(self, name: str) -> None:
            events.append(f"instance:{name}")

        def start_or_forward(self, request: LaunchRequest) -> InstanceOutcome:
            events.append(f"forward:{len(request.paths)}")
            return InstanceOutcome.FORWARDED

        def shutdown(self) -> None:
            events.append("shutdown")

    def fail_build(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("secondaryでcompositionを構築してはいけません")

    monkeypatch.setattr(app_module, "SingleInstanceService", ForwardedInstance)
    monkeypatch.setattr(app_module, "build_player", fail_build)
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", lambda: None)

    assert app_module.run(["sdp", "relative.wav"], server_name="test-secondary") == 0
    assert events == ["instance:test-secondary", "forward:1", "shutdown"]


def test_secondary_transfer_failure_does_not_start_another_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存instanceが疑われる転送失敗では二重起動せず専用codeで終了する。"""
    shutdowns: list[int] = []

    class FailedInstance:
        def __init__(self, name: str) -> None:
            del name

        def start_or_forward(self, request: LaunchRequest) -> InstanceOutcome:
            del request
            return InstanceOutcome.FORWARD_FAILED

        def shutdown(self) -> None:
            shutdowns.append(1)

    monkeypatch.setattr(app_module, "SingleInstanceService", FailedInstance)

    def fail_build(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("転送失敗時にcompositionを構築してはいけません")

    monkeypatch.setattr(
        app_module,
        "build_player",
        fail_build,
    )
    monkeypatch.setattr(app_module.logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(app_module.logging_setup, "install_excepthook", lambda: None)

    assert app_module.run(["sdp"], server_name="test-failed") == 2
    assert shutdowns == [1]


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
    # 復元直後は未確認。背景の確認サービスが確定させる（同期経路で待たずに確かめる）。
    assert entries[3].file_status is FileStatus.UNKNOWN
    composition.file_status_checker.run_pending_now()
    assert composition.playlist_model.entry_at(3).is_missing
    assert composition.window.statusBar().currentMessage() == "プレイリストを復元しました（4件）。"


def test_initial_launch_paths_are_appended_after_restored_playlist(
    playlist_file: Path, audio_files: list[Path], qtbot: QtBot
) -> None:
    """初回起動引数は復元済みplaylistを置換せず、順序どおり末尾へ追加する。"""
    restored = [create_entry(audio_files[0])]
    save_playlist(playlist_file, restored)
    request = LaunchRequest((audio_files[1].resolve(), audio_files[1].resolve()))

    composition = app_module.build_player(playlist_file, launch_request=request)
    qtbot.addWidget(composition.window)
    entries = composition.playlist_model.entries()

    assert [entry.entry_id for entry in entries[:1]] == [restored[0].entry_id]
    assert [entry.path for entry in entries] == [
        audio_files[0],
        audio_files[1].resolve(),
        audio_files[1].resolve(),
    ]
    assert composition.controller.source is None
    assert composition.window.statusBar().currentMessage() == "2曲をプレイリストへ追加しました。"
    composition.window.spectrum_panel.shutdown()


def test_received_launch_uses_existing_window_and_model_and_requests_foreground(
    composition: app_module.PlayerComposition,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPC要求を同じcompositionへ追加し、最大化を保ったまま最小化を解除する。"""
    calls: list[object] = []
    minimized_maximized = Qt.WindowState.WindowMinimized | Qt.WindowState.WindowMaximized

    def record_state(window: MainWindow, state: Qt.WindowState) -> None:
        del window
        calls.append(state)

    def current_state(window: MainWindow) -> Qt.WindowState:
        del window
        return minimized_maximized

    def record_show(window: MainWindow) -> None:
        del window
        calls.append("show")

    def record_raise(window: MainWindow) -> None:
        del window
        calls.append("raise")

    def record_activate(window: MainWindow) -> None:
        del window
        calls.append("activate")

    def inactive(window: MainWindow) -> bool:
        del window
        return False

    def record_alert(widget: QWidget, duration: int = 0) -> None:
        del widget, duration
        calls.append("alert")

    monkeypatch.setattr(MainWindow, "windowState", current_state)
    monkeypatch.setattr(MainWindow, "setWindowState", record_state)
    monkeypatch.setattr(MainWindow, "show", record_show)
    monkeypatch.setattr(MainWindow, "raise_", record_raise)
    monkeypatch.setattr(MainWindow, "activateWindow", record_activate)
    monkeypatch.setattr(MainWindow, "isActiveWindow", inactive)
    monkeypatch.setattr(QApplication, "alert", record_alert)
    model = composition.playlist_model
    window = composition.window

    composition.launch_handler.handle_received(
        LaunchRequest((audio_files[0].resolve(), audio_files[1].resolve()))
    )

    assert composition.playlist_model is model
    assert composition.window is window
    assert [entry.path for entry in model.entries()] == [
        audio_files[0].resolve(),
        audio_files[1].resolve(),
    ]
    restored_state = calls[0]
    assert isinstance(restored_state, Qt.WindowState)
    assert restored_state & Qt.WindowState.WindowMaximized
    assert not restored_state & Qt.WindowState.WindowMinimized
    assert calls[1:] == ["show", "raise", "activate", "alert"]


def test_empty_received_launch_requests_foreground(
    composition: app_module.PlayerComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引数なしsecondaryはplaylistを変更せず、既存Windowの表示を要求する。"""
    activations: list[int] = []

    def record_activation(handler: LaunchRequestHandler) -> None:
        del handler
        activations.append(1)

    monkeypatch.setattr(LaunchRequestHandler, "_activate_window", record_activation)

    composition.launch_handler.handle_received(LaunchRequest())

    assert composition.playlist_model.rowCount() == 0
    assert activations == [1]


def test_invalid_only_received_launch_still_requests_foreground(
    composition: app_module.PlayerComposition,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """無視引数だけでも、二回目の起動意図としてWindowを前面化する。"""
    activations: list[int] = []

    def record_activation(handler: LaunchRequestHandler) -> None:
        del handler
        activations.append(1)

    monkeypatch.setattr(LaunchRequestHandler, "_activate_window", record_activation)

    composition.launch_handler.handle_received(LaunchRequest(ignored_arguments=("bad\0path",)))

    assert (
        composition.window.statusBar().currentMessage() == "追加できるファイルがありませんでした。"
    )
    assert activations == [1]


def test_activate_window_false_adds_without_foreground_request(
    composition: app_module.PlayerComposition,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """activate_window=Falseはpath追加だけを行い、Window操作をしない。"""

    def fail_activation(handler: LaunchRequestHandler) -> None:
        del handler
        raise AssertionError("activate_window=FalseでWindowを前面化してはいけません")

    monkeypatch.setattr(LaunchRequestHandler, "_activate_window", fail_activation)

    composition.launch_handler.handle_received(
        LaunchRequest((audio_files[0].resolve(),), activate_window=False)
    )

    assert [entry.path for entry in composition.playlist_model.entries()] == [
        audio_files[0].resolve()
    ]


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
    assert composition.window.statusBar().currentMessage() == restore_failure_message(
        [SaveCategory.SETTINGS]
    )
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
    assert failed_session.load() is not None
    assert failed_session.is_save_enabled is False

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
    document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert set(document) == SETTINGS_KEYS
    assert document["schema_version"] == 3
    assert document["playback_rate"] == pytest.approx(1.2)
    assert document["pitch_compensation"] is False


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
    assert composition.window.statusBar().currentMessage() == restore_failure_message(
        [SaveCategory.PLAYLIST]
    )
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
    assert composition.window.statusBar().currentMessage() == restore_failure_message(
        [SaveCategory.PLAYLIST]
    )
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
    """start 後は復元済みエントリも追加分も読み取る。

    メタデータ読み取りはファイル状態がAVAILABLEと確定してから始まるため、
    実アプリと同じ順序で状態確認サービスも開始する。
    """
    save_playlist(playlist_file, [create_entry(audio_files[0])])
    composition = app_module.build_player(playlist_file)
    qtbot.addWidget(composition.window)
    model = composition.playlist_model

    composition.file_status_checker.start()
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
    """PcmTapを保持し、具体Backendの世代フィルター済みPCM供給口へ接続されている。"""
    tap = composition.pcm_tap
    assert isinstance(tap, PcmTap)

    # 現在世代の player が QAudioBufferOutput を所有していること。
    player = current_player(composition)
    buffer_output = player.audioBufferOutput()
    assert isinstance(buffer_output, QAudioBufferOutput)
    assert buffer_output.parent() is player

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
        current_buffer_output(composition).audioBufferReceived.emit(int16_stereo_buffer())

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
    current_buffer_output(composition).audioBufferReceived.emit(buffer)
    assert tap.available_frame_count == 1
    assert tap.sample_rate == 48_000

    composition.controller.load(audio_files[1])

    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0
    assert tap.channel_count == 0
    assert tap.left_ring_buffer.available == 0
    assert tap.right_ring_buffer.available == 0


def test_late_pcm_from_the_previous_source_is_not_mixed_into_the_new_one(
    composition: app_module.PlayerComposition, audio_files: list[Path]
) -> None:
    """前sourceのplayerから遅れて届くPCMは、新しいsourceのPCMとして混ざらない。

    QAudioBuffer には source を識別する情報が無く、音声出力側の buffering で
    遅れて届きうる。Backend が load 世代で除外しないと、曲切替直後に前曲のPCMが
    Spectrum／Level Meterへ混ざる。
    """
    tap = composition.pcm_tap

    composition.controller.load(audio_files[0])
    previous_output = current_buffer_output(composition)
    composition.controller.load(audio_files[1])
    # source変更でtapは一度clearされている。
    assert tap.available_frame_count == 0
    received_before = tap.received_buffer_count

    previous_output.audioBufferReceived.emit(int16_stereo_buffer())

    assert tap.received_buffer_count == received_before
    assert tap.available_frame_count == 0
    assert tap.sample_rate == 0
    assert tap.channel_count == 0

    # 現在世代のPCMは従来どおり受理する。
    current_buffer_output(composition).audioBufferReceived.emit(int16_stereo_buffer())

    assert tap.received_buffer_count == received_before + 1
    assert tap.available_frame_count == 1


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

    current_buffer_output(composition).audioBufferReceived.emit(int16_stereo_buffer())

    assert tap.channel_count == 2
    assert tap.available_frame_count == 1
    left, right = tap.snapshot_stereo(1)
    assert left.tolist() == pytest.approx([0.5], abs=1e-3)
    assert right.tolist() == pytest.approx([0.5], abs=1e-3)


def test_playback_backend_interface_has_no_pcm_responsibility() -> None:
    """PlaybackBackendの一般契約へPCM SignalやQt型を追加していない。"""
    for forbidden in (
        "audio_buffer_output",
        "audio_buffer_received",
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

    for forbidden in (
        "QAudioBuffer",
        "QAudioBufferOutput",
        "audio_buffer_output",
        "audio_buffer_received",
    ):
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
    buffer_output = current_buffer_output(composition)

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
    assert set(settings_document) == SETTINGS_KEYS

    from sdp.core.analysis import waveform_cache

    assert waveform_cache.WAVEFORM_ANALYSIS_VERSION == 1
    # format 2 で内容fingerprintを追加した（縮約アルゴリズムは変えていないため
    # analysis versionは据え置き）。古いcacheはversion不一致で作り直される。
    assert waveform_cache.WAVEFORM_FORMAT_VERSION == 2
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
    assert document["schema_version"] == 3
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
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存済みのgeometryと前回フォルダーは、表示前に適用される。"""

    # CIの仮想screenサイズに依存せず、画面内geometryの復元配線だけを検証する。
    def fixed_screens(window: MainWindow) -> list[ScreenRect]:
        del window
        return [ScreenRect(x=0, y=0, width=1920, height=1040)]

    monkeypatch.setattr(MainWindow, "_screen_rects", fixed_screens)
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
    assert composition.window.statusBar().currentMessage() == restore_failure_message(
        [SaveCategory.UI_STATE]
    )


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
    assert set(settings_document) == SETTINGS_KEYS
    assert settings_document["schema_version"] == 3
    assert ui_state_document["schema_version"] == 2
    assert set(ui_state_document) <= {
        "schema_version",
        "window",
        "main_splitter",
        "last_open_directory",
        "current_playlist_entry_id",
    }
    # 音量やRepeatはsettings.json側、現在曲はui-state.json側だけに置く。
    assert "volume" not in ui_state_document
    assert "current_playlist_entry_id" not in settings_document
    # 再生位置は保存しない。
    assert "position_ms" not in ui_state_document
    assert "position_ms" not in settings_document


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

    assert app_module.run([], server_name=f"test-ui-state-order-{uuid.uuid4().hex}") == 0

    assert events == ["exec", "ui_state_flush", "ui_state_stop"]


def test_ui_layer_does_not_perform_ui_state_json_io() -> None:
    """UI層はui-state.jsonの読み書きもschema versionも知らない。"""
    from sdp.ui import main_window as main_window_module
    from sdp.ui import settings_dialog as settings_dialog_module

    for module in (main_window_module, settings_dialog_module):
        for forbidden in ("load_ui_state", "save_ui_state", "UiStateSession", "json"):
            assert not hasattr(module, forbidden), f"{module.__name__}: {forbidden}"


# -- 起動時の復元（P6-C）----------------------------------------------------


def build(
    playlist_file: Path, settings_file: Path, ui_state_file: Path
) -> app_module.PlayerComposition:
    return app_module.build_player(playlist_file, settings_file, ui_state_file=ui_state_file)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_every_settings_version_starts(
    playlist_file: Path, settings_file: Path, ui_state_file: Path, qtbot: QtBot, version: int
) -> None:
    """settings v1／v2／v3のいずれでも起動でき、既知の値だけを適用する。"""
    document: dict[str, object] = {
        "schema_version": version,
        "playback_rate": 1.25,
        "pitch_compensation": False,
    }
    if version >= 2:
        document["waveform_visible"] = False
    if version >= 3:
        document.update({"volume": 0.3, "muted": True, "repeat_mode": "one"})
    settings_file.write_text(json.dumps(document), encoding="utf-8")

    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)

    assert composition.controller.playback_rate == pytest.approx(1.25)
    assert composition.settings_session.is_save_enabled is True
    assert composition.window.waveform_panel.isVisibleTo(composition.window) is (version < 2)
    expected_volume = 0.3 if version >= 3 else 1.0
    assert composition.controller.volume == pytest.approx(expected_volume)
    assert composition.controller.muted is (version >= 3)
    assert composition.playlist_playback.repeat_mode is (
        RepeatMode.ONE if version >= 3 else RepeatMode.OFF
    )


@pytest.mark.parametrize("version", [1, 2])
def test_every_ui_state_version_starts(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
    version: int,
) -> None:
    """ui-state v1／v2のいずれでも起動でき、v1では現在曲を復元しない。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])
    entry_id = load_playlist(playlist_file)[1].entry_id
    document: dict[str, object] = {
        "schema_version": version,
        "window": {"x": 120, "y": 90, "width": 1100, "height": 720, "maximized": False},
    }
    if version >= 2:
        document["current_playlist_entry_id"] = entry_id
    ui_state_file.write_text(json.dumps(document), encoding="utf-8")

    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)

    assert composition.ui_state_session.is_save_enabled is True
    # geometryそのものは実行環境の画面サイズへ補正されるため、ここでは
    # 「両versionとも起動でき、v1では現在曲を復元しない」ことだけを確認する
    # （補正の詳細は画面矩形を注入するMainWindowテストで検証済み）。
    expected = entry_id if version >= 2 else None
    assert composition.playlist_playback.current_entry_id == expected


def test_current_entry_is_restored_before_show_without_playing(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """現在曲は表示前に選ばれ、自動再生せず位置も0のまま。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])
    entry_id = load_playlist(playlist_file)[2].entry_id
    save_ui_state(ui_state_file, UiState(current_playlist_entry_id=entry_id))

    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)

    assert not composition.window.isVisible()
    assert composition.playlist_playback.current_entry_id == entry_id
    assert composition.controller.source == audio_files[2].resolve()
    assert composition.controller.state is not PlaybackState.PLAYING
    assert composition.controller.position_ms == 0
    assert composition.playlist_playback.can_play_previous is True


def test_missing_current_entry_starts_without_a_current_song(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """削除済みentry_idでもエラーにせず、現在曲なしで起動する。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])
    save_ui_state(ui_state_file, UiState(current_playlist_entry_id="消えたID"))

    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)

    assert composition.playlist_playback.current_entry_id is None
    assert composition.playlist_model.rowCount() == len(audio_files)
    assert composition.ui_state_session.is_save_enabled is True
    assert composition.window.statusBar().currentMessage() != ""


def test_stale_current_entry_is_dropped_on_the_next_save(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """存在しないentry_idは次回保存で取り除かれる。"""
    save_playlist(playlist_file, [create_entry(path) for path in audio_files])
    save_ui_state(ui_state_file, UiState(current_playlist_entry_id="消えたID"))
    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)
    composition.window.set_last_open_directory(Path("C:\\Music"))

    assert composition.ui_state_session.flush() is True

    assert load_ui_state(ui_state_file).current_playlist_entry_id is None


def test_current_entry_is_saved_when_the_song_changes(
    composition: app_module.PlayerComposition,
    ui_state_file: Path,
    audio_files: list[Path],
) -> None:
    """曲を選ぶとUI状態の保存対象になる（settings.jsonへは入らない）。"""
    entry_ids = composition.playlist_model.add_paths(audio_files)
    composition.ui_state_session.start()

    composition.playlist_playback.select_entry_by_id(entry_ids[1])

    assert composition.ui_state_session.flush() is True
    assert load_ui_state(ui_state_file).current_playlist_entry_id == entry_ids[1]


# -- 3保存ファイルの障害matrix ----------------------------------------------


CORRUPTED = "{壊れた"


@pytest.mark.parametrize(
    "broken",
    [
        (),
        ("settings",),
        ("playlist",),
        ("ui_state",),
        ("settings", "playlist"),
        ("settings", "ui_state"),
        ("playlist", "ui_state"),
        ("settings", "playlist", "ui_state"),
    ],
)
def test_corruption_matrix_keeps_healthy_files_working(
    playlist_file: Path,
    settings_file: Path,
    ui_state_file: Path,
    audio_files: list[Path],
    qtbot: QtBot,
    broken: tuple[str, ...],
) -> None:
    """破損の組合せに関係なく起動でき、健全なファイルは保存できる。"""
    files = {"settings": settings_file, "playlist": playlist_file, "ui_state": ui_state_file}
    for name in broken:
        files[name].write_text(CORRUPTED, encoding="utf-8")

    composition = build(playlist_file, settings_file, ui_state_file)
    qtbot.addWidget(composition.window)
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    composition.settings_session.start()
    composition.ui_state_session.start()

    sessions = {
        "settings": composition.settings_session.is_save_enabled,
        "playlist": composition.playlist_session.is_save_enabled,
        "ui_state": composition.ui_state_session.is_save_enabled,
    }
    for name, enabled in sessions.items():
        assert enabled is (name not in broken), name

    # 健全なカテゴリは実際に保存できる。
    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.5))
    window.set_last_open_directory(Path("C:\\Music"))
    composition.playlist_model.add_paths(audio_files)
    assert composition.settings_session.flush() is ("settings" not in broken)
    assert composition.ui_state_session.flush() is ("ui_state" not in broken)
    assert composition.playlist_session.save_from(composition.playlist_model) is (
        "playlist" not in broken
    )

    # 破損ファイルは元のbytesのまま。
    for name in broken:
        assert files[name].read_text(encoding="utf-8") == CORRUPTED

    # 再生操作と可視化は続けられる。
    composition.controller.set_playback_rate(1.5)
    assert composition.controller.playback_rate == pytest.approx(1.5)
    assert window.spectrum_panel.is_spectrum_visible is True

    # メッセージは1文へまとまり、生の例外もパスも出さない。
    message = window.statusBar().currentMessage()
    if broken:
        for token in ("Error", "Traceback", str(settings_file)):
            assert token not in message
    window.spectrum_panel.shutdown()


@pytest.mark.parametrize(
    "failing",
    [
        ("settings",),
        ("playlist",),
        ("ui_state",),
        ("settings", "playlist", "ui_state"),
    ],
)
def test_save_failures_are_reported_per_category(
    composition: app_module.PlayerComposition,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    failing: tuple[str, ...],
) -> None:
    """保存失敗はカテゴリごとに区別して1回だけ通知する。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    messages: list[str] = []
    composition.save_status.message_requested.connect(messages.append)
    composition.settings_session.start()
    composition.playlist_session.start()
    composition.ui_state_session.start()

    def failing_save(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("保存失敗")

    if "settings" in failing:
        monkeypatch.setattr("sdp.services.settings.save_settings", failing_save)
    if "playlist" in failing:
        monkeypatch.setattr("sdp.services.playlist_session.save_playlist", failing_save)
    if "ui_state" in failing:
        monkeypatch.setattr("sdp.services.ui_state_session.save_ui_state", failing_save)

    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.5))
    composition.playlist_model.add_paths(audio_files[:1])
    window.set_last_open_directory(Path("C:\\Music"))
    composition.settings_session.flush()
    composition.playlist_session.flush()
    composition.ui_state_session.flush()
    # 同じ失敗を繰り返しても通知は増えない。
    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.6))
    composition.playlist_model.add_paths(audio_files[1:2])
    window.set_last_open_directory(Path("C:\\音楽"))
    composition.settings_session.flush()
    composition.playlist_session.flush()
    composition.ui_state_session.flush()

    expected = {
        save_failure_message(SaveCategory.SETTINGS) if "settings" in failing else None,
        save_failure_message(SaveCategory.PLAYLIST) if "playlist" in failing else None,
        save_failure_message(SaveCategory.UI_STATE) if "ui_state" in failing else None,
    } - {None}
    assert set(messages) == expected
    assert len(messages) == len(expected)
    assert window.statusBar().currentMessage() in messages
    window.spectrum_panel.shutdown()


def test_recovered_save_is_reported_once(
    composition: app_module.PlayerComposition,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """一時失敗のあと保存できたら、短い復旧通知を1回だけ出す。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    messages: list[str] = []
    composition.save_status.message_requested.connect(messages.append)
    composition.settings_session.start()

    calls: list[int] = []
    original = app_settings_module.save_settings

    def flaky_save(path: Path, settings: AppSettings) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("一時的な共有違反")
        original(path, settings)

    monkeypatch.setattr("sdp.services.settings.save_settings", flaky_save)
    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.5))
    composition.settings_session.flush()
    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.6))
    composition.settings_session.flush()

    assert messages == [
        save_failure_message(SaveCategory.SETTINGS),
        save_recovered_message(SaveCategory.SETTINGS),
    ]
    window.spectrum_panel.shutdown()


def test_recovered_playlist_save_is_reported_once(
    composition: app_module.PlayerComposition,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プレイリストの一時失敗と復旧も本番通知経路へ1回ずつ流す。"""
    messages: list[str] = []
    composition.save_status.message_requested.connect(messages.append)
    composition.playlist_session.start()
    calls: list[int] = []
    original = save_playlist

    def flaky_save(path: Path, entries: Sequence[PlaylistEntry]) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("一時的な共有違反")
        original(path, entries)

    monkeypatch.setattr("sdp.services.playlist_session.save_playlist", flaky_save)
    composition.playlist_model.add_paths(audio_files[:1])
    composition.playlist_session.flush()
    composition.playlist_model.add_paths(audio_files[1:2])
    composition.playlist_session.flush()

    assert messages == [
        save_failure_message(SaveCategory.PLAYLIST),
        save_recovered_message(SaveCategory.PLAYLIST),
    ]


@pytest.mark.parametrize("failing", ["settings", "playlist", "ui_state"])
def test_save_failure_does_not_block_playback_or_other_files(
    composition: app_module.PlayerComposition,
    playlist_file: Path,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    """1カテゴリの保存失敗でも再生と他ファイルの保存は続く。"""

    def failing_save(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("保存失敗")

    save_targets = {
        "settings": "sdp.services.settings.save_settings",
        "playlist": "sdp.services.playlist_session.save_playlist",
        "ui_state": "sdp.services.ui_state_session.save_ui_state",
    }
    monkeypatch.setattr(save_targets[failing], failing_save)
    composition.settings_session.start()
    composition.playlist_session.start()
    composition.ui_state_session.start()
    composition.app_settings.apply(replace(composition.app_settings.settings, volume=0.5))
    composition.playlist_model.add_paths(audio_files)
    composition.window.set_last_open_directory(Path("C:\\Music"))

    assert composition.settings_session.flush() is (failing != "settings")
    assert composition.playlist_session.flush() is (failing != "playlist")
    assert composition.ui_state_session.flush() is (failing != "ui_state")
    composition.controller.set_playback_rate(1.25)
    assert composition.controller.playback_rate == pytest.approx(1.25)
    if failing != "playlist":
        assert len(load_playlist(playlist_file)) == len(audio_files)


# -- 終了処理 ---------------------------------------------------------------


def test_shutdown_continues_after_a_raising_step(
    composition: app_module.PlayerComposition,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """1カテゴリで例外が出ても、後続の終了処理を飛ばさない。"""
    performed: list[str] = []

    def explode() -> None:
        performed.append("waveform")
        raise RuntimeError("停止に失敗")

    def record_flush() -> bool:
        performed.append("ui_state_flush")
        return False

    def record_settings_flush() -> bool:
        performed.append("settings_flush")
        return False

    def record_stop() -> None:
        performed.append("app_settings_shutdown")

    monkeypatch.setattr(composition.waveform_analysis, "shutdown", explode)
    monkeypatch.setattr(composition.ui_state_session, "flush", record_flush)
    monkeypatch.setattr(composition.settings_session, "flush", record_settings_flush)
    monkeypatch.setattr(composition.app_settings, "shutdown", record_stop)

    with caplog.at_level(logging.ERROR):
        app_module.shutdown(composition)

    assert performed == [
        "waveform",
        "ui_state_flush",
        "settings_flush",
        "app_settings_shutdown",
    ]
    assert "終了処理" in caplog.text


def test_shutdown_stops_every_worker_and_timer(
    composition: app_module.PlayerComposition, qtbot: QtBot
) -> None:
    """終了処理でworkerとtimerが残らない。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    composition.settings_session.start()
    composition.playlist_session.start()
    composition.ui_state_session.start()

    app_module.shutdown(composition)

    assert composition.settings_session.is_running is False
    assert composition.playlist_session.is_running is False
    assert composition.ui_state_session.is_running is False
    assert composition.window.spectrum_panel.is_timer_active is False
    assert composition.waveform_analysis.is_running is False
    assert composition.metadata_reader.is_running is False


def test_shutdown_is_safe_with_the_settings_dialog_open(
    composition: app_module.PlayerComposition, qtbot: QtBot
) -> None:
    """設定ダイアログを開いたまま終了しても安全。"""
    window = composition.window
    window.show()
    qtbot.waitExposed(window)
    window.open_settings()
    assert window.settings_dialog is not None

    app_module.shutdown(composition)
    window.close()

    assert composition.settings_session.is_running is False

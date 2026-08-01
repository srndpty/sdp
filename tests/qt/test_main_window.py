"""MainWindow の責務を FakeBackend + PlaybackController で検証する。

ネイティブのファイルダイアログは開かず、`QFileDialog.getOpenFileName` を差し替える。
"""

import inspect
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLabel,
    QSplitter,
    QTableView,
    QWidget,
)
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
)
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.services.pcm_tap import PcmTap
from sdp.services.settings import AppSettings, AppSettingsController
from sdp.services.ui_state import (
    MINIMUM_SPLITTER_SIZE,
    ScreenRect,
    SplitterState,
    UiState,
    WindowState,
    distribute_splitter_sizes,
)
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui import main_window as main_window_module
from sdp.ui.legal_dialog import LegalDocumentDialog
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView
from sdp.ui.settings_dialog import SettingsDialog
from sdp.ui.speed_panel import SpeedPanel


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def playlist_model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def playlist_playback(
    controller: PlaybackController, playlist_model: PlaylistModel
) -> Iterator[PlaylistPlaybackController]:
    yield PlaylistPlaybackController(controller, playlist_model)


@pytest.fixture
def window(
    controller: PlaybackController,
    playlist_model: PlaylistModel,
    playlist_playback: PlaylistPlaybackController,
    qtbot: QtBot,
    tmp_path: Path,
) -> Iterator[MainWindow]:
    waveform_analysis = WaveformAnalysisService(controller, tmp_path / "waveform-cache")
    pcm_tap = PcmTap(controller)
    main = MainWindow(
        controller,
        playlist_model,
        playlist_playback,
        waveform_analysis,
        pcm_tap,
        AppSettingsController(controller),
    )
    qtbot.addWidget(main)
    yield main
    main.spectrum_panel.shutdown()
    pcm_tap.shutdown()
    waveform_analysis.shutdown()


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "テスト 音源.wav"
    path.write_bytes(b"RIFF----WAVEfmt ")
    return path


def file_name_text(window: MainWindow) -> str:
    label = window.findChild(QLabel, "fileNameLabel")
    assert label is not None
    return label.text()


def stub_open_dialog(selected: str) -> Callable[..., tuple[str, str]]:
    """`QFileDialog.getOpenFileName` の差し替え。空文字はキャンセルを表す。"""

    def _dialog(*args: object, **kwargs: object) -> tuple[str, str]:
        del args, kwargs
        return (selected, "")

    return _dialog


def action_of(window: MainWindow, name: str) -> QAction:
    action = window.findChild(QAction, name)
    assert action is not None, name
    return action


@pytest.mark.parametrize(
    ("object_name", "accessible_name"),
    [
        ("fileNameLabel", "現在のファイル"),
        ("playButton", "再生"),
        ("pauseButton", "一時停止"),
        ("stopButton", "停止"),
        ("seekSlider", "再生位置"),
        ("volumeSlider", "音量"),
        ("muteButton", "ミュート"),
        ("repeatModeButton", "リピート"),
        ("shuffleButton", "シャッフル"),
        ("playlistTable", "プレイリスト"),
    ],
)
def test_core_controls_have_stable_accessible_names(
    window: MainWindow, object_name: str, accessible_name: str
) -> None:
    """配布版UI Automationが座標や翻訳済み表示順へ依存せず操作できる。"""
    widget = window.findChild(QWidget, object_name)
    assert widget is not None, object_name
    assert widget.accessibleName() == accessible_name


# -- 依存の向き -------------------------------------------------------------


def test_main_window_takes_only_its_composed_dependencies() -> None:
    """Controller・Model・プレイリスト再生・波形解析・PCMタップ・設定調停（と親）だけ。

    具体Backend（QtMultimediaBackend）とSettingsSession（JSON保存）は渡さない。
    """
    parameters = list(inspect.signature(MainWindow.__init__).parameters)
    assert parameters == [
        "self",
        "controller",
        "playlist_model",
        "playlist_playback",
        "waveform_analysis",
        "pcm_tap",
        "app_settings",
        "parent",
    ]


def test_main_window_module_does_not_import_the_qt_backend() -> None:
    """MainWindow のモジュールが具体的な Backend も永続化も参照していない。"""
    for forbidden in (
        "QtMultimediaBackend",
        "QMediaPlayer",
        "save_playlist",
        "load_playlist",
        "PlaylistSession",
    ):
        assert not hasattr(main_window_module, forbidden), forbidden


def test_main_window_delegates_to_child_widgets(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """再生は PlayerControls、プレイリスト操作は PlaylistView へ委譲する。"""
    controls = window.findChild(PlayerControls)
    speed_panels = window.findChildren(SpeedPanel)
    playlist_views = window.findChildren(PlaylistView)
    assert controls is not None
    assert len(speed_panels) == 1
    assert len(playlist_views) == 1
    assert playlist_views[0].findChild(QTableView, "playlistTable") is not None

    for forbidden in (
        "play",
        "pause",
        "stop",
        "seek",
        "set_volume",
        "set_playback_rate",
        "set_pitch_compensation",
        "add_files",
        "remove_selected",
        "clear_playlist",
    ):
        assert not hasattr(window, forbidden), forbidden


def test_speed_panel_keeps_controller_state_across_source_changes(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """直接loadでsourceが変わっても速度・ピッチ表示を維持する。"""
    spin_box = window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    assert spin_box is not None
    controller.set_playback_rate(1.5)
    controller.set_pitch_compensation(False)
    backend.calls.clear()

    controller.load(audio_file)

    assert spin_box.value() == 1.5
    assert controller.pitch_compensation is False
    assert backend.call_names() == ["load"]


def test_speed_panel_state_survives_playlist_switch_and_repeat_one(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    playlist_model: PlaylistModel,
    playlist_playback: PlaylistPlaybackController,
    tmp_path: Path,
    qtbot: QtBot,
) -> None:
    """プレイリスト曲切替とRepeat ONEのreload後も速度・pitchを維持する。"""
    sources = [tmp_path / "曲A.wav", tmp_path / "曲B.wav"]
    for source in sources:
        source.write_bytes(b"x")
    entry_ids = playlist_model.add_paths(sources)
    spin_box = window.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    assert spin_box is not None
    controller.set_playback_rate(1.25)
    controller.set_pitch_compensation(False)

    assert playlist_playback.play_entry(entry_ids[0]) is True
    assert playlist_playback.play_entry(entry_ids[1]) is True
    playlist_playback.set_repeat_mode(RepeatMode.ONE)
    backend.emit_position(100)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    qtbot.waitUntil(lambda: len(backend.call_args("load")) == 3)

    assert controller.playback_rate == 1.25
    assert controller.pitch_compensation is False
    assert spin_box.value() == 1.25
    assert backend.call_args("set_playback_rate") == [(1.25,)]
    assert backend.call_args("set_pitch_compensation") == [(False,)]
    assert backend.call_args("load") == [
        (sources[0].resolve(), 1),
        (sources[1].resolve(), 2),
        (sources[1].resolve(), 3),
    ]


def test_playlist_view_uses_the_given_model(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """配置された PlaylistView に同じ PlaylistModel が設定される。"""
    table = window.findChild(QTableView, "playlistTable")
    assert table is not None
    assert table.model() is playlist_model


def test_playlist_messages_reach_the_status_bar(
    window: MainWindow, playlist_model: PlaylistModel
) -> None:
    """PlaylistView のメッセージ要求がステータスバーへ表示される。"""
    del playlist_model
    view = window.findChild(PlaylistView)
    assert view is not None

    view.message_requested.emit("3曲を追加しました。")

    assert window.statusBar().currentMessage() == "3曲を追加しました。"


# -- ファイルを開く ---------------------------------------------------------


def test_cancelled_dialog_does_not_load(
    window: MainWindow, backend: FakePlaybackBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイル選択をキャンセルしたら何もしない。"""
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(""))

    window.open_file()

    assert backend.call_names() == []


def test_selected_file_is_loaded_as_path(
    window: MainWindow,
    backend: FakePlaybackBackend,
    audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選択したファイルが Path として Controller へ渡る。"""
    monkeypatch.setattr(
        main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(str(audio_file))
    )

    window.open_file()

    assert backend.call_args("load") == [(audio_file.resolve(), 1)]


def test_all_files_filter_is_available() -> None:
    """拡張子で再生可否を断定しないため「すべてのファイル」を選べる。"""
    assert "すべてのファイル (*)" in main_window_module.FILE_DIALOG_FILTER


def test_source_change_updates_file_name_and_title(
    window: MainWindow, controller: PlaybackController, audio_file: Path
) -> None:
    """source_changed でファイル名表示・ツールチップ・タイトルが更新される。"""
    controller.load(audio_file)

    assert file_name_text(window) == audio_file.name
    assert window.windowTitle() == f"sdp — {audio_file.name}"
    label = window.findChild(QLabel, "fileNameLabel")
    assert label is not None
    assert label.toolTip() == str(audio_file.resolve())


def test_cleared_source_restores_the_initial_title(
    window: MainWindow, controller: PlaybackController, audio_file: Path
) -> None:
    """source が無くなったらファイル名表示とタイトルを初期状態へ戻す。"""
    controller.load(audio_file)

    controller.source_changed.emit(None)

    assert file_name_text(window) == main_window_module.NO_FILE_TEXT
    assert window.windowTitle() == main_window_module.WINDOW_TITLE
    assert window.statusBar().currentMessage() == "音声ファイルを開いてください。"


# -- ステータス表示 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (MediaStatus.LOADING, "読み込み中..."),
        (MediaStatus.LOADED, "読み込み完了"),
        (MediaStatus.BUFFERED, "読み込み完了"),
        (MediaStatus.STALLED, "再生が一時的に停止しています"),
        (MediaStatus.BUFFERING, "バッファリング中..."),
        (MediaStatus.END_OF_MEDIA, "再生終了"),
        (MediaStatus.INVALID_MEDIA, "音声ファイルを読み込めませんでした"),
    ],
)
def test_media_status_updates_the_status_bar(
    window: MainWindow,
    backend: FakePlaybackBackend,
    status: MediaStatus,
    expected: str,
) -> None:
    """MediaStatus に応じてステータスバーが更新される。"""
    backend.emit_media_status(status)

    assert window.statusBar().currentMessage() == expected


def test_error_message_is_shown_without_technical_detail(
    window: MainWindow, backend: FakePlaybackBackend
) -> None:
    """エラーは message だけを表示し、detail を画面へ出さない。"""
    error = PlaybackError(
        code=PlaybackErrorCode.FORMAT_ERROR,
        message="この音声形式は再生できません。",
        detail="QMediaPlayer.Error.FormatError / errorString='unsupported codec'",
    )

    backend.emit_error(error)

    assert window.statusBar().currentMessage() == error.message
    assert error.detail not in window.statusBar().currentMessage()
    assert error.detail not in file_name_text(window)


@pytest.mark.parametrize("error_first", [True, False])
def test_specific_error_wins_over_invalid_media_status(
    window: MainWindow, backend: FakePlaybackBackend, error_first: bool
) -> None:
    """通知順にかかわらず、INVALID_MEDIAより具体的な再生エラーを表示する。"""
    error = PlaybackError(
        code=PlaybackErrorCode.ACCESS_DENIED,
        message="音声ファイルへのアクセスが拒否されました。",
        detail="QMediaPlayer.AccessDeniedError",
    )

    if error_first:
        backend.emit_error(error)
        backend.emit_media_status(MediaStatus.INVALID_MEDIA)
    else:
        backend.emit_media_status(MediaStatus.INVALID_MEDIA)
        backend.emit_error(error)

    assert window.statusBar().currentMessage() == error.message


def test_new_source_clears_specific_error_priority(
    window: MainWindow,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """source変更後のINVALID_MEDIAは新しいsourceの一般エラーとして表示する。"""
    backend.emit_error(
        PlaybackError(
            code=PlaybackErrorCode.FORMAT_ERROR,
            message="この音声形式は再生できません。",
            detail="old source",
        )
    )

    controller.load(audio_file)
    backend.emit_media_status(MediaStatus.INVALID_MEDIA)

    assert window.statusBar().currentMessage() == "音声ファイルを読み込めませんでした"


# -- メニュー ---------------------------------------------------------------


def test_quit_action_closes_the_window(window: MainWindow) -> None:
    """終了アクションでウィンドウが閉じる。"""
    window.show()
    assert window.isVisible()

    action_of(window, "quitAction").trigger()

    assert not window.isVisible()


def test_open_action_opens_the_file_dialog(
    window: MainWindow,
    backend: FakePlaybackBackend,
    audio_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「開く...」アクションからファイル選択が始まる。"""
    monkeypatch.setattr(
        main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(str(audio_file))
    )

    action_of(window, "openAction").trigger()

    assert backend.call_args("load") == [(audio_file.resolve(), 1)]


@pytest.mark.parametrize(
    ("action_name", "title", "expected"),
    [
        ("aboutAction", "sdpについて", "一切の保証がありません"),
        ("showLicenseAction", "ライセンス", "GNU GENERAL PUBLIC LICENSE"),
        ("showThirdPartyLicensesAction", "第三者ライセンス", "PySide6 / Qt / Shiboken"),
    ],
)
def test_help_actions_show_legal_notices(
    window: MainWindow, action_name: str, title: str, expected: str
) -> None:
    """Help menuからGPL告知・本文・第三者通知を読める。"""
    action_of(window, action_name).trigger()

    dialog = window.legal_dialog
    assert isinstance(dialog, LegalDocumentDialog)
    assert dialog.windowTitle() == title
    assert expected in dialog.text


def test_about_notice_points_to_license_and_corresponding_source(window: MainWindow) -> None:
    """適切な法的告知から配布条件と対応sourceの文書名を確認できる。"""
    action_of(window, "aboutAction").trigger()

    dialog = window.legal_dialog
    assert dialog is not None
    for expected in (
        "Copyright (C) 2026 sdp contributors",
        "GNU General Public License version 3",
        "LICENSE",
        "THIRD_PARTY_NOTICES.txt",
        "CORRESPONDING_SOURCE.md",
    ):
        assert expected in dialog.text


# -- 設定（P6-A）------------------------------------------------------------


def app_settings_of(window: MainWindow) -> AppSettingsController:
    settings_controller = window._app_settings  # pyright: ignore[reportPrivateUsage]
    return settings_controller


def test_settings_action_opens_the_dialog(window: MainWindow) -> None:
    """ツールメニューの設定アクションからダイアログが開く。"""
    assert window.settings_dialog is None

    action_of(window, "openSettingsAction").trigger()

    dialog = window.settings_dialog
    assert isinstance(dialog, SettingsDialog)
    assert dialog.isVisible()
    assert dialog.applied_settings == app_settings_of(window).settings


def test_settings_dialog_is_not_opened_twice(window: MainWindow) -> None:
    """既に開いている場合は新しく作らず前面へ出す。"""
    action_of(window, "openSettingsAction").trigger()
    first = window.settings_dialog

    action_of(window, "openSettingsAction").trigger()

    assert window.settings_dialog is first
    assert len(window.findChildren(SettingsDialog)) == 1


def test_reopening_an_open_dialog_keeps_unapplied_edits(window: MainWindow) -> None:
    """開いているダイアログの再前面化では編集中の全入力を戻さない。"""
    action = action_of(window, "openSettingsAction")
    action.trigger()
    dialog = window.settings_dialog
    assert dialog is not None
    rate = dialog.findChild(QDoubleSpinBox, "settingsPlaybackRateSpinBox")
    waveform = dialog.findChild(QCheckBox, "settingsWaveformVisibleCheckBox")
    spectrum = dialog.findChild(QCheckBox, "settingsSpectrumVisibleCheckBox")
    level = dialog.findChild(QCheckBox, "settingsLevelMeterVisibleCheckBox")
    pitch = dialog.findChild(QCheckBox, "settingsPitchCompensationCheckBox")
    assert all(value is not None for value in (rate, waveform, spectrum, level, pitch))
    assert rate is not None
    assert waveform is not None
    assert spectrum is not None
    assert level is not None
    assert pitch is not None
    rate.setValue(1.75)
    pitch.setChecked(False)
    waveform.setChecked(False)
    spectrum.setChecked(False)
    level.setChecked(False)
    edited = dialog.current_input()

    action.trigger()

    assert window.settings_dialog is dialog
    assert dialog.current_input() == edited


def test_settings_dialog_can_be_reopened_after_closing(window: MainWindow, qtbot: QtBot) -> None:
    """閉じたあとは再度開ける。"""
    action_of(window, "openSettingsAction").trigger()
    first = window.settings_dialog
    assert first is not None

    first.reject()
    qtbot.waitUntil(lambda: window.settings_dialog is None, timeout=2_000)
    action_of(window, "openSettingsAction").trigger()

    assert window.settings_dialog is not None
    assert window.settings_dialog is not first


def test_reopened_dialog_shows_the_current_settings(window: MainWindow, qtbot: QtBot) -> None:
    """開き直したダイアログには最新の適用済み設定が入る。"""
    app_settings_of(window).apply(AppSettings(1.5, False, spectrum_visible=False))

    action_of(window, "openSettingsAction").trigger()
    dialog = window.settings_dialog
    assert dialog is not None

    assert dialog.applied_settings.playback_rate == pytest.approx(1.5)
    assert dialog.applied_settings.spectrum_visible is False
    del qtbot


def test_dialog_request_is_applied_through_the_mediator(
    window: MainWindow, backend: FakePlaybackBackend
) -> None:
    """ダイアログの要求は調停サービス経由でControllerと可視化へ届く。"""
    action_of(window, "openSettingsAction").trigger()
    dialog = window.settings_dialog
    assert dialog is not None

    dialog.settings_requested.emit(AppSettings(1.25, False, waveform_visible=False))

    assert backend.call_args("set_playback_rate") == [(1.25,)]
    assert app_settings_of(window).settings.waveform_visible is False
    assert not window.waveform_panel.isVisible()


def test_failed_dialog_apply_keeps_the_dialog_open(
    window: MainWindow, backend: FakePlaybackBackend
) -> None:
    """Controller適用失敗ではOKでも閉じず、入力と適用済みsnapshotを区別する。"""
    action_of(window, "openSettingsAction").trigger()
    dialog = window.settings_dialog
    assert dialog is not None
    before = dialog.applied_settings
    rate = dialog.findChild(QDoubleSpinBox, "settingsPlaybackRateSpinBox")
    pitch = dialog.findChild(QCheckBox, "settingsPitchCompensationCheckBox")
    button_box = dialog.findChild(QDialogButtonBox, "settingsButtonBox")
    assert rate is not None
    assert pitch is not None
    assert button_box is not None
    ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button is not None
    rate.setValue(1.25)
    pitch.setChecked(False)
    backend.setter_errors["set_pitch_compensation"] = RuntimeError("故障注入")

    ok_button.click()

    assert dialog.isVisible()
    assert dialog.applied_settings == before
    assert dialog.current_input().playback_rate == pytest.approx(1.25)
    assert dialog.error_text == "設定を適用できませんでした。"


def test_main_window_does_not_touch_the_settings_file() -> None:
    """MainWindowはJSON・schema version・保存タイマーを持たない。"""
    for forbidden in (
        "json",
        "load_settings",
        "save_settings",
        "SettingsSession",
        "SETTINGS_SCHEMA_VERSION",
        "QTimer",
    ):
        assert not hasattr(main_window_module, forbidden), forbidden


# -- 可視化の表示ON/OFF -----------------------------------------------------


def test_visualization_settings_are_applied_before_show(
    controller: PlaybackController,
    playlist_model: PlaylistModel,
    playlist_playback: PlaylistPlaybackController,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    """復元済み設定は表示前に反映する（見えてから消えるフリッカーを避ける）。"""
    waveform_analysis = WaveformAnalysisService(controller, tmp_path / "waveform-cache")
    pcm_tap = PcmTap(controller)
    app_settings = AppSettingsController(controller)
    app_settings.apply(AppSettings(1.0, True, waveform_visible=False, spectrum_visible=False))

    main = MainWindow(
        controller,
        playlist_model,
        playlist_playback,
        waveform_analysis,
        pcm_tap,
        app_settings,
    )
    qtbot.addWidget(main)

    assert main.waveform_panel.isVisibleTo(main) is False
    assert main.spectrum_panel.spectrum_widget.isVisibleTo(main.spectrum_panel) is False
    assert main.spectrum_panel.level_meter_widget.isVisibleTo(main.spectrum_panel) is True
    main.spectrum_panel.shutdown()
    pcm_tap.shutdown()
    waveform_analysis.shutdown()


def test_toggling_settings_shows_and_hides_each_visualization(
    window: MainWindow, qtbot: QtBot
) -> None:
    """3つの可視化を個別にON/OFFできる。"""
    window.show()
    qtbot.waitExposed(window)
    app_settings = app_settings_of(window)
    panel = window.spectrum_panel

    app_settings.apply(
        AppSettings(
            1.0, True, waveform_visible=False, spectrum_visible=False, level_meter_visible=False
        )
    )

    assert not window.waveform_panel.isVisible()
    assert not panel.is_spectrum_visible
    assert not panel.is_level_meter_visible
    # プレイリストと再生操作は残る。
    assert window.findChild(PlaylistView) is not None
    assert window.findChild(PlayerControls) is not None

    app_settings.apply(AppSettings(1.0, True))

    assert window.waveform_panel.isVisible()
    assert panel.is_spectrum_visible
    assert panel.is_level_meter_visible


def test_hidden_waveform_panel_stops_following_the_position(
    window: MainWindow, controller: PlaybackController, audio_file: Path, qtbot: QtBot
) -> None:
    """非表示中は位置追従の描画更新を行わず、再表示で現在位置へ復帰する。"""
    window.show()
    qtbot.waitExposed(window)
    controller.load(audio_file)
    widget = window.waveform_panel.waveform_widget
    app_settings_of(window).apply(AppSettings(1.0, True, waveform_visible=False))
    before = widget.position_ms

    controller.seek(12_000)

    assert widget.position_ms == before

    app_settings_of(window).apply(AppSettings(1.0, True, waveform_visible=True))

    assert widget.position_ms == controller.position_ms


# -- UI状態（P6-B）----------------------------------------------------------


def screens() -> list[ScreenRect]:
    """テストから注入する固定の画面構成（実モニターに依存しない）。"""
    return [ScreenRect(x=0, y=0, width=1920, height=1040)]


def usable_size(window: MainWindow) -> tuple[int, int]:
    """minimumSizeHintを下回らないテスト用サイズ。

    MainWindowの最小サイズは可視化とコントロールの分だけ大きいため、
    固定値を書くとプラットフォームやフォント差で壊れる。
    """
    hint = window.minimumSizeHint()
    return (hint.width() + 60, hint.height() + 60)


def window_state_of(window: MainWindow) -> WindowState:
    state = window.capture_ui_state().window
    assert state is not None
    return state


def expected_player_ratio(window: MainWindow, saved: SplitterState, total: int) -> float:
    """保存比率を子Widgetのminimum sizeで補正した期待値。"""
    splitter = window.findChild(QSplitter, "mainSplitter")
    assert splitter is not None
    player = splitter.widget(0)
    playlist = splitter.widget(1)
    assert player is not None
    assert playlist is not None
    player_minimum = max(player.minimumHeight(), player.minimumSizeHint().height(), 0)
    playlist_minimum = max(playlist.minimumHeight(), playlist.minimumSizeHint().height(), 0)
    requested_player, _ = distribute_splitter_sizes(saved, total)
    fitted_player = max(player_minimum, min(total - playlist_minimum, requested_player))
    return fitted_player / total


def test_capture_returns_the_current_geometry(window: MainWindow, qtbot: QtBot) -> None:
    """captureは現在のnormal geometryとSplitterサイズを返す。"""
    width, height = usable_size(window)
    window.setGeometry(120, 80, width, height)
    window.show()
    qtbot.waitExposed(window)

    state = window.capture_ui_state()

    assert state.window is not None
    assert (state.window.x, state.window.y) == (120, 80)
    assert (state.window.width, state.window.height) == (width, height)
    assert state.window.maximized is False
    assert state.main_splitter is not None
    assert state.main_splitter.total > 0


def test_capture_keeps_the_normal_geometry_while_maximized(
    window: MainWindow, qtbot: QtBot
) -> None:
    """最大化中でも、最大化後の画面サイズをnormal sizeとして保存しない。"""
    width, height = usable_size(window)
    window.setGeometry(150, 90, width, height)
    window.show()
    qtbot.waitExposed(window)
    before = window_state_of(window)

    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.waitUntil(lambda: window.isMaximized(), timeout=2_000)
    state = window.capture_ui_state()

    assert state.window is not None
    # 最大化後のgeometry（画面全体）ではなく、最大化前のnormal geometryを保存する。
    assert (state.window.x, state.window.y) == (before.x, before.y)
    assert (state.window.width, state.window.height) == (before.width, before.height)
    assert state.window.maximized is True


def test_capture_does_not_save_the_minimized_state(window: MainWindow, qtbot: QtBot) -> None:
    """最小化状態は保存しない（次回はnormalまたはmaximizedで起動する）。"""
    width, height = usable_size(window)
    window.setGeometry(100, 100, width, height)
    window.show()
    qtbot.waitExposed(window)
    before = window_state_of(window)

    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.wait(20)
    state = window.capture_ui_state()

    assert state.window is not None
    assert (state.window.width, state.window.height) == (before.width, before.height)
    assert state.window.maximized is False


def test_restore_applies_the_geometry(window: MainWindow) -> None:
    """restoreで位置とサイズを適用する。"""
    width, height = usable_size(window)

    window.restore_ui_state(
        UiState(window=WindowState(x=200, y=150, width=width, height=height, maximized=False)),
        screens(),
    )

    assert window.geometry().x() == 200
    assert window.geometry().y() == 150
    assert window.width() == width
    assert window.height() == height
    assert not window.isMaximized()


def test_restore_applies_maximized_after_the_normal_geometry(
    window: MainWindow, qtbot: QtBot
) -> None:
    """最大化はnormal geometryを設定したあとに適用する。"""
    width, height = usable_size(window)

    window.restore_ui_state(
        UiState(window=WindowState(x=140, y=120, width=width, height=height, maximized=True)),
        screens(),
    )
    window.show()
    qtbot.waitExposed(window)

    assert window.isMaximized()
    captured = window_state_of(window)
    assert (captured.x, captured.y) == (140, 120)
    assert (captured.width, captured.height) == (width, height)
    assert captured.maximized is True


def test_restore_normal_state_clears_existing_maximized_state(
    window: MainWindow, qtbot: QtBot
) -> None:
    """normal状態の復元は既存の最大化・最小化フラグを明示的に解除する。"""
    width, height = usable_size(window)
    window.show()
    qtbot.waitExposed(window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.waitUntil(window.isMaximized, timeout=2_000)

    window.restore_ui_state(
        UiState(window=WindowState(x=160, y=110, width=width, height=height, maximized=False)),
        screens(),
    )
    qtbot.waitUntil(lambda: not window.isMaximized(), timeout=2_000)

    assert not window.isMinimized()
    assert (window.geometry().x(), window.geometry().y()) == (160, 110)
    assert (window.width(), window.height()) == (width, height)


def test_restore_moves_an_offscreen_window_back(window: MainWindow) -> None:
    """完全に画面外の保存値は画面内へ補正して適用する。"""
    window.restore_ui_state(
        UiState(window=WindowState(x=-5000, y=-4000, width=800, height=600, maximized=False)),
        screens(),
    )

    assert window.geometry().x() >= 0
    assert window.geometry().y() >= 0


def test_restore_and_capture_round_trip_the_splitter(window: MainWindow, qtbot: QtBot) -> None:
    """Splitterは比率として復元され、captureで取り出せる。"""
    window.resize(800, 900)
    window.show()
    qtbot.waitExposed(window)

    saved = SplitterState(600, 200)
    window.restore_ui_state(UiState(main_splitter=saved), screens())
    qtbot.wait(20)
    captured = window.capture_ui_state().main_splitter

    assert captured is not None
    assert captured.player_size > captured.playlist_size
    assert captured.player_ratio == pytest.approx(
        expected_player_ratio(window, saved, captured.total), abs=0.03
    )


def test_splitter_restore_adapts_to_the_current_window_size(
    window: MainWindow, qtbot: QtBot
) -> None:
    """保存時と違うWindow高さでも、比率を保って再配分する。"""
    window.resize(800, 500)
    window.show()
    qtbot.waitExposed(window)

    # 保存時は合計2000相当（前回のほうが大きいウィンドウ）。
    saved = SplitterState(1400, 600)
    window.restore_ui_state(UiState(main_splitter=saved), screens())
    qtbot.wait(20)
    captured = window.capture_ui_state().main_splitter

    assert captured is not None
    assert captured.total <= window.height()
    assert captured.playlist_size > 0
    assert captured.player_ratio == pytest.approx(
        expected_player_ratio(window, saved, captured.total), abs=0.03
    )


def test_splitter_restore_works_with_every_visualization_hidden(
    window: MainWindow, qtbot: QtBot
) -> None:
    """可視化を全部OFFにしてもSplitter復元が破綻せず、プレイリストが残る。"""
    window._app_settings.apply(  # pyright: ignore[reportPrivateUsage]
        AppSettings(
            1.0,
            True,
            waveform_visible=False,
            spectrum_visible=False,
            level_meter_visible=False,
        )
    )
    window.resize(800, 700)
    window.show()
    qtbot.waitExposed(window)

    saved = SplitterState(500, 200)
    window.restore_ui_state(UiState(main_splitter=saved), screens())
    qtbot.wait(20)
    captured = window.capture_ui_state().main_splitter

    assert captured is not None
    assert captured.playlist_size >= MINIMUM_SPLITTER_SIZE
    assert captured.player_ratio == pytest.approx(
        expected_player_ratio(window, saved, captured.total), abs=0.03
    )


def test_splitter_restore_works_with_every_visualization_visible(
    window: MainWindow, qtbot: QtBot
) -> None:
    """可視化を全部ONにした状態でも同じ契約で復元できる。"""
    window.resize(800, 1100)
    window.show()
    qtbot.waitExposed(window)

    saved = SplitterState(700, 300)
    window.restore_ui_state(UiState(main_splitter=saved), screens())
    qtbot.wait(20)
    captured = window.capture_ui_state().main_splitter

    assert captured is not None
    assert captured.playlist_size >= MINIMUM_SPLITTER_SIZE
    assert captured.player_ratio == pytest.approx(
        expected_player_ratio(window, saved, captured.total), abs=0.03
    )


# -- 前回フォルダー ---------------------------------------------------------


def test_file_dialog_starts_in_the_last_directory(
    window: MainWindow, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保存された前回フォルダーをファイルダイアログの初期位置へ渡す。"""
    requested: list[str] = []

    def record_dialog(*args: object, **kwargs: object) -> tuple[str, str]:
        del kwargs
        requested.append(str(args[2]))
        return ("", "")

    window.set_last_open_directory(audio_file.parent)
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", record_dialog)

    window.open_file()

    assert requested == [str(audio_file.parent)]


def test_selecting_a_file_updates_the_last_directory(
    window: MainWindow, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ファイル選択が成功したら、その親フォルダーを保存対象へ反映する。"""
    monkeypatch.setattr(
        main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(str(audio_file))
    )
    changes: list[int] = []
    window.ui_state_changed.connect(lambda: changes.append(1))

    window.open_file()

    assert window.last_open_directory == audio_file.parent
    assert len(changes) == 1


def test_cancelling_the_dialog_keeps_the_last_directory(
    window: MainWindow, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelでは前回フォルダーを変更しない。"""
    window.set_last_open_directory(audio_file.parent)
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", stub_open_dialog(""))
    changes: list[int] = []
    window.ui_state_changed.connect(lambda: changes.append(1))

    window.open_file()

    assert window.last_open_directory == audio_file.parent
    assert changes == []


def test_drag_and_drop_does_not_change_the_last_directory(
    window: MainWindow, playlist_model: PlaylistModel, audio_file: Path
) -> None:
    """プレイリストへの追加（D&D相当）では前回フォルダーを変えない。"""
    playlist_model.add_paths([audio_file])

    assert window.last_open_directory is None
    assert window.capture_ui_state().last_open_directory is None


def test_missing_last_directory_is_passed_without_synchronous_io(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前回フォルダーは存在確認せずダイアログへ渡し、GUIを事前I/Oで止めない。"""
    directory = tmp_path / "切断済みを想定した フォルダー"
    requested: list[str] = []

    def fail_if_checked(self: Path) -> bool:
        del self
        raise AssertionError("GUIスレッドでPath.is_dir()を呼んではならない")

    def record_dialog(*args: object, **kwargs: object) -> tuple[str, str]:
        del kwargs
        requested.append(str(args[2]))
        return ("", "")

    window.set_last_open_directory(directory)
    monkeypatch.setattr(Path, "is_dir", fail_if_checked)
    monkeypatch.setattr(main_window_module.QFileDialog, "getOpenFileName", record_dialog)

    window.open_file()

    assert requested == [str(directory)]


def test_relative_last_directory_is_ignored(window: MainWindow) -> None:
    """相対パスは前回フォルダーとして保持しない。"""
    window.set_last_open_directory(Path("音楽"))

    assert window.last_open_directory is None


def test_unicode_last_directory_round_trips(window: MainWindow, tmp_path: Path) -> None:
    """日本語・空白を含むフォルダーも保持できる。"""
    directory = tmp_path / "音 楽"
    directory.mkdir()

    window.set_last_open_directory(directory)

    assert window.capture_ui_state().last_open_directory == directory
    assert window.file_dialog_directory() == str(directory)


# -- 通知と責務 -------------------------------------------------------------


def test_move_and_resize_notify_once_each(window: MainWindow, qtbot: QtBot) -> None:
    """移動・リサイズ・最大化でUI状態の変更を通知する。"""
    window.show()
    qtbot.waitExposed(window)
    changes: list[int] = []
    window.ui_state_changed.connect(lambda: changes.append(1))

    window.move(180, 140)
    window.resize(840, 680)
    qtbot.wait(20)

    assert len(changes) >= 2


def test_restore_does_not_notify(window: MainWindow, qtbot: QtBot) -> None:
    """復元適用をユーザー変更として通知しない。"""
    window.show()
    qtbot.waitExposed(window)
    changes: list[int] = []
    window.ui_state_changed.connect(lambda: changes.append(1))

    window.restore_ui_state(
        UiState(
            window=WindowState(x=210, y=160, width=900, height=700, maximized=False),
            main_splitter=SplitterState(400, 300),
            last_open_directory=Path("C:\\Music"),
        ),
        screens(),
    )

    assert changes == []


def test_main_window_does_not_know_the_ui_state_file() -> None:
    """MainWindowはJSON・schema version・保存先・保存タイマーを持たない。"""
    for forbidden in (
        "json",
        "load_ui_state",
        "save_ui_state",
        "UiStateSession",
        "UI_STATE_SCHEMA_VERSION",
        "default_ui_state_path",
    ):
        assert not hasattr(main_window_module, forbidden), forbidden

"""メインウィンドウ。

レイアウト骨格・メニュー・現在のファイル表示・ステータス表示だけを担当し、
再生操作の細部は PlayerControls へ委譲する（god class にしない）。
"""

import logging
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QMoveEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus, PlaybackError
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services.pcm_tap import PcmTap
from sdp.services.settings import AppSettings, AppSettingsController
from sdp.services.ui_state import (
    ScreenRect,
    SplitterState,
    UiState,
    WindowState,
    distribute_splitter_sizes,
    fit_window_state,
)
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.legal_dialog import ABOUT_NOTICE, LegalDocumentDialog, load_legal_document
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistView
from sdp.ui.settings_dialog import SettingsDialog
from sdp.ui.shortcuts import ShortcutManager
from sdp.ui.spectrum_panel import SpectrumPanel
from sdp.ui.speed_panel import SpeedPanel
from sdp.ui.waveform_panel import WaveformPanel

_logger = logging.getLogger(__name__)

WINDOW_TITLE = "sdp"
NO_FILE_TEXT = "ファイル未選択"

# ファイルダイアログのフィルターはユーザー補助にすぎない。拡張子で再生可否を
# 断定しないため（ADR-0001 の制約 3）、「すべてのファイル」も必ず選べるようにする。
FILE_DIALOG_FILTER = ";;".join(
    [
        "音声ファイル (*.wav *.mp3 *.ogg *.opus *.flac *.m4a *.aac)",
        "WAV (*.wav)",
        "MP3 (*.mp3)",
        "OGG Vorbis (*.ogg)",
        "OGG Opus (*.opus)",
        "FLAC (*.flac)",
        "M4A / AAC (*.m4a *.aac)",
        "すべてのファイル (*)",
    ]
)

_MEDIA_STATUS_MESSAGES: dict[MediaStatus, str] = {
    MediaStatus.LOADING: "読み込み中...",
    MediaStatus.LOADED: "読み込み完了",
    MediaStatus.BUFFERED: "読み込み完了",
    MediaStatus.STALLED: "再生が一時的に停止しています",
    MediaStatus.BUFFERING: "バッファリング中...",
    MediaStatus.END_OF_MEDIA: "再生終了",
    MediaStatus.INVALID_MEDIA: "音声ファイルを読み込めませんでした",
}


class MainWindow(QMainWindow):
    """メインウィンドウ。アプリ層で組み立て済みの依存だけを受け取る。

    レイアウト骨格・メニュー・ステータス表示に徹し、再生操作は PlayerControls、
    波形操作はWaveformPanel、プレイリスト操作はPlaylistViewへ委譲する。
    永続化（playlist.json／settings.json／ui-state.json）は知らない。
    UI状態は :meth:`capture_ui_state` / :meth:`restore_ui_state` の小さなAPIで
    受け渡すだけで、JSON・schema version・保存先・保存タイマー・破損処理は持たない。
    """

    ui_state_changed = Signal()
    """位置・サイズ・最大化・Splitter・前回フォルダーのいずれかが変わった。

    復元適用中は発火しない（復元をユーザー変更として保存予約しないため）。
    """

    def __init__(
        self,
        controller: PlaybackController,
        playlist_model: PlaylistModel,
        playlist_playback: PlaylistPlaybackController,
        waveform_analysis: WaveformAnalysisService,
        pcm_tap: PcmTap,
        app_settings: AppSettingsController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAccessibleName("sdpメインウィンドウ")
        self._controller = controller
        self._app_settings = app_settings
        self._settings_dialog: SettingsDialog | None = None
        self._legal_dialog: LegalDocumentDialog | None = None
        self._has_current_source_error = False
        self._last_open_directory: Path | None = None
        # 表示前はレイアウトが確定しないため、最初のshowEventで比率を適用し直す。
        self._pending_splitter: SplitterState | None = None
        self._restoring_ui_state = False

        self.setWindowTitle(WINDOW_TITLE)

        self._file_name_label = QLabel(NO_FILE_TEXT)
        self._file_name_label.setObjectName("fileNameLabel")
        self._file_name_label.setAccessibleName("現在のファイル")
        self._controls = PlayerControls(controller)
        self._speed_panel = SpeedPanel(controller)
        self._waveform_panel = WaveformPanel(controller, waveform_analysis)
        # FFT・タイマー・リングバッファの扱いはPanel側の責務。Windowは配置だけ行う。
        self._spectrum_panel = SpectrumPanel(controller, pcm_tap)
        self._playlist_view = PlaylistView(playlist_model)

        player_panel = QWidget()
        player_layout = QVBoxLayout(player_panel)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.addWidget(self._file_name_label)
        player_layout.addWidget(self._controls)
        player_layout.addWidget(self._speed_panel)
        player_layout.addWidget(self._waveform_panel)
        player_layout.addWidget(self._spectrum_panel)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setObjectName("mainSplitter")
        splitter.addWidget(player_panel)
        splitter.addWidget(self._playlist_view)
        # プレイリストが十分な高さを持つようにする。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.splitterMoved.connect(self._on_ui_state_changed)
        self._splitter = splitter
        self.setCentralWidget(splitter)
        # 波形とスペクトラムが増えた分だけ既定の高さを広げ、プレイリスト領域を残す。
        self.resize(720, 700)

        self._build_menu()
        self.statusBar().showMessage("音声ファイルを開いてください。")

        controller.source_changed.connect(self._on_source_changed)
        controller.media_status_changed.connect(self._on_media_status_changed)
        controller.error_occurred.connect(self._on_error_occurred)
        self._playlist_view.message_requested.connect(self.show_status_message)

        # プレイリスト再生の配線。次曲探索や欠損スキップの判断はここに置かない。
        self._playlist_view.entry_activated.connect(playlist_playback.play_entry)
        self._controls.previous_requested.connect(playlist_playback.play_previous)
        self._controls.next_requested.connect(playlist_playback.play_next)
        playlist_playback.current_entry_changed.connect(self._on_current_entry_changed)
        playlist_playback.navigation_availability_changed.connect(
            self._controls.set_playlist_navigation_available
        )
        playlist_playback.message_requested.connect(self.show_status_message)
        self._controls.repeat_mode_requested.connect(playlist_playback.cycle_repeat_mode)
        self._controls.shuffle_toggled.connect(playlist_playback.set_shuffle_enabled)
        playlist_playback.repeat_mode_changed.connect(self._controls.set_repeat_mode)
        playlist_playback.shuffle_enabled_changed.connect(self._controls.set_shuffle_enabled)

        # Controller が Model 復元後に作られた場合も、接続前に確定していた状態を反映する。
        self._on_current_entry_changed(playlist_playback.current_entry_id)
        self._controls.set_playlist_navigation_available(
            playlist_playback.can_play_previous,
            playlist_playback.can_play_next,
        )
        self._controls.set_repeat_mode(playlist_playback.repeat_mode)
        self._controls.set_shuffle_enabled(playlist_playback.shuffle_enabled)
        # ショートカットは既存Controller群への別入力経路。Windowが寿命だけを保持する。
        self._shortcut_manager = ShortcutManager(
            self,
            controller,
            playlist_playback,
            parent=self,
        )

        # 復元済み設定を表示前に反映し、可視化が一瞬見えてから消えるのを防ぐ。
        app_settings.settings_changed.connect(self._on_settings_changed)
        self._apply_visualization_settings(app_settings.settings)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        open_action = QAction("開く...(&O)", self)
        open_action.setObjectName("openAction")
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        add_to_playlist_action = QAction("プレイリストに追加...(&A)", self)
        add_to_playlist_action.setObjectName("addToPlaylistAction")
        add_to_playlist_action.setShortcut("Ctrl+Shift+O")
        add_to_playlist_action.triggered.connect(self._playlist_view.add_files)
        file_menu.addAction(add_to_playlist_action)

        file_menu.addSeparator()

        quit_action = QAction("終了(&X)", self)
        quit_action.setObjectName("quitAction")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools_menu = self.menuBar().addMenu("ツール(&T)")
        settings_action = QAction("設定...(&S)", self)
        settings_action.setObjectName("openSettingsAction")
        settings_action.triggered.connect(self.open_settings)
        tools_menu.addAction(settings_action)

        help_menu = self.menuBar().addMenu("ヘルプ(&H)")
        for label, object_name, callback in (
            ("sdpについて(&A)", "aboutAction", self.show_about),
            ("ライセンスを表示(&L)", "showLicenseAction", self.show_license),
            ("第三者ライセンス(&T)", "showThirdPartyLicensesAction", self.show_third_party),
        ):
            action = QAction(label, self)
            action.setObjectName(object_name)
            action.triggered.connect(callback)
            help_menu.addAction(action)

    # -- 操作 ---------------------------------------------------------------

    @property
    def spectrum_panel(self) -> SpectrumPanel:
        """終了処理でタイマーを止めるために composition root へ公開する。"""
        return self._spectrum_panel

    @property
    def waveform_panel(self) -> WaveformPanel:
        """表示ON/OFFの検証のために公開する（Windowは配置だけを行う）。"""
        return self._waveform_panel

    @property
    def settings_dialog(self) -> SettingsDialog | None:
        """開いている設定ダイアログ（無ければ ``None``）。"""
        return self._settings_dialog

    @property
    def legal_dialog(self) -> LegalDocumentDialog | None:
        """開いている法的告知ダイアログ（無ければ``None``）。"""
        return self._legal_dialog

    def show_about(self) -> None:
        """GPLの適切な法的告知を表示する。"""
        self._show_legal_dialog("sdpについて", ABOUT_NOTICE)

    def show_license(self) -> None:
        """GPLv3本文を表示する。"""
        self._show_legal_dialog("ライセンス", load_legal_document("LICENSE"))

    def show_third_party(self) -> None:
        """第三者ソフトウェアの通知を表示する。"""
        self._show_legal_dialog("第三者ライセンス", load_legal_document("THIRD_PARTY_NOTICES.txt"))

    def _show_legal_dialog(self, title: str, text: str) -> None:
        dialog = self._legal_dialog
        if dialog is not None:
            dialog.close()
        dialog = LegalDocumentDialog(title, text, self)
        dialog.finished.connect(partial(self._forget_legal_dialog, dialog))
        self._legal_dialog = dialog
        dialog.show()

    def _forget_legal_dialog(self, dialog: LegalDocumentDialog, _result: int) -> None:
        if self._legal_dialog is dialog:
            self._legal_dialog = None

    def open_settings(self) -> None:
        """設定ダイアログを開く。既に開いていれば前面へ出す（二重に開かない）。"""
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = SettingsDialog(self._app_settings.settings, self)
        dialog.settings_requested.connect(self._on_settings_requested)
        dialog.finished.connect(self._on_settings_dialog_finished)
        self._settings_dialog = dialog
        dialog.show()

    def show_status_message(self, message: str) -> None:
        """ステータスバーへ短いメッセージを表示する。"""
        self.statusBar().showMessage(message)

    def open_file(self) -> None:
        """ファイルダイアログで選んだ音源を読み込む。キャンセル時は何もしない。"""
        selected, _ = QFileDialog.getOpenFileName(
            self, "音声ファイルを開く", self.file_dialog_directory(), FILE_DIALOG_FILTER
        )
        if not selected:
            # Cancelでは前回フォルダーを変更しない。
            return
        path = Path(selected)
        self.set_last_open_directory(path.parent)
        self._controller.load(path)

    # -- UI状態（P6-B）-------------------------------------------------------

    @property
    def last_open_directory(self) -> Path | None:
        """「開く...」で最後に使ったフォルダー（未使用なら ``None``）。"""
        return self._last_open_directory

    def set_last_open_directory(self, path: Path | None) -> None:
        """前回フォルダーを更新する。相対パスは無視する。"""
        directory = path
        if directory is not None and not directory.is_absolute():
            _logger.debug("相対パスは前回フォルダーとして保存しません: %s", directory)
            return
        if directory == self._last_open_directory:
            return
        self._last_open_directory = directory
        self._on_ui_state_changed()

    def file_dialog_directory(self) -> str:
        """ファイルダイアログの初期ディレクトリ。

        GUIスレッドで存在確認すると切断済みネットワークパス等で停止し得るため、
        保存値はI/OせずそのままQtへ渡す。未保存の場合だけ空文字を返す。
        """
        directory = self._last_open_directory
        return "" if directory is None else str(directory)

    def connect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        """UI状態の変更通知を購読する（UiStateSession用の小さな入口）。"""
        self.ui_state_changed.connect(slot)

    def disconnect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        self.ui_state_changed.disconnect(slot)

    def capture_ui_state(self) -> UiState:
        """現在のウィンドウ状態を取得する（保存はしない）。

        最大化・全画面中でも**normal geometry**を返し、最小化状態は保存しない。
        """
        return UiState(
            window=self._capture_window_state(),
            main_splitter=self._capture_splitter_state(),
            last_open_directory=self._last_open_directory,
        )

    def restore_ui_state(self, state: UiState, screens: Sequence[ScreenRect] | None = None) -> None:
        """保存済みUI状態を適用する（表示前に呼ぶ）。

        geometry は現在の画面構成へ補正してから適用し、最大化はそのあとで
        設定する（最大化を先にすると normal geometry が失われる）。
        Splitterは可視化の表示設定を適用したあとの比率として反映する。
        """
        self._restoring_ui_state = True
        try:
            window = state.window
            if window is not None:
                available = self._screen_rects() if screens is None else list(screens)
                minimum = self.minimumSizeHint()
                fitted = fit_window_state(
                    window,
                    available,
                    minimum_size=(max(1, minimum.width()), max(1, minimum.height())),
                )
                self.setGeometry(fitted.x, fitted.y, fitted.width, fitted.height)
                window_state = self.windowState() & ~Qt.WindowState.WindowMinimized
                if fitted.maximized:
                    window_state |= Qt.WindowState.WindowMaximized
                else:
                    window_state &= ~Qt.WindowState.WindowMaximized
                self.setWindowState(window_state)
            splitter = state.main_splitter
            if splitter is not None:
                self._pending_splitter = splitter
                self._apply_splitter_state(splitter)
            if state.last_open_directory is not None:
                self._last_open_directory = state.last_open_directory
        finally:
            self._restoring_ui_state = False

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        pending = self._pending_splitter
        if pending is None:
            return
        # 表示でレイアウトが確定してから、保存比率をもう一度当てる。
        self._pending_splitter = None
        self._restoring_ui_state = True
        try:
            self._apply_splitter_state(pending)
        finally:
            self._restoring_ui_state = False

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self._on_ui_state_changed()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._on_ui_state_changed()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() is QEvent.Type.WindowStateChange:
            self._on_ui_state_changed()

    def _on_ui_state_changed(self, *args: object) -> None:
        del args
        # 復元適用そのものをユーザー変更として保存予約させない。
        if not self._restoring_ui_state:
            self.ui_state_changed.emit()

    def _capture_window_state(self) -> WindowState:
        # 最大化・全画面中は geometry() が画面全体になるため normalGeometry を使う。
        rectangle = self.normalGeometry()
        if not rectangle.isValid():
            rectangle = self.geometry()
        # 最小化中でも「最大化されていたかどうか」は保持する（最小化自体は保存しない）。
        maximized = bool(self.windowState() & Qt.WindowState.WindowMaximized)
        return WindowState(
            x=rectangle.x(),
            y=rectangle.y(),
            width=max(1, rectangle.width()),
            height=max(1, rectangle.height()),
            maximized=maximized,
        )

    def _capture_splitter_state(self) -> SplitterState | None:
        sizes = self._splitter.sizes()
        if len(sizes) != 2 or sum(sizes) < 1:
            return None
        return SplitterState(player_size=max(0, sizes[0]), playlist_size=max(0, sizes[1]))

    def _apply_splitter_state(self, state: SplitterState) -> None:
        total = sum(self._splitter.sizes())
        player, playlist = distribute_splitter_sizes(state, total)
        self._splitter.setSizes([player, playlist])

    def _screen_rects(self) -> list[ScreenRect]:
        """現在の画面の利用可能領域（primaryを先頭にする）。"""
        primary = QGuiApplication.primaryScreen()
        screens = list(QGuiApplication.screens())
        # PySideのstubはNoneを含めないが、screen無し環境では実際にNoneになり得る。
        if primary is not None and primary in screens:  # pyright: ignore[reportUnnecessaryComparison]
            screens.remove(primary)
            screens.insert(0, primary)
        rectangles: list[ScreenRect] = []
        for screen in screens:
            available = screen.availableGeometry()
            if available.width() < 1 or available.height() < 1:
                continue
            rectangles.append(
                ScreenRect(
                    x=available.x(),
                    y=available.y(),
                    width=available.width(),
                    height=available.height(),
                )
            )
        return rectangles

    # -- Controller からの通知 ----------------------------------------------

    def _on_source_changed(self, source: object) -> None:
        self._has_current_source_error = False
        if not isinstance(source, Path):
            self._file_name_label.setText(NO_FILE_TEXT)
            self._file_name_label.setToolTip("")
            self.setWindowTitle(WINDOW_TITLE)
            self.statusBar().showMessage("音声ファイルを開いてください。")
            return
        # メタデータ（タイトル・アーティスト）は P2 の責務。ここではファイル名だけ。
        self._file_name_label.setText(source.name)
        self._file_name_label.setToolTip(str(source))
        self.setWindowTitle(f"{WINDOW_TITLE} — {source.name}")
        self.statusBar().showMessage(_MEDIA_STATUS_MESSAGES[MediaStatus.LOADING])

    def _on_current_entry_changed(self, entry_id: object) -> None:
        self._playlist_view.set_current_entry_id(entry_id if isinstance(entry_id, str) else None)

    def _on_media_status_changed(self, status: MediaStatus, load_generation: int) -> None:
        # 前sourceの遅延statusはPlaybackControllerが除外済みのため、
        # ここへ届くのは常に現在sourceの状況（世代は表示に使わない）。
        del load_generation
        if status is MediaStatus.INVALID_MEDIA and self._has_current_source_error:
            return
        message = _MEDIA_STATUS_MESSAGES.get(status)
        if message is not None:
            self.statusBar().showMessage(message)

    # -- 設定 ---------------------------------------------------------------

    def _on_settings_requested(self, settings: object) -> None:
        """ダイアログの適用要求を調停サービスへ渡すだけ（分岐も保存も持たない）。"""
        if not isinstance(settings, AppSettings):
            return
        try:
            self._app_settings.apply(settings)
        except Exception:
            # Qt slot境界から例外を漏らさず、ダイアログを閉じない。
            _logger.exception("設定を適用できませんでした")
            self.show_status_message("設定を適用できませんでした。")
            dialog = self._settings_dialog
            if dialog is not None:
                dialog.show_apply_error()
            return
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.mark_applied(self._app_settings.settings)

    def _on_settings_dialog_finished(self, result: int) -> None:
        del result
        dialog = self._settings_dialog
        self._settings_dialog = None
        if dialog is not None:
            dialog.deleteLater()

    def _on_settings_changed(self, settings: object) -> None:
        if not isinstance(settings, AppSettings):
            return
        self._apply_visualization_settings(settings)
        dialog = self._settings_dialog
        if dialog is not None:
            # PlayerControlsやショートカットからの変更は、ダイアログを編集中でなければ
            # 取り込む。編集中は入力を勝手に書き換えない（未適用編集を失わせない）。
            dialog.refresh_if_unedited(settings)

    def _apply_visualization_settings(self, settings: AppSettings) -> None:
        """可視化の表示ON/OFFを各Panelへ配る（解析停止は各Panelの責務）。"""
        self._waveform_panel.setVisible(settings.waveform_visible)
        self._spectrum_panel.set_spectrum_visible(settings.spectrum_visible)
        self._spectrum_panel.set_level_meter_visible(settings.level_meter_visible)

    # -- Controller からの通知（続き）---------------------------------------

    def _on_error_occurred(self, error: PlaybackError) -> None:
        # ユーザーへは message だけを見せる。detail は画面へ出さない。
        # 技術詳細のログ記録は PlaybackController の責務のため、ここでは記録しない。
        # 通常の再生エラーでモーダルダイアログを出すと連続操作を妨げるため使わない。
        self._has_current_source_error = True
        self.statusBar().showMessage(error.message)

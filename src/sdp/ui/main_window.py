"""メインウィンドウ。

レイアウト骨格・メニュー・現在のファイル表示・ステータス表示だけを担当し、
再生操作の細部は PlayerControls へ委譲する（god class にしない）。
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
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
from sdp.services.waveform_analysis import WaveformAnalysisService
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
    永続化（playlist.json）は知らない。
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
        self._controller = controller
        self._app_settings = app_settings
        self._settings_dialog: SettingsDialog | None = None
        self._has_current_source_error = False

        self.setWindowTitle(WINDOW_TITLE)

        self._file_name_label = QLabel(NO_FILE_TEXT)
        self._file_name_label.setObjectName("fileNameLabel")
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
        splitter.addWidget(player_panel)
        splitter.addWidget(self._playlist_view)
        # プレイリストが十分な高さを持つようにする。
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
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
            self, "音声ファイルを開く", "", FILE_DIALOG_FILTER
        )
        if not selected:
            return
        self._controller.load(Path(selected))

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

    def _on_media_status_changed(self, status: MediaStatus) -> None:
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
        if isinstance(settings, AppSettings):
            self._apply_visualization_settings(settings)

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

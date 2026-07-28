"""メインウィンドウ。

レイアウト骨格・メニュー・現在のファイル表示・ステータス表示だけを担当し、
再生操作の細部は PlayerControls へ委譲する（god class にしない）。
"""

from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFileDialog, QLabel, QMainWindow, QVBoxLayout, QWidget

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus, PlaybackError
from sdp.ui.player_controls import PlayerControls

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
    """単曲再生のメインウィンドウ。受け取るのは PlaybackController だけ。"""

    def __init__(self, controller: PlaybackController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._has_current_source_error = False

        self.setWindowTitle(WINDOW_TITLE)

        self._file_name_label = QLabel(NO_FILE_TEXT)
        self._file_name_label.setObjectName("fileNameLabel")
        self._controls = PlayerControls(controller)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addWidget(self._file_name_label)
        layout.addWidget(self._controls)
        layout.addStretch(1)
        self.setCentralWidget(central)

        self._build_menu()
        self.statusBar().showMessage("音声ファイルを開いてください。")

        controller.source_changed.connect(self._on_source_changed)
        controller.media_status_changed.connect(self._on_media_status_changed)
        controller.error_occurred.connect(self._on_error_occurred)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        open_action = QAction("開く...(&O)", self)
        open_action.setObjectName("openAction")
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        quit_action = QAction("終了(&X)", self)
        quit_action.setObjectName("quitAction")
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # -- 操作 ---------------------------------------------------------------

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

    def _on_media_status_changed(self, status: MediaStatus) -> None:
        if status is MediaStatus.INVALID_MEDIA and self._has_current_source_error:
            return
        message = _MEDIA_STATUS_MESSAGES.get(status)
        if message is not None:
            self.statusBar().showMessage(message)

    def _on_error_occurred(self, error: PlaybackError) -> None:
        # ユーザーへは message だけを見せる。detail は画面へ出さない。
        # 技術詳細のログ記録は PlaybackController の責務のため、ここでは記録しない。
        # 通常の再生エラーでモーダルダイアログを出すと連続操作を妨げるため使わない。
        self._has_current_source_error = True
        self.statusBar().showMessage(error.message)

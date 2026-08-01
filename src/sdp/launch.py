"""起動要求を既存compositionへ適用する小さなアダプター。"""

import logging

from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QApplication

from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services.launch_request import LaunchRequest
from sdp.ui.main_window import MainWindow

_logger = logging.getLogger(__name__)


class LaunchRequestHandler(QObject):
    """起動要求をプレイリスト末尾へ追加し、既存Windowへ通知する。"""

    def __init__(
        self,
        playlist: PlaylistModel,
        playlist_playback: PlaylistPlaybackController,
        window: MainWindow,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playlist = playlist
        self._playlist_playback = playlist_playback
        self._window = window

    def apply_initial(self, request: LaunchRequest) -> None:
        """保存済みplaylist復元後の初回要求を適用する。前面化は行わない。"""
        self._apply(request, initial=True)

    @Slot(object)
    def handle_received(self, value: object) -> None:
        """実行中にIPCで届いた検証済み要求を適用する。"""
        if not isinstance(value, LaunchRequest):
            _logger.error("LaunchRequest以外の受信通知を拒否しました: %r", type(value))
            return
        self._apply(value, initial=False)

    def _apply(self, request: LaunchRequest, *, initial: bool) -> None:
        if request.ignored_arguments:
            _logger.warning(
                "起動引数のうち%d件を無視しました: %r",
                len(request.ignored_arguments),
                request.ignored_arguments,
            )
        added_entry_ids: tuple[str, ...] = ()
        if request.paths:
            added_entry_ids = self._playlist.add_paths(request.paths)
            message = f"{len(request.paths)}曲をプレイリストへ追加しました。"
            if request.ignored_arguments:
                message += f" {len(request.ignored_arguments)}件の引数を無視しました。"
            self._window.show_status_message(message)
        elif request.ignored_arguments:
            self._window.show_status_message("追加できるファイルがありませんでした。")
        # argvはExplorerの関連付け起動と明示的な引数付き起動を表す。保存済みの
        # current entryではなく、今回追加した先頭entryを再生する。
        if added_entry_ids:
            self._playlist_playback.play_entry(added_entry_ids[0])
        if not initial and request.activate_window:
            self._activate_window()

    def _activate_window(self) -> None:
        state = self._window.windowState()
        if state & Qt.WindowState.WindowMinimized:
            # WindowMaximizedは残し、最小化だけを解除する。
            self._window.setWindowState(state & ~Qt.WindowState.WindowMinimized)
            self._window.show()
        self._window.raise_()
        self._window.activateWindow()
        if not self._window.isActiveWindow():
            _logger.info("OSのforeground制約により前面化できないためalertを要求します")
            QApplication.alert(self._window, 0)

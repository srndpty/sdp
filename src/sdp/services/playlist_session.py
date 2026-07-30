"""プレイリストの起動時復元とデバウンス保存。

保存先の決定と読み書きのタイミングだけを担当する小さなライフサイクルサービス。
UI（`ui/`）はこのモジュールを import しない。呼び出すのは composition root。
PlaylistModel 自身へ save / load を持たせない。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import PlaylistFileError, load_playlist, save_playlist
from sdp.services.user_paths import app_data_directory

_logger = logging.getLogger(__name__)

PLAYLIST_FILE_NAME = "playlist.json"
DEFAULT_DEBOUNCE_MS = 1_500
DEFAULT_RETRY_MS = 5_000

RESTORE_FAILED_MESSAGE = "プレイリストの復元に失敗しました。ログを確認してください。"


def default_playlist_path() -> Path:
    """既定の保存先（``%LOCALAPPDATA%\\sdp\\playlist.json``）。"""
    return app_data_directory() / PLAYLIST_FILE_NAME


class PlaylistSession(QObject):
    """プレイリストファイルとモデルの間を取り持つ。

    復元に失敗した場合は、その起動中の保存を無効にする。空のモデルを保存して
    既存ファイルを上書きすると、ユーザーのプレイリストが失われるため。
    """

    save_failed = Signal()
    """保存に失敗した（成功→失敗へ変わったときだけ）。"""

    save_recovered = Signal()
    """失敗のあとに保存できた（失敗→成功へ変わったときだけ）。"""

    def __init__(
        self,
        file_path: Path,
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        retry_ms: int = DEFAULT_RETRY_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._save_enabled = True
        self._model: PlaylistModel | None = None
        self._started = False
        self._debounce_ms = max(1, debounce_ms)
        self._retry_ms = max(1, retry_ms)
        self._retry_attempted = False
        self._save_failed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._debounce_ms)
        self._timer.timeout.connect(self.flush)

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def is_save_enabled(self) -> bool:
        """保存を行ってよいか。復元に失敗した起動では ``False``。"""
        return self._save_enabled

    @property
    def is_running(self) -> bool:
        return self._started

    def load_into(self, model: PlaylistModel) -> str | None:
        """保存済みプレイリストをモデルへ復元し、UI 向けメッセージを返す。

        ファイルが無い場合は初回起動として空のまま正常終了し、``None`` を返す。
        壊れている場合と読み込み I/O に失敗した場合は、技術詳細をログへ残し、
        空のモデルで起動しつつ**この起動中の保存を無効化**する。
        """
        try:
            entries = load_playlist(self._file_path)
        except (PlaylistFileError, OSError):
            _logger.exception("プレイリストの復元に失敗しました: %s", self._file_path)
            self._save_enabled = False
            return RESTORE_FAILED_MESSAGE

        model.replace_entries(entries)
        self._model = model
        if not entries:
            return None
        return f"プレイリストを復元しました（{len(entries)}件）。"

    def start(self, model: PlaylistModel | None = None) -> None:
        """構造変更の監視とデバウンス保存を開始する（冪等）。"""
        if model is not None:
            self._model = model
        if self._started or self._model is None:
            return
        self._started = True
        self._model.rowsInserted.connect(self._schedule_save)
        self._model.rowsRemoved.connect(self._schedule_save)
        self._model.rowsMoved.connect(self._schedule_save)
        self._model.modelReset.connect(self._schedule_save)

    def flush(self) -> bool:
        """監視中Modelの現在の並びを即時保存する。"""
        timer_triggered = self.sender() is self._timer
        self._timer.stop()
        model = self._model
        if model is None:
            return False
        return self._save(model, retry_on_failure=timer_triggered)

    def save_from(self, model: PlaylistModel) -> bool:
        """モデルの現在の並びを保存する。保存したら ``True``。

        復元に失敗した起動では何もしない（既存ファイルを守る）。
        保存の失敗はログへ残すだけにして、終了処理を止めない。
        """
        self._model = model
        self._timer.stop()
        return self._save(model, retry_on_failure=False)

    def _save(self, model: PlaylistModel, *, retry_on_failure: bool) -> bool:
        if not self._save_enabled:
            _logger.info("復元に失敗したため、プレイリストを保存しません: %s", self._file_path)
            return False
        try:
            save_playlist(self._file_path, model.entries())
        except (OSError, ValueError):
            _logger.exception("プレイリストの保存に失敗しました: %s", self._file_path)
            if retry_on_failure and self._started and not self._retry_attempted:
                self._retry_attempted = True
                self._timer.start(self._retry_ms)
                _logger.info("プレイリスト保存を%dミリ秒後に1回再試行します", self._retry_ms)
            self._report_failure()
            return False
        self._retry_attempted = False
        self._report_success()
        return True

    def _schedule_save(self, *args: object) -> None:
        del args
        if self._save_enabled:
            self._retry_attempted = False
            self._timer.start(self._debounce_ms)

    def _report_failure(self) -> None:
        if self._save_failed:
            return
        self._save_failed = True
        self.save_failed.emit()

    def _report_success(self) -> None:
        if not self._save_failed:
            return
        self._save_failed = False
        self.save_recovered.emit()

    def stop(self) -> None:
        """保存タイマーとModel監視を止める（冪等）。"""
        self._timer.stop()
        model = self._model
        if not self._started or model is None:
            return
        self._started = False
        model.rowsInserted.disconnect(self._schedule_save)
        model.rowsRemoved.disconnect(self._schedule_save)
        model.rowsMoved.disconnect(self._schedule_save)
        model.modelReset.disconnect(self._schedule_save)

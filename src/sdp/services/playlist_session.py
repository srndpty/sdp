"""プレイリストの起動時復元と終了時保存。

保存先の決定と読み書きのタイミングだけを担当する小さなライフサイクルサービス。
UI（`ui/`）はこのモジュールを import しない。呼び出すのは composition root。
PlaylistModel 自身へ save / load を持たせない。
"""

import logging
from pathlib import Path

from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.persistence import PlaylistFileError, load_playlist, save_playlist
from sdp.services.user_paths import app_data_directory

_logger = logging.getLogger(__name__)

PLAYLIST_FILE_NAME = "playlist.json"

RESTORE_FAILED_MESSAGE = "プレイリストの復元に失敗しました。ログを確認してください。"


def default_playlist_path() -> Path:
    """既定の保存先（``%LOCALAPPDATA%\\sdp\\playlist.json``）。"""
    return app_data_directory() / PLAYLIST_FILE_NAME


class PlaylistSession:
    """プレイリストファイルとモデルの間を取り持つ。

    復元に失敗した場合は、その起動中の保存を無効にする。空のモデルを保存して
    既存ファイルを上書きすると、ユーザーのプレイリストが失われるため。
    """

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._save_enabled = True

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def is_save_enabled(self) -> bool:
        """終了時保存を行ってよいか。復元に失敗した起動では ``False``。"""
        return self._save_enabled

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

        if not entries:
            return None
        model.replace_entries(entries)
        return f"プレイリストを復元しました（{len(entries)}件）。"

    def save_from(self, model: PlaylistModel) -> bool:
        """モデルの現在の並びを保存する。保存したら ``True``。

        復元に失敗した起動では何もしない（既存ファイルを守る）。
        保存の失敗はログへ残すだけにして、終了処理を止めない。
        """
        if not self._save_enabled:
            _logger.info("復元に失敗したため、プレイリストを保存しません: %s", self._file_path)
            return False
        try:
            save_playlist(self._file_path, model.entries())
        except (OSError, ValueError):
            _logger.exception("プレイリストの保存に失敗しました: %s", self._file_path)
            return False
        return True

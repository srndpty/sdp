"""ユーザーごとのアプリデータ置き場。

「sdp のデータをどこへ置くか」という規則を 1 か所へ集約する
（ログとプレイリストで別々に決めない）。
"""

import os
import tempfile
from pathlib import Path

APP_DIR_NAME = "sdp"
SETTINGS_FILE_NAME = "settings.json"
UI_STATE_FILE_NAME = "ui-state.json"
WAVEFORM_CACHE_DIRECTORY = Path("cache") / "waveforms"


def app_data_directory() -> Path:
    """ユーザーごとのアプリデータディレクトリを返す。

    Windows の通常環境では ``%LOCALAPPDATA%\\sdp``。
    ``LOCALAPPDATA`` が無い環境（一部の CI やテスト）では一時ディレクトリ配下を使う。
    互換レイヤーはこの 1 段だけとし、それ以上の分岐は作らない。
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return base / APP_DIR_NAME


def default_settings_path() -> Path:
    """既定の設定保存先（``%LOCALAPPDATA%\\sdp\\settings.json``）。"""
    return app_data_directory() / SETTINGS_FILE_NAME


def default_ui_state_path() -> Path:
    """既定のUI状態保存先（``%LOCALAPPDATA%\\sdp\\ui-state.json``）。

    設定画面から明示的に変更する ``settings.json`` とは別ファイルにする
    （日常利用で自然に変わる状態を混ぜない）。
    """
    return app_data_directory() / UI_STATE_FILE_NAME


def default_waveform_cache_directory() -> Path:
    """既定の波形キャッシュ保存先。"""
    return app_data_directory() / WAVEFORM_CACHE_DIRECTORY

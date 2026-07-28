"""ログ出力の設定。標準ライブラリのみで構成する。

出力先は ``%LOCALAPPDATA%\\sdp\\logs\\sdp.log``（RotatingFileHandler、UTF-8）。
再生エラーの記録は PlaybackController の責務のため、UI 側で重複して記録しない。

Qt のログ統合（``qInstallMessageHandler``）はここへ含めない。
ADR-0001 の制約 11 のとおり終了前に解除しないとアクセス違反で落ちるため、
終了処理と併せて別途扱う。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from sdp.services.user_paths import app_data_directory

LOG_FILE_NAME = "sdp.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
_HANDLER_NAME = "sdp-rotating-file"

_logger = logging.getLogger(__name__)
_excepthook_installed = False


def default_log_directory() -> Path:
    """ログの出力先ディレクトリを返す。

    Windows の通常環境では ``%LOCALAPPDATA%\\sdp\\logs``。置き場所の規則は
    :func:`sdp.services.user_paths.app_data_directory` へ集約している。
    """
    return app_data_directory() / "logs"


def configure_logging(*, level: int = logging.INFO, log_directory: Path | None = None) -> Path:
    """ルートロガーへローテーション付きファイル出力を設定し、ログファイルのパスを返す。

    同じプロセスで複数回呼ばれてもハンドラーを重複追加しない。
    """
    directory = default_log_directory() if log_directory is None else log_directory
    directory.mkdir(parents=True, exist_ok=True)
    log_path = (directory / LOG_FILE_NAME).resolve()

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if existing.get_name() != _HANDLER_NAME:
            continue
        if isinstance(existing, RotatingFileHandler):
            existing_path = Path(existing.baseFilename).resolve()
            if existing_path == log_path:
                return existing_path
        root.removeHandler(existing)
        existing.close()

    handler = RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    return log_path


def install_excepthook() -> None:
    """未捕捉の Python 例外をログへ記録する。多重インストールはしない。"""
    global _excepthook_installed
    if _excepthook_installed:
        return
    previous = sys.excepthook

    def hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            _logger.critical("未捕捉の例外", exc_info=(exc_type, exc_value, exc_traceback))
        previous(exc_type, exc_value, exc_traceback)

    sys.excepthook = hook
    _excepthook_installed = True

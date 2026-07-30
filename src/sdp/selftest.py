"""配布版からGUIを表示せず実行する軽量な自己診断。"""

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioDecoder, QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from sdp import __version__
from sdp.app import create_application
from sdp.services import logging_setup
from sdp.services.user_paths import app_data_directory

_logger = logging.getLogger(__name__)

SELFTEST_SUCCESS = 0
SELFTEST_DEPENDENCY_FAILURE = 1


def run_selftest(argv: list[str]) -> int:
    """Qt依存と書き込み先を確認し、固定終了コードを返す。"""
    try:
        log_path = logging_setup.configure_logging()
        logging_setup.install_excepthook()
        _logger.info("sdp selftestを開始します: version=%s", __version__)
        application = create_application(argv)
        _check_qt_dependencies(application)
        _check_writable_directory(app_data_directory(), "ユーザーdata保存先")
        _check_writable_directory(_temporary_directory(), "単一instance用temp directory")
        _logger.info("sdp selftestに成功しました: version=%s log=%s", __version__, log_path)
    except Exception:
        _logger.exception("sdp selftestに失敗しました")
        return SELFTEST_DEPENDENCY_FAILURE
    return SELFTEST_SUCCESS


def _temporary_directory() -> Path:
    """Qtが選んだ一時directoryを返す。"""
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
    if not location:
        raise OSError("単一instance用temp directoryを取得できません")
    return Path(location)


def _check_qt_dependencies(application: QApplication) -> None:
    """Qt Widgets・Network・Multimediaの必須objectを構築する。"""
    player = QMediaPlayer()
    audio_output = QAudioOutput()
    buffer_output = QAudioBufferOutput()
    decoder = QAudioDecoder()
    socket = QLocalSocket()
    player.setAudioOutput(audio_output)
    player.setAudioBufferOutput(buffer_output)
    socket.abort()
    for value in (player, audio_output, buffer_output, decoder, socket):
        value.deleteLater()
    application.processEvents()


def _check_writable_directory(directory: Path, label: str) -> None:
    """対象内へ一時ファイルを作成・削除できることを確認する。"""
    directory.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".sdp-selftest-",
            dir=directory,
            delete=False,
        ) as stream:
            stream.write(b"sdp selftest\n")
            path = Path(stream.name)
        path.unlink()
        path = None
    except OSError as error:
        raise OSError(f"{label}へ書き込めません: {directory}") from error
    finally:
        if path is not None:
            path.unlink(missing_ok=True)

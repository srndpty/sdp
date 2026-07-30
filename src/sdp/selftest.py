"""配布版からGUIを表示せず実行する軽量な自己診断。"""

import logging
import os
import tempfile
import wave
from pathlib import Path

from PySide6.QtCore import QEventLoop, QStandardPaths, QTimer, QUrl
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
        _check_writable_directory(app_data_directory(), "ユーザーdata保存先")
        temporary_directory = _temporary_directory()
        _check_writable_directory(temporary_directory, "単一instance用temp directory")
        _check_multimedia_decode(temporary_directory)
        _check_qt_dependencies(application)
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


def _check_multimedia_decode(directory: Path) -> None:
    """FFmpeg backendで一時WAVを実decodeし、pluginと依存DLLを検査する。"""
    source = _create_silent_wav(directory)
    previous_backend = os.environ.get("QT_MEDIA_BACKEND")
    decoder: QAudioDecoder | None = None
    timeout: QTimer | None = None

    try:
        os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"
        event_loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        decoder = QAudioDecoder()
        decoded_buffer_count = 0
        decode_errors: list[QAudioDecoder.Error] = []

        def read_buffer() -> None:
            nonlocal decoded_buffer_count
            assert decoder is not None
            buffer = decoder.read()
            if buffer.isValid():
                decoded_buffer_count += 1

        def record_error(error: QAudioDecoder.Error) -> None:
            decode_errors.append(error)
            event_loop.quit()

        decoder.bufferReady.connect(read_buffer)
        decoder.finished.connect(event_loop.quit)
        decoder.error.connect(record_error)
        timeout.timeout.connect(event_loop.quit)
        decoder.setSource(QUrl.fromLocalFile(str(source)))
        timeout.start(5_000)
        decoder.start()
        event_loop.exec()
        timeout.stop()

        if decode_errors:
            raise RuntimeError(f"Qt Multimedia decodeに失敗しました: {decoder.errorString()}")
        if decoded_buffer_count == 0:
            raise RuntimeError("Qt Multimedia backendからPCM bufferを取得できませんでした")
        _logger.info("Qt Multimedia FFmpeg backendのWAV decodeに成功しました")
    finally:
        if timeout is not None:
            timeout.stop()
        if decoder is not None:
            decoder.stop()
            decoder.deleteLater()
        source.unlink(missing_ok=True)
        if previous_backend is None:
            os.environ.pop("QT_MEDIA_BACKEND", None)
        else:
            os.environ["QT_MEDIA_BACKEND"] = previous_backend


def _create_silent_wav(directory: Path) -> Path:
    """selftest専用の短いPCM WAVを作る。呼び出し側が必ず削除する。"""
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".sdp-selftest-audio-",
        suffix=".wav",
        dir=directory,
    )
    os.close(descriptor)
    path = Path(raw_path)
    try:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8_000)
            stream.writeframes(b"\x00\x00" * 800)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


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

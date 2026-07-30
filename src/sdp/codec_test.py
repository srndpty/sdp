"""配布版が実際にPCMへdecodeできるかを、GUIも音も出さずに検査する。

``--selftest`` が「Qt依存と書き込み先が揃っているか」を見るのに対し、こちらは
**指定された音源を実際にdecodeできるか**だけを見る。判定は次を満たしたときだけ成功。

- source設定に成功する
- :class:`QAudioDecoder` のerrorが発生しない
- 期待時間内にfinishedへ到達する
- 有効なPCM bufferを1件以上受け取り、frame数・sample rate・channel数が正

metadataを読めただけでは成功にしない（pluginやDLLが欠けていても、形式によっては
metadataだけ読めることがあるため）。

契約:

- Windowを表示しない・音を鳴らさない・単一instance IPCを開始しない
- settings／playlist／ui-stateとwaveform cacheを作らない
- 一時ファイルを作る場合はtemp directoryだけを使い、正常終了時に削除する
- 検査対象は呼び出し側が渡す（製品配布物へ検査用音源を同梱しない）
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtMultimedia import QAudioDecoder

from sdp import __version__
from sdp.app import create_application
from sdp.services import logging_setup

_logger = logging.getLogger(__name__)

CODEC_TEST_SUCCESS = 0
CODEC_TEST_FAILURE = 1
DEFAULT_TIMEOUT_MS = 15_000


@dataclass(frozen=True, slots=True)
class CodecTestResult:
    """1ファイル分のdecode結果（表示と集計のためだけに持つ）。"""

    path: Path
    succeeded: bool
    buffer_count: int = 0
    frame_count: int = 0
    sample_rate: int = 0
    channel_count: int = 0
    failure_reason: str | None = None

    def summary(self) -> str:
        if self.succeeded:
            return (
                f"OK {self.path.name}: buffers={self.buffer_count} frames={self.frame_count} "
                f"{self.sample_rate}Hz {self.channel_count}ch"
            )
        return f"NG {self.path.name}: {self.failure_reason}"


def run_codec_test(argv: list[str], targets: Sequence[str]) -> int:
    """指定された全ファイルをdecodeし、1件でも失敗したら ``1`` を返す。

    一部が失敗しても残りを必ず試し、形式ごとの可否を1回の実行で把握できるようにする。
    """
    try:
        logging_setup.configure_logging()
        logging_setup.install_excepthook()
        _logger.info("sdp codec testを開始します: version=%s 件数=%d", __version__, len(targets))
        application = create_application(argv)
        results = [decode_file(Path(target)) for target in targets]
        _log_results(results)
        application.processEvents()
    except Exception:
        _logger.exception("sdp codec testに失敗しました")
        return CODEC_TEST_FAILURE
    return CODEC_TEST_SUCCESS if all(result.succeeded for result in results) else CODEC_TEST_FAILURE


def decode_file(path: Path, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> CodecTestResult:
    """1ファイルをdecodeし、PCMを取り出せたかを返す（例外は結果へ畳む）。"""
    if not path.is_file():
        return CodecTestResult(path, succeeded=False, failure_reason="ファイルがありません")

    decoder = QAudioDecoder()
    timer = QTimer()
    timer.setSingleShot(True)
    event_loop = QEventLoop()
    buffer_count = 0
    frame_count = 0
    sample_rate = 0
    channel_count = 0
    errors: list[str] = []
    finished = False
    timed_out = False

    def read_buffer() -> None:
        nonlocal buffer_count, frame_count, sample_rate, channel_count
        buffer = decoder.read()
        if not buffer.isValid():
            return
        audio_format = buffer.format()
        buffer_count += 1
        frame_count += buffer.frameCount()
        sample_rate = max(sample_rate, audio_format.sampleRate())
        channel_count = max(channel_count, audio_format.channelCount())

    def record_error(error: QAudioDecoder.Error) -> None:
        del error
        errors.append(decoder.errorString() or "不明なdecodeエラー")
        event_loop.quit()

    def record_finished() -> None:
        nonlocal finished
        finished = True
        event_loop.quit()

    def record_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        event_loop.quit()

    decoder.bufferReady.connect(read_buffer)
    decoder.finished.connect(record_finished)
    decoder.error.connect(record_error)
    timer.timeout.connect(record_timeout)
    try:
        decoder.setSource(QUrl.fromLocalFile(str(path.resolve())))
        timer.start(timeout_ms)
        decoder.start()
        event_loop.exec()
    finally:
        timer.stop()
        decoder.stop()
        decoder.deleteLater()
        timer.deleteLater()

    reason = _failure_reason(
        errors=errors,
        timed_out=timed_out,
        finished=finished,
        buffer_count=buffer_count,
        frame_count=frame_count,
        sample_rate=sample_rate,
        channel_count=channel_count,
    )
    return CodecTestResult(
        path=path,
        succeeded=reason is None,
        buffer_count=buffer_count,
        frame_count=frame_count,
        sample_rate=sample_rate,
        channel_count=channel_count,
        failure_reason=reason,
    )


def _failure_reason(
    *,
    errors: Sequence[str],
    timed_out: bool,
    finished: bool,
    buffer_count: int,
    frame_count: int,
    sample_rate: int,
    channel_count: int,
) -> str | None:
    """decodeを成功とみなせない理由を返す（成功なら ``None``）。"""
    if errors:
        return f"decodeエラー: {errors[0]}"
    if timed_out:
        return "decodeが時間内に終わりませんでした"
    if not finished:
        return "decodeが完了しませんでした"
    if buffer_count == 0:
        return "PCM bufferを1件も取得できませんでした"
    if frame_count <= 0:
        return "PCM bufferのframe数が0です"
    if sample_rate <= 0:
        return "sample rateが不正です"
    if channel_count <= 0:
        return "channel countが不正です"
    return None


def _log_results(results: Sequence[CodecTestResult]) -> None:
    for result in results:
        if result.succeeded:
            _logger.info("%s", result.summary())
        else:
            _logger.error("%s", result.summary())
    failed = [result for result in results if not result.succeeded]
    _logger.info("sdp codec test: 成功%d件 / 失敗%d件", len(results) - len(failed), len(failed))

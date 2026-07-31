"""専用QThread上のQAudioDecoderによる波形解析サービス。"""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QElapsedTimer, QObject, QThread, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioDecoder

from sdp.core.analysis.pcm import audio_buffer_to_mono
from sdp.core.analysis.waveform import WAVEFORM_BUCKET_MS, WaveformData, WaveformReducer
from sdp.core.analysis.waveform_cache import (
    MAX_WAVEFORM_CACHE_BYTES,
    WaveformCache,
    WaveformCacheKey,
)
from sdp.core.playback.controller import PlaybackController
from sdp.services.thread_shutdown import (
    DEFAULT_HARD_TIMEOUT_MS,
    ShutdownOutcome,
    stop_thread,
)

_logger = logging.getLogger(__name__)

PARTIAL_BUCKET_INTERVAL = 1_024
PARTIAL_TIME_INTERVAL_MS = 250
SHUTDOWN_TIMEOUT_MS = 3_000
FILE_CHANGED_MESSAGE = "解析中に音声ファイルが変更されました。"


@dataclass(frozen=True, slots=True)
class WaveformRequest:
    """1回の解析要求。

    キャッシュキーはここへ持たない。内容fingerprintの算出は最大192KiBの読み取りを
    伴うため、GUIスレッドで作るとNAS・休止ディスク・クラウドプレースホルダーで
    source切替のたびにUIが止まる。キーはworker threadで作る。
    """

    path: Path
    token: int


@dataclass(frozen=True, slots=True)
class DecodedChunk:
    """テスト用decode境界がworkerへ逐次渡すmono PCM。"""

    samples: NDArray[np.float32]
    sample_rate: int


DecodeFunction = Callable[[Path, Callable[[], bool]], Iterable[DecodedChunk]]


class _CancellationRegistry:
    """GUI threadからworkerへ即時に伝わるtoken単位の論理キャンセル。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled: set[int] = set()

    def cancel(self, token: int) -> None:
        with self._lock:
            self._cancelled.add(token)

    def is_cancelled(self, token: int) -> bool:
        with self._lock:
            return token in self._cancelled

    def discard(self, token: int) -> None:
        with self._lock:
            self._cancelled.discard(token)

    def count(self) -> int:
        """テストと診断向けに未回収token数を返す。"""
        with self._lock:
            return len(self._cancelled)


class _WaveformWorker(QObject):
    """QAudioDecoder、縮約、cache I/Oを専用thread内だけで実行する。"""

    started = Signal(object)
    partial = Signal(object, object)
    finished = Signal(object, object, bool)
    failed = Signal(object, str)
    decoder_created = Signal(bool)

    def __init__(
        self,
        cache: WaveformCache,
        cancellations: _CancellationRegistry,
        decode_function: DecodeFunction | None,
    ) -> None:
        super().__init__()
        self._cache = cache
        self._cancellations = cancellations
        self._decode_function = decode_function
        self._request: WaveformRequest | None = None
        self._key: WaveformCacheKey | None = None
        self._decoder: QAudioDecoder | None = None
        self._reducer: WaveformReducer | None = None
        self._sample_rate: int | None = None
        self._last_partial_bucket = 0
        self._partial_timer = QElapsedTimer()
        self._terminal = False

    @Slot(object)
    def analyze(self, value: object) -> None:
        if not isinstance(value, WaveformRequest):
            return
        request = value
        self._cleanup_decoder()
        self._request = request
        self._key = None
        self._reducer = None
        self._sample_rate = None
        self._last_partial_bucket = 0
        self._terminal = False
        self._partial_timer.start()
        if self._cancelled(request):
            return
        self.started.emit(request)

        # fingerprintを含む完全なキーはここ（worker thread）で作る。
        try:
            key = WaveformCacheKey.from_path(request.path)
        except OSError as error:
            self._fail(request, str(error))
            return
        self._key = key
        cached = self._cache.load(key)
        if self._cancelled(request):
            return
        if cached is not None:
            # cache hitの直前にもう一度だけ完全一致を確認する。
            if not _key_still_matches(request.path, key):
                self._fail(request, FILE_CHANGED_MESSAGE)
                return
            self._terminal = True
            self.finished.emit(request, cached, True)
            return
        if self._decode_function is not None:
            self._analyze_with_function(request)
            return
        self._start_qt_decoder(request)

    @Slot(int)
    def cancel(self, token: int) -> None:
        request = self._request
        if request is not None and request.token == token:
            if self._decoder is not None:
                self._decoder.stop()
            self._cleanup_decoder()
        # cancelより前にqueueされた旧cache保存は既にcancel状態を確認済み。
        self._cancellations.discard(token)

    @Slot(object, object)
    def save_cache(self, request_value: object, data_value: object) -> None:
        if not isinstance(request_value, WaveformRequest) or not isinstance(
            data_value, WaveformData
        ):
            return
        key = self._key
        current = self._request
        if key is None or current is None or current.token != request_value.token:
            return
        if self._cancelled(request_value):
            return
        # 保存前だけは完全なfingerprintで確認する（解析中の安価な確認では
        # 内容差し替えを見逃しうるため、古い波形を書き込まない）。
        if not _key_still_matches(request_value.path, key):
            return
        try:
            self._cache.save(key, data_value)
        except (OSError, ValueError):
            # cache失敗は解析結果・再生を失敗扱いにしない。
            _logger.exception("波形キャッシュを保存できません: %s", request_value.path)

    @Slot()
    def shutdown(self) -> None:
        if self._decoder is not None:
            self._decoder.stop()
        self._cleanup_decoder()

    def _analyze_with_function(self, request: WaveformRequest) -> None:
        assert self._decode_function is not None
        try:
            for chunk in self._decode_function(request.path, lambda: self._cancelled(request)):
                if self._cancelled(request):
                    return
                self._append_mono(request, chunk.samples, chunk.sample_rate)
                if self._terminal or self._cancelled(request):
                    return
            self._finish(request)
        except (OSError, ValueError) as error:
            self._fail(request, str(error))
        except Exception:
            _logger.exception("波形解析で予期しない例外: %s", request.path)
            self._fail(request, "波形を解析できませんでした。")

    def _start_qt_decoder(self, request: WaveformRequest) -> None:
        if self._cancelled(request):
            return
        decoder = QAudioDecoder(self)
        self._decoder = decoder
        decoder.bufferReady.connect(self._on_buffer_ready)
        decoder.finished.connect(self._on_decoder_finished)
        decoder.error.connect(self._on_decoder_error)
        decoder.setSource(QUrl.fromLocalFile(str(request.path)))
        self.decoder_created.emit(decoder.thread() is QThread.currentThread())
        decoder.start()

    @Slot()
    def _on_buffer_ready(self) -> None:
        request = self._request
        decoder = self._decoder
        if request is None or decoder is None or self._terminal:
            return
        if self._cancelled(request):
            decoder.stop()
            self._cleanup_decoder()
            return
        buffer = decoder.read()
        try:
            samples, sample_rate = audio_buffer_to_mono(buffer)
            self._append_mono(request, samples, sample_rate)
        except ValueError as error:
            self._fail(request, str(error))
        if self._terminal:
            return
        if self._cancelled(request):
            decoder.stop()
            self._cleanup_decoder()

    @Slot()
    def _on_decoder_finished(self) -> None:
        request = self._request
        if request is not None:
            self._finish(request)

    @Slot()
    def _on_decoder_error(self) -> None:
        request = self._request
        decoder = self._decoder
        if request is not None and decoder is not None:
            detail = decoder.errorString() or "QAudioDecoderで不明なエラーが発生しました"
            self._fail(request, detail)

    def _append_mono(
        self,
        request: WaveformRequest,
        samples: NDArray[np.float32],
        sample_rate: int,
    ) -> None:
        if self._reducer is None:
            key = self._key
            bucket_ms = WAVEFORM_BUCKET_MS if key is None else key.bucket_ms
            self._reducer = WaveformReducer(sample_rate, bucket_ms)
            self._sample_rate = sample_rate
        elif sample_rate != self._sample_rate:
            raise ValueError("デコード途中でsample rateが変更されました")
        self._reducer.append(samples)
        count = self._reducer.bucket_count
        should_emit = count > 0 and (
            self._last_partial_bucket == 0
            or count - self._last_partial_bucket >= PARTIAL_BUCKET_INTERVAL
            or self._partial_timer.elapsed() >= PARTIAL_TIME_INTERVAL_MS
        )
        if should_emit and not self._cancelled(request):
            if not self._identity_unchanged(request):
                self._fail(request, FILE_CHANGED_MESSAGE)
                return
            self.partial.emit(request, self._reducer.snapshot(complete=False))
            self._last_partial_bucket = count
            self._partial_timer.restart()

    def _identity_unchanged(self, request: WaveformRequest) -> bool:
        """解析中のこまめな確認（size と mtime だけ。内容は読まない）。

        partialのたびに最大192KiBを読み直すと、長時間音源でランダムI/Oが
        繰り返される。差し替えの取りこぼしは、完了時と保存前の完全な
        fingerprint確認で塞ぐ。
        """
        key = self._key
        if key is None:
            return False
        try:
            stat = request.path.stat()
        except OSError:
            return False
        return stat.st_size == key.size and stat.st_mtime_ns == key.mtime_ns

    def _finish(self, request: WaveformRequest) -> None:
        if self._terminal or self._cancelled(request):
            return
        key = self._key
        # 完了時だけは完全なfingerprintで確認する。
        if key is None or not _key_still_matches(request.path, key):
            self._fail(request, FILE_CHANGED_MESSAGE)
            return
        self._terminal = True
        reducer = self._reducer
        if reducer is None:
            # decoderがbufferを一度も返さない正常完了も有効な空結果とする。
            reducer = WaveformReducer(1_000, key.bucket_ms)
        data = reducer.snapshot(complete=True)
        if not self._cancelled(request):
            self.finished.emit(request, data, False)
        self._cleanup_decoder()

    def _fail(self, request: WaveformRequest, message: str) -> None:
        if self._terminal or self._cancelled(request):
            return
        self._terminal = True
        if self._decoder is not None:
            self._decoder.stop()
        self.failed.emit(request, message)
        self._cleanup_decoder()

    def _cancelled(self, request: WaveformRequest) -> bool:
        return self._cancellations.is_cancelled(request.token)

    def _cleanup_decoder(self) -> None:
        decoder = self._decoder
        self._decoder = None
        if decoder is not None:
            decoder.disconnect(self)
            decoder.deleteLater()


class WaveformAnalysisService(QObject):
    """現在sourceだけの解析結果をGUI threadへ公開する。"""

    analysis_started = Signal(object, int)
    partial_ready = Signal(object, int, object)
    analysis_finished = Signal(object, int, object, bool)
    analysis_failed = Signal(object, int, str)
    analysis_cleared = Signal()

    _analyze_requested = Signal(object)
    _cancel_requested = Signal(int)
    _cache_save_requested = Signal(object, object)
    _shutdown_requested = Signal()

    def __init__(
        self,
        playback: PlaybackController,
        cache_directory: Path,
        *,
        decode_function: DecodeFunction | None = None,
        max_cache_bytes: int = MAX_WAVEFORM_CACHE_BYTES,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._thread = QThread(self)
        self._thread.setObjectName("waveformAnalysisThread")
        self._cancellations = _CancellationRegistry()
        self._worker = _WaveformWorker(
            WaveformCache(cache_directory, max_bytes=max_cache_bytes),
            self._cancellations,
            decode_function,
        )
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._analyze_requested.connect(self._worker.analyze)
        self._cancel_requested.connect(self._worker.cancel)
        self._cache_save_requested.connect(self._worker.save_cache)
        self._shutdown_requested.connect(self._worker.shutdown)
        self._worker.started.connect(self._on_worker_started)
        self._worker.partial.connect(self._on_worker_partial)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._started = False
        self._shutdown = False
        self._shutdown_outcome: ShutdownOutcome | None = None
        self._next_token = 0
        self._request: WaveformRequest | None = None

    @property
    def is_running(self) -> bool:
        return self._started and not self._shutdown and self._thread.isRunning()

    def start(self) -> None:
        """source監視とworker threadを開始する（冪等）。"""
        if self._started or self._shutdown:
            return
        self._started = True
        self._playback.source_changed.connect(self._on_source_changed)
        self._thread.start()
        self._on_source_changed(self._playback.source)

    def shutdown(
        self,
        timeout_ms: int = SHUTDOWN_TIMEOUT_MS,
        *,
        hard_timeout_ms: int = DEFAULT_HARD_TIMEOUT_MS,
    ) -> ShutdownOutcome:
        """新規要求を禁止し、decoder停止を要求してthread終了を待つ。

        待機は上限つきで、**超えても無期限待機へ移らない**（「閉じるを押したのに
        プロセスが残る」状態を作らない）。上限を超えた場合はthreadとworkerを
        放棄して ``ABANDONED`` を返す。terminateは使わない。

        冪等だが、**初回の結果を書き換えない**。2回目以降は初回の結果を返し、
        放棄したthreadが実際に終了していた場合だけ ``STOPPED`` へ更新する。
        """
        if self._shutdown:
            return self._recheck_shutdown_outcome()
        self._shutdown = True
        if self._started:
            self._playback.source_changed.disconnect(self._on_source_changed)
        self._invalidate_current()
        self._shutdown_requested.emit()
        self._thread.requestInterruption()
        self._thread.quit()
        # workerはthread内でまだ動きうる。放棄する場合はPython参照ごと引き取らせる
        # （self自身も、workerのSignal接続先として生かしておく必要がある）。
        outcome = stop_thread(
            self._thread,
            label="波形解析thread",
            soft_timeout_ms=timeout_ms,
            hard_timeout_ms=hard_timeout_ms,
            keepalive=(self._worker, self),
        )
        self._shutdown_outcome = outcome
        return outcome

    def _recheck_shutdown_outcome(self) -> ShutdownOutcome:
        """初回の結果を返す。放棄後に終了していれば STOPPED へ更新する。"""
        if self._shutdown_outcome is ShutdownOutcome.ABANDONED and self._thread.wait(0):
            self._shutdown_outcome = ShutdownOutcome.STOPPED
        return ShutdownOutcome.STOPPED if self._shutdown_outcome is None else self._shutdown_outcome

    @Slot(object)
    def _on_source_changed(self, source: object) -> None:
        if self._shutdown or not self._started:
            return
        self._invalidate_current()
        # Bのworker処理を待たず、source変更時点でAの表示を解除できる契約。
        self.analysis_cleared.emit()
        if source is None:
            return
        if not isinstance(source, Path):
            return
        self._next_token += 1
        token = self._next_token
        # GUIスレッドではファイルシステムへ触れない。source は PlaybackController が
        # 既に strict resolve 済みのパス（source_changed が渡す値）なので、ここで
        # 再度 resolve(strict=True) すると NAS・休止ディスク・クラウドプレースホルダー
        # でUIを止めるだけの重複I/Oになる。strict resolve・stat・内容fingerprint・
        # cache lookup はすべて worker thread（analyze / from_path）へ任せる。
        # 解析直前に消えていた等の失敗も worker が analysis_failed で終端する。
        request = WaveformRequest(path=source, token=token)
        self._request = request
        self._cancellations.discard(request.token)
        self._analyze_requested.emit(request)

    def _invalidate_current(self) -> None:
        request = self._request
        self._request = None
        if request is not None:
            self._cancellations.cancel(request.token)
            self._cancel_requested.emit(request.token)

    @Slot(object)
    def _on_worker_started(self, value: object) -> None:
        request = self._applicable_request(value)
        if request is not None:
            self.analysis_started.emit(request.path, request.token)

    @Slot(object, object)
    def _on_worker_partial(self, request_value: object, data_value: object) -> None:
        request = self._applicable_request(request_value)
        if request is not None and isinstance(data_value, WaveformData):
            self.partial_ready.emit(request.path, request.token, data_value)

    @Slot(object, object, bool)
    def _on_worker_finished(
        self, request_value: object, data_value: object, from_cache: bool
    ) -> None:
        request = self._applicable_request(request_value)
        if request is None or not isinstance(data_value, WaveformData):
            return
        if not from_cache:
            self._cache_save_requested.emit(request, data_value)
        self.analysis_finished.emit(request.path, request.token, data_value, from_cache)

    @Slot(object, str)
    def _on_worker_failed(self, request_value: object, message: str) -> None:
        request = self._applicable_request(request_value)
        if request is not None:
            _logger.warning("波形解析に失敗しました: %s (%s)", request.path, message)
            self.analysis_failed.emit(request.path, request.token, message)
            if self._request == request:
                self._request = None

    def _applicable_request(self, value: object) -> WaveformRequest | None:
        if self._shutdown or not isinstance(value, WaveformRequest):
            return None
        current = self._request
        if current is None or value.token != current.token or value.path != current.path:
            return None
        if self._playback.source != value.path:
            return None
        return value


def _key_still_matches(path: Path, key: WaveformCacheKey) -> bool:
    """内容fingerprintまで含めて同じファイルかを確かめる（読み取りを伴う）。

    cache hitの適用直前、解析完了時、cache保存前だけで使う。解析中の
    こまめな確認には :meth:`_WaveformWorker._identity_unchanged` を使う。
    """
    try:
        return WaveformCacheKey.from_path(path) == key
    except OSError:
        return False

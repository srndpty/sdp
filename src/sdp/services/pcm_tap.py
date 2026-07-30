"""再生中PCMを QAudioBufferOutput から受け取り、リングバッファへ書き込むタップ。

Qt Multimedia 固有の補助ポートであり、``PlaybackBackend`` の一般インターフェース
ではない。接続するのは composition root だけで、UI 層は
:class:`~sdp.core.playback.qt_backend.QtMultimediaBackend` を参照しない。

音声コールバック（``audioBufferReceived``。P0-C と P5-A の probe でともに
**GUI スレッド受信**を実測）で行うのは、buffer の妥当性確認・bytes 化・NumPy に
よる正規化とmono／L／R派生・リングバッファ追記・軽量なシグナルだけとする。
FFT、Peak／RMS、dB変換、Peak hold、描画、ファイル I/O、Model / Controller 操作は
行わない。
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtMultimedia import QAudioBuffer, QAudioBufferOutput

from sdp.core.analysis.pcm import audio_buffer_to_pcm_chunk
from sdp.core.analysis.ring_buffer import (
    DEFAULT_PCM_SAMPLE_RATE,
    PcmRingBuffer,
    pcm_ring_capacity,
)
from sdp.core.analysis.spectrum import FFT_SIZE
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState

_logger = logging.getLogger(__name__)

_DISCARD_LOG_INTERVAL = 100
"""無効bufferのログ間隔。音声コールバックからログを大量に出さないため。"""


@dataclass(frozen=True, slots=True)
class VisualizationPcmSnapshot:
    """同一時点のformatとmono／L／R PCMをまとめた可視化用snapshot。"""

    mono: NDArray[np.float32]
    left: NDArray[np.float32]
    right: NDArray[np.float32]
    sample_rate: int
    channel_count: int


class PcmTap(QObject):
    """QAudioBuffer を mono／左／右の float32 へ変換してリングバッファへ流すアダプター。

    1 QAudioBuffer につき bytes 化は 1 回だけで、mono とL／Rは同じ bytes から
    派生させる（:class:`~sdp.core.analysis.pcm.PcmChunk`）。

    持たないもの: FFT、Peak／RMS、dB変換、Peak hold、Hann窓、QTimer、QWidget、
    QPainter、PlaylistModel、キャッシュ、設定、波形解析、シーク。
    """

    sample_rate_changed = Signal(int)
    """PCMのsample rateが変化した（未確定・解除時は0）。"""

    channel_count_changed = Signal(int)
    """PCMのchannel countが変化した（未確定・解除時は0）。"""

    def __init__(
        self,
        playback: PlaybackController,
        ring_buffer: PcmRingBuffer | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._mono_buffer = (
            PcmRingBuffer(pcm_ring_capacity(DEFAULT_PCM_SAMPLE_RATE, FFT_SIZE))
            if ring_buffer is None
            else ring_buffer
        )
        # L／R も mono と同じ固定容量にする（48kHz×2秒×float32で3本合計約1.1MB）。
        capacity = self._mono_buffer.capacity
        self._left_buffer = PcmRingBuffer(capacity)
        self._right_buffer = PcmRingBuffer(capacity)
        self._sample_rate = 0
        self._channel_count = 0
        self._received_count = 0
        self._discarded_count = 0
        self._unexpected_error_count = 0
        self._buffer_output: QAudioBufferOutput | None = None
        self._shutdown = False
        # formatと3本のリングバッファを、可視化から見て1つの世代として扱う。
        self._snapshot_lock = threading.RLock()

        playback.source_changed.connect(self._on_source_changed)
        playback.state_changed.connect(self._on_state_changed)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def ring_buffer(self) -> PcmRingBuffer:
        """monoリングバッファ。同一バッファを共有していることの確認用。"""
        return self._mono_buffer

    @property
    def left_ring_buffer(self) -> PcmRingBuffer:
        return self._left_buffer

    @property
    def right_ring_buffer(self) -> PcmRingBuffer:
        return self._right_buffer

    @property
    def sample_rate(self) -> int:
        """最新PCMのsample rate。まだ届いていなければ0。"""
        with self._snapshot_lock:
            return self._sample_rate

    @property
    def channel_count(self) -> int:
        """最新PCMのchannel count。まだ届いていなければ0。"""
        with self._snapshot_lock:
            return self._channel_count

    @property
    def available_frame_count(self) -> int:
        with self._snapshot_lock:
            return self._mono_buffer.available

    @property
    def received_buffer_count(self) -> int:
        return self._received_count

    @property
    def discarded_buffer_count(self) -> int:
        return self._discarded_count

    def snapshot(self, frame_count: int) -> NDArray[np.float32]:
        """monoの最新PCMのread-onlyコピーを返す（左0 padding）。"""
        return self._mono_buffer.snapshot(frame_count)

    def snapshot_mono(self, frame_count: int) -> NDArray[np.float32]:
        """:meth:`snapshot` と同じ。呼び出し側の意図を明示するための別名。"""
        return self._mono_buffer.snapshot(frame_count)

    def snapshot_stereo(self, frame_count: int) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        """左右の最新PCMのread-onlyコピーを返す（左0 padding）。

        mono音源では左右が同じ値になる（PCM変換時に複製している）。
        """
        with self._snapshot_lock:
            return self._left_buffer.snapshot(frame_count), self._right_buffer.snapshot(frame_count)

    def snapshot_visualization(
        self,
        *,
        mono_frames: int,
        level_frames: int,
    ) -> VisualizationPcmSnapshot:
        """formatとmono／L／Rを同一世代から取得する。

        コピー完了後にlockを解放するため、FFT・Peak／RMS・描画中はPCM受信を
        妨げない。各配列はリングバッファの契約どおり独立したread-onlyコピー。
        """
        with self._snapshot_lock:
            return VisualizationPcmSnapshot(
                mono=self._mono_buffer.snapshot(mono_frames),
                left=self._left_buffer.snapshot(level_frames),
                right=self._right_buffer.snapshot(level_frames),
                sample_rate=self._sample_rate,
                channel_count=self._channel_count,
            )

    # -- 接続 ---------------------------------------------------------------

    def connect_audio_buffer_output(self, buffer_output: QAudioBufferOutput) -> None:
        """QAudioBufferOutput のPCM通知を受け取り始める（composition rootのみ）。"""
        if self._shutdown:
            raise RuntimeError("shutdown後のPcmTapは再接続できません")
        if self._buffer_output is buffer_output:
            return
        self.disconnect_audio_buffer_output()
        self._buffer_output = buffer_output
        buffer_output.audioBufferReceived.connect(self.handle_audio_buffer)

    def disconnect_audio_buffer_output(self) -> None:
        buffer_output = self._buffer_output
        self._buffer_output = None
        if buffer_output is None:
            return
        try:
            buffer_output.audioBufferReceived.disconnect(self.handle_audio_buffer)
        except RuntimeError:
            # QAudioBufferOutput が既に破棄済み。終了処理を妨げない。
            _logger.debug("QAudioBufferOutputの接続は既に解除されています")

    def shutdown(self) -> None:
        """PCM受信とsource監視を止め、保持中のPCMを捨てる（冪等）。"""
        if self._shutdown:
            return
        # disconnect前に終端化し、既にqueueされたbufferも受理しない。
        self._shutdown = True
        self.disconnect_audio_buffer_output()
        for signal, slot in (
            (self._playback.source_changed, self._on_source_changed),
            (self._playback.state_changed, self._on_state_changed),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                # Controllerが既に破棄済み。終了処理を妨げない。
                _logger.debug("PcmTapのController接続は既に解除されています")
        self.clear()

    def clear(self) -> None:
        """3本のリングバッファとformat状態を解除する。"""
        with self._snapshot_lock:
            for buffer in (self._mono_buffer, self._left_buffer, self._right_buffer):
                buffer.clear()
            sample_rate_changed, channel_count_changed = self._replace_format(0, 0)
            self._emit_format_changes(sample_rate_changed, channel_count_changed, 0, 0)

    # -- Qt シグナル --------------------------------------------------------

    @Slot(object)
    def handle_audio_buffer(self, value: object) -> None:
        """音声コールバック。例外を外へ漏らさず、無効bufferは数えて捨てる。

        ``QAudioBufferOutput.audioBufferReceived`` の接続先であり、
        QAudioBufferOutput を用意せずにPCMを注入できる境界として公開する。

        PySide6 はスロット内の例外を呼び出し元へ伝播させずに処理を継続するため
        （P0-C）、ここで観測可能な失敗（件数とログ）へ変換する。
        QAudioBuffer とその memory view はこのスロットの外へ持ち出さない。
        """
        if self._shutdown:
            return
        try:
            if not isinstance(value, QAudioBuffer):
                self._discard("QAudioBuffer以外が通知されました")
                return
            chunk = audio_buffer_to_pcm_chunk(value)
            with self._snapshot_lock:
                # shutdownと変換が競合した場合も、変換済みPCMを終端後に追記しない。
                if self._shutdown:
                    return
                if (
                    chunk.sample_rate != self._sample_rate
                    or chunk.channel_count != self._channel_count
                ):
                    # 旧formatのサンプルを混ぜないため、3本とも容量ごと作り直す。
                    capacity = pcm_ring_capacity(chunk.sample_rate, FFT_SIZE)
                    for buffer in (self._mono_buffer, self._left_buffer, self._right_buffer):
                        buffer.set_capacity(capacity)
                sample_rate_changed, channel_count_changed = self._replace_format(
                    chunk.sample_rate, chunk.channel_count
                )
                self._mono_buffer.append(chunk.mono)
                self._left_buffer.append(chunk.left)
                self._right_buffer.append(chunk.right)
                self._received_count += 1
                self._emit_format_changes(
                    sample_rate_changed,
                    channel_count_changed,
                    chunk.sample_rate,
                    chunk.channel_count,
                )
        except ValueError as error:
            self._discard(str(error))
        except Exception:
            self._discarded_count += 1
            self._unexpected_error_count += 1
            if self._unexpected_error_count == 1:
                _logger.exception("PCMタップで予期しない例外が発生")
            elif self._unexpected_error_count % _DISCARD_LOG_INTERVAL == 0:
                _logger.warning(
                    "PCMタップの予期しない例外が累計%d件発生",
                    self._unexpected_error_count,
                )

    @Slot(object)
    def _on_source_changed(self, source: object) -> None:
        del source
        if self._shutdown:
            return
        # 新sourceの最初のPCMが届くまで、前sourceのPCMを残さない。
        self.clear()

    @Slot(PlaybackState)
    def _on_state_changed(self, state: PlaybackState) -> None:
        if self._shutdown:
            return
        # PAUSED では保持する（最後のフレームを静止表示できるようにする）。
        if state in (PlaybackState.STOPPED, PlaybackState.NO_MEDIA):
            self.clear()

    # -- 内部 ---------------------------------------------------------------

    def _replace_format(self, sample_rate: int, channel_count: int) -> tuple[bool, bool]:
        """lock内でformatを置換し、各値が変化したかを返す。"""
        sample_rate_changed = sample_rate != self._sample_rate
        channel_count_changed = channel_count != self._channel_count
        self._sample_rate = sample_rate
        self._channel_count = channel_count
        return sample_rate_changed, channel_count_changed

    def _emit_format_changes(
        self,
        sample_rate_changed: bool,
        channel_count_changed: bool,
        sample_rate: int,
        channel_count: int,
    ) -> None:
        """formatと3本の更新後に通知する（同期slotからのsnapshot再入も可能）。"""
        if sample_rate_changed:
            self.sample_rate_changed.emit(sample_rate)
        if channel_count_changed:
            self.channel_count_changed.emit(channel_count)

    def _discard(self, reason: str) -> None:
        self._discarded_count += 1
        if self._discarded_count % _DISCARD_LOG_INTERVAL == 1:
            source: Path | None = self._playback.source
            _logger.debug(
                "PCM bufferを破棄しました（累計%d件）: %s / source=%s",
                self._discarded_count,
                reason,
                source,
            )

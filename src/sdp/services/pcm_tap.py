"""再生中PCMを QAudioBufferOutput から受け取り、リングバッファへ書き込むタップ。

Qt Multimedia 固有の補助ポートであり、``PlaybackBackend`` の一般インターフェース
ではない。接続するのは composition root だけで、UI 層は
:class:`~sdp.core.playback.qt_backend.QtMultimediaBackend` を参照しない。

音声コールバック（``audioBufferReceived``。P0-C と P5-A の probe でともに
**GUI スレッド受信**を実測）で行うのは、buffer の妥当性確認・bytes 化・NumPy に
よる正規化とmono化・リングバッファ追記・軽量なシグナルだけとする。
FFT、band集約、描画、ファイル I/O、Model / Controller 操作は行わない。
"""

import logging
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtMultimedia import QAudioBuffer, QAudioBufferOutput

from sdp.core.analysis.pcm import audio_buffer_to_mono
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


class PcmTap(QObject):
    """QAudioBuffer を mono float32 へ変換してリングバッファへ流す小さなアダプター。

    持たないもの: FFT、Hann窓、dB変換、QWidget、QPainter、PlaylistModel、
    キャッシュ、設定、波形解析、シーク。
    """

    sample_rate_changed = Signal(int)
    """PCMのsample rateが変化した（未確定・解除時は0）。"""

    def __init__(
        self,
        playback: PlaybackController,
        ring_buffer: PcmRingBuffer | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._ring_buffer = (
            PcmRingBuffer(pcm_ring_capacity(DEFAULT_PCM_SAMPLE_RATE, FFT_SIZE))
            if ring_buffer is None
            else ring_buffer
        )
        self._sample_rate = 0
        self._received_count = 0
        self._discarded_count = 0
        self._buffer_output: QAudioBufferOutput | None = None
        self._shutdown = False

        playback.source_changed.connect(self._on_source_changed)
        playback.state_changed.connect(self._on_state_changed)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def ring_buffer(self) -> PcmRingBuffer:
        """同一バッファを共有していることを確認するために返す。"""
        return self._ring_buffer

    @property
    def sample_rate(self) -> int:
        """最新PCMのsample rate。まだ届いていなければ0。"""
        return self._sample_rate

    @property
    def available_frame_count(self) -> int:
        return self._ring_buffer.available

    @property
    def received_buffer_count(self) -> int:
        return self._received_count

    @property
    def discarded_buffer_count(self) -> int:
        return self._discarded_count

    def snapshot(self, frame_count: int) -> NDArray[np.float32]:
        """最新PCMのread-onlyコピーを返す（左0 padding）。"""
        return self._ring_buffer.snapshot(frame_count)

    # -- 接続 ---------------------------------------------------------------

    def connect_audio_buffer_output(self, buffer_output: QAudioBufferOutput) -> None:
        """QAudioBufferOutput のPCM通知を受け取り始める（composition rootのみ）。"""
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
        self.disconnect_audio_buffer_output()
        if not self._shutdown:
            self._shutdown = True
            try:
                self._playback.source_changed.disconnect(self._on_source_changed)
                self._playback.state_changed.disconnect(self._on_state_changed)
            except RuntimeError:
                # Controllerが既に破棄済み。終了処理を妨げない。
                _logger.debug("PcmTapのController接続は既に解除されています")
        self.clear()

    def clear(self) -> None:
        """リングバッファとsample rate状態を解除する。"""
        self._ring_buffer.clear()
        self._set_sample_rate(0)

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
        try:
            if not isinstance(value, QAudioBuffer):
                self._discard("QAudioBuffer以外が通知されました")
                return
            samples, sample_rate = audio_buffer_to_mono(value)
            if sample_rate != self._sample_rate:
                # 旧formatのサンプルを混ぜないため、容量ごと作り直す。
                self._ring_buffer.set_capacity(pcm_ring_capacity(sample_rate, FFT_SIZE))
                self._set_sample_rate(sample_rate)
            self._ring_buffer.append(samples)
            self._received_count += 1
        except ValueError as error:
            self._discard(str(error))
        except Exception:
            self._discarded_count += 1
            _logger.exception("PCMタップで予期しない例外が発生")

    @Slot(object)
    def _on_source_changed(self, source: object) -> None:
        del source
        # 新sourceの最初のPCMが届くまで、前sourceのPCMを残さない。
        self.clear()

    @Slot(PlaybackState)
    def _on_state_changed(self, state: PlaybackState) -> None:
        # PAUSED では保持する（最後のフレームを静止表示できるようにする）。
        if state in (PlaybackState.STOPPED, PlaybackState.NO_MEDIA):
            self.clear()

    # -- 内部 ---------------------------------------------------------------

    def _set_sample_rate(self, sample_rate: int) -> None:
        if sample_rate == self._sample_rate:
            return
        self._sample_rate = sample_rate
        self.sample_rate_changed.emit(sample_rate)

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

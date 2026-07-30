"""再生状態・PCMタップ・スペクトラムWidgetを接続するUI調停層。

固定FPSのQTimerでリングバッファのスナップショットを取り、FFTと平滑化を通して
Widgetへ反映する。PCMのdecode、QAudioBuffer、キャッシュ、PlaylistModel、
Backendの具体型は知らない。
"""

import logging

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from sdp.core.analysis.spectrum import SPECTRUM_TIMER_INTERVAL_MS, SpectrumProcessor
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.services.pcm_tap import PcmTap
from sdp.ui.spectrum_widget import NO_SOURCE_MESSAGE, SpectrumWidget

_logger = logging.getLogger(__name__)

STOPPED_MESSAGE = "停止中"
WAITING_MESSAGE = "PCMを待機中…"
FAILED_MESSAGE = "スペクトラムを表示できません"


class SpectrumPanel(QWidget):
    """PLAYING かつ表示中のときだけ、タイマー1回につき最大1回FFTを行う。"""

    def __init__(
        self,
        playback: PlaybackController,
        pcm_tap: PcmTap,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumPanel")
        self._playback = playback
        self._pcm_tap = pcm_tap
        self._processor = SpectrumProcessor()
        self._widget = SpectrumWidget(self)
        self._processing = False
        self._failed = False
        self._shutdown = False
        self._snapshot_count = 0
        self._analysis_count = 0
        self._watched_window: QWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)

        self._timer = QTimer(self)
        # 30FPSでは既定のCoarseTimerの粒度が粗く、体感のこま落ちになるため精密指定。
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(SPECTRUM_TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timeout)

        playback.source_changed.connect(self._on_source_changed)
        playback.state_changed.connect(self._on_state_changed)
        pcm_tap.sample_rate_changed.connect(self._on_sample_rate_changed)

        self._widget.set_db_floor(self._processor.db_floor)
        self._apply_state(playback.state)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def spectrum_widget(self) -> SpectrumWidget:
        return self._widget

    @property
    def pcm_tap(self) -> PcmTap:
        """compositionで同一タップを共有していることを確認するため返す。"""
        return self._pcm_tap

    @property
    def is_timer_active(self) -> bool:
        return self._timer.isActive()

    @property
    def snapshot_count(self) -> int:
        """スナップショット取得回数（タイマー1tickにつき1回）。"""
        return self._snapshot_count

    @property
    def analysis_count(self) -> int:
        """FFT実行回数（タイマー1tickにつき最大1回）。"""
        return self._analysis_count

    def shutdown(self) -> None:
        """接続とタイマーを終端し、以後の通知で再開しない（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        self._timer.stop()
        for signal, slot in (
            (self._playback.source_changed, self._on_source_changed),
            (self._playback.state_changed, self._on_state_changed),
            (self._pcm_tap.sample_rate_changed, self._on_sample_rate_changed),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                _logger.debug("SpectrumPanelのSignal接続は既に解除されています")
        if self._watched_window is not None:
            self._watched_window.removeEventFilter(self)
            self._watched_window = None

    # -- Qt イベント --------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._shutdown:
            return
        self._watch_top_level_window()
        self._update_timer()

    def hideEvent(self, event: QHideEvent) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # top-level windowの最小化・復帰では子へhideEventが来ないため直接監視する。
        if (
            not self._shutdown
            and watched is self._watched_window
            and event.type() is QEvent.Type.WindowStateChange
        ):
            self._update_timer()
        return super().eventFilter(watched, event)

    # -- Controller / PcmTap からの通知 -------------------------------------

    @Slot(object)
    def _on_source_changed(self, source: object) -> None:
        if self._shutdown:
            return
        # PcmTap側もclearするが、表示状態はPanelが独立して解除する。
        self._failed = False
        self._processor.reset()
        # source_changed時点のstateがPLAYINGでも、前sourceのframeを即時に捨てる。
        self._widget.clear_frame(NO_SOURCE_MESSAGE if source is None else WAITING_MESSAGE)
        self._update_timer()

    @Slot(PlaybackState)
    def _on_state_changed(self, state: PlaybackState) -> None:
        if self._shutdown:
            return
        self._apply_state(state)

    @Slot(int)
    def _on_sample_rate_changed(self, sample_rate: int) -> None:
        del sample_rate
        if self._shutdown:
            return
        # 旧formatの平滑化状態を新formatへ持ち越さない。
        self._processor.reset()

    # -- 内部 ---------------------------------------------------------------

    def _apply_state(self, state: PlaybackState) -> None:
        """再生状態に応じて表示とタイマーを合わせる。

        PLAYING は追従、PAUSED は最後のフレームを静止表示、STOPPED と
        NO_MEDIA は履歴を捨ててプレースホルダーへ戻す。
        """
        if state is PlaybackState.PLAYING:
            if not self._failed:
                self._widget.set_status_text("")
        elif state is PlaybackState.PAUSED:
            # 最後のフレームは残したまま新しいFFTを止める。
            pass
        else:
            self._processor.reset()
            self._failed = False
            self._widget.clear_frame(
                NO_SOURCE_MESSAGE if self._playback.source is None else STOPPED_MESSAGE
            )
        self._update_timer()

    def _update_timer(self) -> None:
        if self._should_run():
            if not self._timer.isActive():
                self._timer.start()
            return
        self._timer.stop()

    def _should_run(self) -> bool:
        window = self.window()
        return (
            not self._shutdown
            and not self._failed
            and self._playback.state is PlaybackState.PLAYING
            and self.isVisible()
            and not window.isMinimized()
        )

    def _watch_top_level_window(self) -> None:
        window = self.window()
        if window is self._watched_window:
            return
        if self._watched_window is not None:
            self._watched_window.removeEventFilter(self)
        self._watched_window = window
        window.installEventFilter(self)

    @Slot()
    def _on_timeout(self) -> None:
        # timer停止後に残っていた古いtimeoutでも安全に何もしない。
        if self._processing or not self._should_run():
            return
        self._processing = True
        try:
            sample_rate = self._pcm_tap.sample_rate
            samples = self._pcm_tap.snapshot(self._processor.fft_size)
            self._snapshot_count += 1
            if sample_rate < 1:
                # 最初のPCMが届くまではプレースホルダーのまま待つ。
                return
            frame = self._processor.process(samples, sample_rate)
            self._analysis_count += 1
            self._widget.set_frame(frame)
            self._widget.set_status_text("")
        except Exception:
            # スペクトラムの失敗は再生を妨げない。Controllerへは何も要求しない。
            _logger.exception("スペクトラムの更新に失敗しました")
            self._failed = True
            self._processor.reset()
            self._widget.clear_frame(FAILED_MESSAGE)
            self._timer.stop()
        finally:
            self._processing = False

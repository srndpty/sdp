"""再生状態・PCMタップ・スペクトラム／レベルメーターを接続するUI調停層。

固定FPSのQTimerで1tickにつき mono snapshot 1回・stereo snapshot 1回を取り、
FFTと平滑化、Peak／RMSとPeak holdへそれぞれ最大1回だけ通してWidgetへ反映する。
PCMのdecode、QAudioBuffer、キャッシュ、PlaylistModel、Backendの具体型は知らない。

スペクトラムとレベルは同じPcmTapと同じtickを共有するが、**例外境界は独立**とする
（片方の失敗で他方と音声再生を止めない）。
"""

import logging

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from sdp.core.analysis.level import LEVEL_WINDOW_SIZE, LevelProcessor
from sdp.core.analysis.spectrum import SPECTRUM_TIMER_INTERVAL_MS, SpectrumProcessor
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.services.pcm_tap import PcmTap
from sdp.ui.level_meter_widget import NO_SOURCE_MESSAGE as LEVEL_NO_SOURCE_MESSAGE
from sdp.ui.level_meter_widget import LevelMeterWidget
from sdp.ui.spectrum_widget import NO_SOURCE_MESSAGE, SpectrumWidget

_logger = logging.getLogger(__name__)

STOPPED_MESSAGE = "停止中"
WAITING_MESSAGE = "PCMを待機中…"
FAILED_MESSAGE = "スペクトラムを表示できません"
LEVEL_FAILED_MESSAGE = "レベルを表示できません"


class SpectrumPanel(QWidget):
    """PLAYING かつ表示中のときだけ、タイマー1tickにつき解析を各最大1回行う。

    スペクトラムとレベルメーターを1つのPanelへまとめ、同じPcmTapと同じtickを
    共有する（MainWindowへ可視化ごとの依存やLevelProcessorを増やさない）。
    """

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
        self._level_processor = LevelProcessor()
        self._widget = SpectrumWidget(self)
        self._level_widget = LevelMeterWidget(self)
        self._processing = False
        self._spectrum_failed = False
        self._level_failed = False
        self._snapshot_failed = False
        self._shutdown = False
        self._snapshot_count = 0
        self._stereo_snapshot_count = 0
        self._analysis_count = 0
        self._level_count = 0
        self._watched_window: QWidget | None = None
        # Peak holdの減衰をタイマー回数ではなく実時間で進めるための時計。
        self._level_clock = QElapsedTimer()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)
        layout.addWidget(self._level_widget)

        self._timer = QTimer(self)
        # 30FPSでは既定のCoarseTimerの粒度が粗く、体感のこま落ちになるため精密指定。
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(SPECTRUM_TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timeout)

        playback.source_changed.connect(self._on_source_changed)
        playback.state_changed.connect(self._on_state_changed)
        pcm_tap.sample_rate_changed.connect(self._on_format_changed)
        pcm_tap.channel_count_changed.connect(self._on_format_changed)

        self._widget.set_db_floor(self._processor.db_floor)
        self._level_widget.set_db_floor(self._level_processor.db_floor)
        self._apply_state(playback.state)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def spectrum_widget(self) -> SpectrumWidget:
        return self._widget

    @property
    def level_meter_widget(self) -> LevelMeterWidget:
        return self._level_widget

    @property
    def pcm_tap(self) -> PcmTap:
        """compositionで同一タップを共有していることを確認するため返す。"""
        return self._pcm_tap

    @property
    def is_timer_active(self) -> bool:
        return self._timer.isActive()

    @property
    def snapshot_count(self) -> int:
        """mono スナップショット取得回数（タイマー1tickにつき1回）。"""
        return self._snapshot_count

    @property
    def stereo_snapshot_count(self) -> int:
        """L／R スナップショット取得回数（タイマー1tickにつき1回）。"""
        return self._stereo_snapshot_count

    @property
    def analysis_count(self) -> int:
        """FFT実行回数（タイマー1tickにつき最大1回）。"""
        return self._analysis_count

    @property
    def level_count(self) -> int:
        """Peak／RMS計算回数（タイマー1tickにつき最大1回）。"""
        return self._level_count

    def shutdown(self) -> None:
        """接続とタイマーを終端し、以後の通知で再開しない（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        self._stop_timer()
        for signal, slot in (
            (self._playback.source_changed, self._on_source_changed),
            (self._playback.state_changed, self._on_state_changed),
            (self._pcm_tap.sample_rate_changed, self._on_format_changed),
            (self._pcm_tap.channel_count_changed, self._on_format_changed),
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
        self._stop_timer()
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
        self._spectrum_failed = False
        self._level_failed = False
        self._snapshot_failed = False
        self._processor.reset()
        self._level_processor.reset()
        # source_changed時点のstateがPLAYINGでも、前sourceのframeを即時に捨てる。
        waiting = source is not None
        self._widget.clear_frame(WAITING_MESSAGE if waiting else NO_SOURCE_MESSAGE)
        self._level_widget.clear_frame(WAITING_MESSAGE if waiting else LEVEL_NO_SOURCE_MESSAGE)
        self._update_timer()

    @Slot(PlaybackState)
    def _on_state_changed(self, state: PlaybackState) -> None:
        if self._shutdown:
            return
        self._apply_state(state)

    @Slot(int)
    def _on_format_changed(self, value: int) -> None:
        del value
        if self._shutdown:
            return
        # 旧formatの平滑化状態とPeak holdを新formatへ持ち越さない。
        self._processor.reset()
        self._level_processor.reset()

    # -- 内部 ---------------------------------------------------------------

    def _apply_state(self, state: PlaybackState) -> None:
        """再生状態に応じて表示とタイマーを合わせる。

        PLAYING は追従、PAUSED は最後のフレームを静止表示、STOPPED と
        NO_MEDIA は履歴を捨ててプレースホルダーへ戻す。
        """
        if state is PlaybackState.PLAYING:
            if not self._spectrum_failed:
                self._widget.set_status_text("")
            if not self._level_failed:
                self._level_widget.set_status_text("")
        elif state is PlaybackState.PAUSED:
            # 最後のフレームは残したまま新しい解析を止める。Peak holdの時間も
            # 進めない（タイマー停止時に時計を無効化する）。
            pass
        else:
            self._processor.reset()
            self._level_processor.reset()
            self._spectrum_failed = False
            self._level_failed = False
            self._snapshot_failed = False
            stopped = self._playback.source is not None
            self._widget.clear_frame(STOPPED_MESSAGE if stopped else NO_SOURCE_MESSAGE)
            self._level_widget.clear_frame(STOPPED_MESSAGE if stopped else LEVEL_NO_SOURCE_MESSAGE)
        self._update_timer()

    def _update_timer(self) -> None:
        if self._should_run():
            if not self._timer.isActive():
                self._timer.start()
            return
        self._stop_timer()

    def _stop_timer(self) -> None:
        self._timer.stop()
        # 停止中の実時間をPeak holdの減衰へ数えない（pause・非表示・最小化）。
        self._level_clock.invalidate()

    def _should_run(self) -> bool:
        window = self.window()
        return (
            not self._shutdown
            and not self._snapshot_failed
            # 片方だけの失敗では、生き残った解析のためにタイマーを続ける。
            and not (self._spectrum_failed and self._level_failed)
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
            try:
                mono = self._pcm_tap.snapshot_mono(self._processor.fft_size)
                self._snapshot_count += 1
                left, right = self._pcm_tap.snapshot_stereo(LEVEL_WINDOW_SIZE)
                self._stereo_snapshot_count += 1
            except Exception:
                # 共通のsnapshot失敗は両方の可視化を止める（再生は妨げない）。
                _logger.exception("PCMスナップショットの取得に失敗しました")
                self._snapshot_failed = True
                self._processor.reset()
                self._level_processor.reset()
                self._widget.clear_frame(FAILED_MESSAGE)
                self._level_widget.clear_frame(LEVEL_FAILED_MESSAGE)
                self._stop_timer()
                return
            elapsed_seconds = self._consumed_elapsed_seconds()
            if sample_rate < 1:
                # 最初のPCMが届くまではプレースホルダーのまま待つ。
                return

            # 解析ごとに例外境界を分ける。片方の失敗で他方と再生を止めない。
            if not self._spectrum_failed:
                try:
                    spectrum_frame = self._processor.process(mono, sample_rate)
                    self._analysis_count += 1
                    self._widget.set_frame(spectrum_frame)
                    self._widget.set_status_text("")
                except Exception:
                    _logger.exception("スペクトラムの更新に失敗しました")
                    self._spectrum_failed = True
                    self._processor.reset()
                    self._widget.clear_frame(FAILED_MESSAGE)

            if not self._level_failed:
                try:
                    level_frame = self._level_processor.process(
                        left, right, elapsed_seconds=elapsed_seconds
                    )
                    self._level_count += 1
                    self._level_widget.set_frame(level_frame)
                    self._level_widget.set_status_text("")
                except Exception:
                    _logger.exception("レベルメーターの更新に失敗しました")
                    self._level_failed = True
                    self._level_processor.reset()
                    self._level_widget.clear_frame(LEVEL_FAILED_MESSAGE)

            # 両方が失敗した場合だけタイマーを止める（ログの大量出力を避ける）。
            self._update_timer()
        finally:
            self._processing = False

    def _consumed_elapsed_seconds(self) -> float:
        """前tickからの実経過秒を取り出す（初回と再開直後は0）。"""
        if not self._level_clock.isValid():
            self._level_clock.start()
            return 0.0
        return max(0.0, self._level_clock.restart() / 1000.0)

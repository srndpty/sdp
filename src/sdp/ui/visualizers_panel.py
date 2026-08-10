"""追加ビジュアライザー群をPcmTapへ接続するUI調停層。

オシロスコープ、ベクトルスコープ、位相相関、スペクトログラム、クロマグラムを
1つのPanelへまとめ、同じPcmTapと同じ固定FPSタイマーを共有する。
PCMのdecode、QAudioBuffer、キャッシュ、PlaylistModel、Backendの具体型は知らない。

各可視化の例外境界は独立させる。片方の失敗で他方と音声再生を止めない。
失敗した可視化は自動では再開しないが、設定でOFF→ONし直したときは再試行する
（一時的な失敗のあと、その曲の間ずっと表示できないままにしない）。
"""

import logging
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Slot
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from sdp.core.analysis.chroma import ChromaProcessor
from sdp.core.analysis.oscilloscope import OSCILLOSCOPE_WINDOW, compute_oscilloscope
from sdp.core.analysis.spectrogram import SpectrogramProcessor
from sdp.core.analysis.spectrum import (
    FFT_SIZE,
    SPECTRUM_TIMER_INTERVAL_MS,
    FrequencyAnalysisFrame,
    compute_frequency_analysis,
)
from sdp.core.analysis.stereo import (
    VECTORSCOPE_WINDOW,
    CorrelationProcessor,
    compute_vectorscope,
)
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.services.pcm_tap import SNAPSHOT_RETRY_DELAYS_MS, PcmTap
from sdp.ui.chromagram_widget import NO_SOURCE_MESSAGE as CHROMA_NO_SOURCE_MESSAGE
from sdp.ui.chromagram_widget import ChromagramWidget
from sdp.ui.correlation_meter_widget import NO_SOURCE_MESSAGE as CORRELATION_NO_SOURCE_MESSAGE
from sdp.ui.correlation_meter_widget import CorrelationMeterWidget
from sdp.ui.oscilloscope_widget import NO_SOURCE_MESSAGE as OSCILLOSCOPE_NO_SOURCE_MESSAGE
from sdp.ui.oscilloscope_widget import OscilloscopeWidget
from sdp.ui.spectrogram_widget import NO_SOURCE_MESSAGE as SPECTROGRAM_NO_SOURCE_MESSAGE
from sdp.ui.spectrogram_widget import SpectrogramWidget
from sdp.ui.vectorscope_widget import NO_SOURCE_MESSAGE as VECTORSCOPE_NO_SOURCE_MESSAGE
from sdp.ui.vectorscope_widget import VectorscopeWidget

_logger = logging.getLogger(__name__)

STOPPED_MESSAGE = "停止中"
WAITING_MESSAGE = "PCMを待機中…"
FAILED_MESSAGE = "可視化を表示できません"


@dataclass(frozen=True, slots=True)
class _VisualizationItem:
    name: str
    widget: QWidget
    no_source_message: str


class VisualizersPanel(QWidget):
    """PLAYING かつ表示中のときだけ、追加ビジュアライザーを更新する。"""

    def __init__(
        self,
        playback: PlaybackController,
        pcm_tap: PcmTap,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("visualizersPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._playback = playback
        self._pcm_tap = pcm_tap
        self._oscilloscope_widget = OscilloscopeWidget(self)
        self._vectorscope_widget = VectorscopeWidget(self)
        self._correlation_widget = CorrelationMeterWidget(self)
        self._spectrogram_widget = SpectrogramWidget(self)
        self._chromagram_widget = ChromagramWidget(self)
        self._chroma_processor = ChromaProcessor()
        self._correlation_processor = CorrelationProcessor()
        self._spectrogram_processor = SpectrogramProcessor()
        self._processing = False
        self._shutdown = False
        self._watched_window: QWidget | None = None
        self._snapshot_count = 0
        self._snapshot_failure_count = 0
        self._analysis_count = 0
        self._oscilloscope_count = 0
        self._vectorscope_count = 0
        self._correlation_count = 0
        self._spectrogram_count = 0
        self._chromagram_count = 0
        self._enabled: dict[str, bool] = {
            "oscilloscope": True,
            "vectorscope": True,
            "correlation": True,
            "spectrogram": True,
            "chromagram": True,
        }
        self._failed: dict[str, bool] = dict.fromkeys(self._enabled, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for widget in (
            self._oscilloscope_widget,
            self._vectorscope_widget,
            self._correlation_widget,
            self._spectrogram_widget,
            self._chromagram_widget,
        ):
            layout.addWidget(widget)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(SPECTRUM_TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timeout)
        # snapshot失敗からの再試行（SpectrumPanelと同じ間隔で段階的に広げる）。
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._on_retry)

        playback.source_changed.connect(self._on_source_changed)
        playback.state_changed.connect(self._on_state_changed)
        pcm_tap.sample_rate_changed.connect(self._on_format_changed)
        pcm_tap.channel_count_changed.connect(self._on_format_changed)
        self._apply_state(playback.state)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def oscilloscope_widget(self) -> OscilloscopeWidget:
        return self._oscilloscope_widget

    @property
    def vectorscope_widget(self) -> VectorscopeWidget:
        return self._vectorscope_widget

    @property
    def correlation_meter_widget(self) -> CorrelationMeterWidget:
        return self._correlation_widget

    @property
    def spectrogram_widget(self) -> SpectrogramWidget:
        return self._spectrogram_widget

    @property
    def chromagram_widget(self) -> ChromagramWidget:
        return self._chromagram_widget

    @property
    def is_timer_active(self) -> bool:
        return self._timer.isActive()

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    @property
    def analysis_count(self) -> int:
        """スペクトログラムとクロマグラムで共有したrFFTの実行回数。"""
        return self._analysis_count

    @property
    def snapshot_failure_count(self) -> int:
        """連続して失敗しているPCM snapshot取得の回数（成功で0へ戻る）。"""
        return self._snapshot_failure_count

    @property
    def oscilloscope_count(self) -> int:
        return self._oscilloscope_count

    @property
    def vectorscope_count(self) -> int:
        return self._vectorscope_count

    @property
    def correlation_count(self) -> int:
        return self._correlation_count

    @property
    def spectrogram_count(self) -> int:
        return self._spectrogram_count

    @property
    def chromagram_count(self) -> int:
        return self._chromagram_count

    def set_oscilloscope_visible(self, visible: bool) -> None:
        self._set_visualization_enabled("oscilloscope", visible)

    def set_vectorscope_visible(self, visible: bool) -> None:
        self._set_visualization_enabled("vectorscope", visible)

    def set_correlation_meter_visible(self, visible: bool) -> None:
        self._set_visualization_enabled("correlation", visible)

    def set_spectrogram_visible(self, visible: bool) -> None:
        self._set_visualization_enabled("spectrogram", visible)

    def set_chromagram_visible(self, visible: bool) -> None:
        self._set_visualization_enabled("chromagram", visible)

    def shutdown(self) -> None:
        """接続とタイマーを終端し、以後の通知で再開しない（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        self._timer.stop()
        self._retry_timer.stop()
        for signal, slot in (
            (self._playback.source_changed, self._on_source_changed),
            (self._playback.state_changed, self._on_state_changed),
            (self._pcm_tap.sample_rate_changed, self._on_format_changed),
            (self._pcm_tap.channel_count_changed, self._on_format_changed),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                _logger.debug("VisualizersPanelのSignal接続は既に解除されています")
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
        if (
            not self._shutdown
            and watched is self._watched_window
            and event.type() is QEvent.Type.WindowStateChange
        ):
            self._update_timer()
        return super().eventFilter(watched, event)

    # -- Controller / PcmTap ------------------------------------------------

    @Slot(object)
    def _on_source_changed(self, source: object) -> None:
        if self._shutdown:
            return
        self._reset_processors()
        self._clear_failures()
        waiting = source is not None
        self._clear_all(WAITING_MESSAGE if waiting else None)
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
        self._reset_processors()
        self._clear_failures()
        self._update_timer()

    # -- 内部 ---------------------------------------------------------------

    def _apply_state(self, state: PlaybackState) -> None:
        if state is PlaybackState.PLAYING:
            self._set_status_for_active("")
        elif state is PlaybackState.PAUSED:
            pass
        else:
            self._reset_processors()
            self._failed = dict.fromkeys(self._failed, False)
            self._clear_all(STOPPED_MESSAGE if self._playback.source is not None else None)
        self._update_timer()

    def _reset_processors(self) -> None:
        self._chroma_processor.reset()
        self._correlation_processor.reset()
        self._spectrogram_processor.reset()

    def _clear_failures(self) -> None:
        """失敗状態と再試行の待機を捨て、次tickから全可視化をやり直せるようにする。"""
        self._failed = dict.fromkeys(self._failed, False)
        self._snapshot_failure_count = 0
        self._retry_timer.stop()

    @Slot()
    def _on_retry(self) -> None:
        if self._shutdown:
            return
        self._update_timer()

    def _items(self) -> tuple[_VisualizationItem, ...]:
        return (
            _VisualizationItem(
                "oscilloscope", self._oscilloscope_widget, OSCILLOSCOPE_NO_SOURCE_MESSAGE
            ),
            _VisualizationItem(
                "vectorscope", self._vectorscope_widget, VECTORSCOPE_NO_SOURCE_MESSAGE
            ),
            _VisualizationItem(
                "correlation", self._correlation_widget, CORRELATION_NO_SOURCE_MESSAGE
            ),
            _VisualizationItem(
                "spectrogram", self._spectrogram_widget, SPECTROGRAM_NO_SOURCE_MESSAGE
            ),
            _VisualizationItem("chromagram", self._chromagram_widget, CHROMA_NO_SOURCE_MESSAGE),
        )

    def _widget_of(self, name: str) -> QWidget:
        return next(item.widget for item in self._items() if item.name == name)

    def _set_status_for_active(self, text: str) -> None:
        for item in self._items():
            if self._active(item.name):
                self._set_widget_status(item.widget, text)

    def _clear_all(self, message: str | None) -> None:
        for item in self._items():
            self._clear_widget(item.widget, item.no_source_message if message is None else message)

    def _set_visualization_enabled(self, name: str, visible: bool) -> None:
        if self._shutdown:
            return
        if self._enabled[name] == visible:
            return
        self._enabled[name] = visible
        item = next(item for item in self._items() if item.name == name)
        item.widget.setVisible(visible)
        if visible:
            # 明示的な再表示は再試行の要求として扱う（一時的な失敗を持ち越さない）。
            self._failed[name] = False
            self._snapshot_failure_count = 0
            self._retry_timer.stop()
            self._set_widget_status(item.widget, self._placeholder_message(item.no_source_message))
        else:
            self._clear_widget(item.widget, self._placeholder_message(item.no_source_message))
            if name == "correlation":
                self._correlation_processor.reset()
            elif name == "spectrogram":
                self._spectrogram_processor.reset()
            elif name == "chromagram":
                self._chroma_processor.reset()
        self.setVisible(any(self._enabled.values()))
        self._update_timer()

    def _placeholder_message(self, no_source_message: str) -> str:
        if self._playback.source is None:
            return no_source_message
        if self._playback.state is PlaybackState.PLAYING:
            return WAITING_MESSAGE
        return STOPPED_MESSAGE

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
            and any(self._active(name) for name in self._enabled)
            and self._playback.state is PlaybackState.PLAYING
            and self.isVisible()
            and not window.isMinimized()
        )

    def _active(self, name: str) -> bool:
        return self._enabled[name] and not self._failed[name]

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
        if self._processing or not self._should_run():
            return
        self._processing = True
        try:
            mono_frames = max(FFT_SIZE, OSCILLOSCOPE_WINDOW) if self._needs_mono() else 0
            stereo_frames = (
                max(VECTORSCOPE_WINDOW, self._correlation_processor.window_size)
                if self._needs_stereo()
                else 0
            )
            try:
                snapshot = self._pcm_tap.snapshot_visualization(
                    mono_frames=mono_frames,
                    level_frames=stereo_frames,
                )
            except Exception:
                self._handle_snapshot_failure()
                return
            self._snapshot_count += 1
            self._snapshot_failure_count = 0

            if snapshot.sample_rate < 1:
                return
            analysis = self._frequency_analysis(snapshot.mono, snapshot.sample_rate)
            self._update_oscilloscope(snapshot.mono)
            self._update_vectorscope(snapshot.left, snapshot.right)
            self._update_correlation(snapshot.left, snapshot.right)
            self._update_spectrogram(snapshot.mono, snapshot.sample_rate, analysis)
            self._update_chromagram(snapshot.mono, snapshot.sample_rate, analysis)
            self._update_timer()
        finally:
            self._processing = False

    def _handle_snapshot_failure(self) -> None:
        """snapshot取得の失敗を、回復可能な障害として扱う。

        規定回数までは間隔を広げながら再試行し（その間は直前の表示を保つ）、
        超えて初めて全可視化を失敗表示にする。同じ例外を毎フレーム記録しない。
        """
        self._snapshot_failure_count += 1
        attempt = self._snapshot_failure_count
        if attempt == 1:
            _logger.exception("追加可視化のPCMスナップショット取得に失敗しました。再試行します")
        else:
            _logger.debug(
                "追加可視化のPCMスナップショット取得に再度失敗しました（%d回目）", attempt
            )

        self._timer.stop()
        if attempt <= len(SNAPSHOT_RETRY_DELAYS_MS):
            self._retry_timer.start(SNAPSHOT_RETRY_DELAYS_MS[attempt - 1])
            return

        _logger.error("PCMスナップショットの取得に%d回失敗したため追加可視化を停止します", attempt)
        for name in self._enabled:
            if self._active(name):
                self._failed[name] = True
        self._clear_all(FAILED_MESSAGE)
        self._update_timer()

    def _frequency_analysis(self, mono: object, sample_rate: int) -> FrequencyAnalysisFrame | None:
        """スペクトログラムとクロマグラムで共有するrFFTを、1tickで1回だけ実行する。

        FFT自体の失敗はこの2つの可視化に共通するため、両方をまとめて失敗させる。
        """
        shared = ("spectrogram", "chromagram")
        if not any(self._active(name) for name in shared):
            return None
        try:
            analysis = compute_frequency_analysis(mono, sample_rate)  # pyright: ignore[reportArgumentType]
        except Exception:
            for name in shared:
                if self._active(name):
                    self._fail(name, self._widget_of(name))
            return None
        self._analysis_count += 1
        return analysis

    def _needs_mono(self) -> bool:
        return any(self._active(name) for name in ("oscilloscope", "spectrogram", "chromagram"))

    def _needs_stereo(self) -> bool:
        return any(self._active(name) for name in ("vectorscope", "correlation"))

    def _update_oscilloscope(self, mono: object) -> None:
        if not self._active("oscilloscope"):
            return
        try:
            frame = compute_oscilloscope(mono, window=OSCILLOSCOPE_WINDOW)  # pyright: ignore[reportArgumentType]
            self._oscilloscope_count += 1
            self._oscilloscope_widget.set_frame(frame)
            self._oscilloscope_widget.set_status_text("")
        except Exception:
            self._fail("oscilloscope", self._oscilloscope_widget)

    def _update_vectorscope(self, left: object, right: object) -> None:
        if not self._active("vectorscope"):
            return
        try:
            frame = compute_vectorscope(left, right)  # pyright: ignore[reportArgumentType]
            self._vectorscope_count += 1
            self._vectorscope_widget.set_frame(frame)
            self._vectorscope_widget.set_status_text("")
        except Exception:
            self._fail("vectorscope", self._vectorscope_widget)

    def _update_correlation(self, left: object, right: object) -> None:
        if not self._active("correlation"):
            return
        try:
            value = self._correlation_processor.process(left, right)  # pyright: ignore[reportArgumentType]
            self._correlation_count += 1
            self._correlation_widget.set_correlation(value)
            self._correlation_widget.set_status_text("")
        except Exception:
            self._fail("correlation", self._correlation_widget)

    def _update_spectrogram(
        self, mono: object, sample_rate: int, analysis: FrequencyAnalysisFrame | None
    ) -> None:
        if not self._active("spectrogram"):
            return
        try:
            frame = self._spectrogram_processor.process(mono, sample_rate, analysis=analysis)  # pyright: ignore[reportArgumentType]
            self._spectrogram_count += 1
            self._spectrogram_widget.set_frame(frame)
            self._spectrogram_widget.set_status_text("")
        except Exception:
            self._fail("spectrogram", self._spectrogram_widget)

    def _update_chromagram(
        self, mono: object, sample_rate: int, analysis: FrequencyAnalysisFrame | None
    ) -> None:
        if not self._active("chromagram"):
            return
        try:
            frame = self._chroma_processor.process(mono, sample_rate, analysis=analysis)  # pyright: ignore[reportArgumentType]
            self._chromagram_count += 1
            self._chromagram_widget.set_frame(frame)
            self._chromagram_widget.set_status_text("")
        except Exception:
            self._fail("chromagram", self._chromagram_widget)

    def _fail(self, name: str, widget: QWidget) -> None:
        _logger.exception("%sの更新に失敗しました", name)
        self._failed[name] = True
        self._clear_widget(widget, FAILED_MESSAGE)

    def _clear_widget(self, widget: QWidget, message: str) -> None:
        if isinstance(
            widget,
            OscilloscopeWidget | VectorscopeWidget | SpectrogramWidget | ChromagramWidget,
        ):
            widget.clear_frame(message)
        elif isinstance(widget, CorrelationMeterWidget):
            widget.clear_value(message)

    def _set_widget_status(self, widget: QWidget, message: str) -> None:
        if isinstance(
            widget,
            OscilloscopeWidget
            | VectorscopeWidget
            | SpectrogramWidget
            | ChromagramWidget
            | CorrelationMeterWidget,
        ):
            widget.set_status_text(message)

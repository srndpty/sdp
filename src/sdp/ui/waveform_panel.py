"""再生・解析状態を追従波形Widgetへ接続するUI調停層。

設定で非表示にされているあいだは位置追従の更新を行わず、再表示時に
現在sourceの位置・長さへ復帰する（[architecture.md](../../../docs/architecture.md) §7）。
"""

from pathlib import Path

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

from sdp.core.analysis.waveform import WaveformData
from sdp.core.playback.controller import PlaybackController
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.waveform_widget import WaveformWidget

ANALYZING_MESSAGE = "波形を解析中…"
FAILED_MESSAGE = "波形を表示できません"


class WaveformPanel(QWidget):
    """Controllerと解析Serviceを接続し、現在sourceだけをWidgetへ反映する。"""

    def __init__(
        self,
        playback: PlaybackController,
        waveform_analysis: WaveformAnalysisService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("waveformPanel")
        self._playback = playback
        self._waveform_analysis = waveform_analysis
        self._active_path: Path | None = None
        self._active_token: int | None = None
        self._waveform_data: WaveformData | None = None

        self._widget = WaveformWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)

        playback.source_changed.connect(self._on_source_changed)
        playback.position_changed.connect(self._on_position_changed)
        playback.duration_changed.connect(self._on_duration_changed)
        waveform_analysis.analysis_started.connect(self._on_analysis_started)
        waveform_analysis.partial_ready.connect(self._on_partial_ready)
        waveform_analysis.analysis_finished.connect(self._on_analysis_finished)
        waveform_analysis.analysis_failed.connect(self._on_analysis_failed)
        waveform_analysis.analysis_cleared.connect(self._on_analysis_cleared)
        self._widget.seek_requested.connect(playback.seek)

        source = playback.source
        self._widget.reset_for_source(source is not None)
        if source is not None:
            self._widget.set_position(max(0, playback.position_ms))
            self._widget.set_duration(max(0, playback.duration_ms))

    @property
    def waveform_analysis(self) -> WaveformAnalysisService:
        """compositionで同一Serviceを共有していることを確認するため返す。"""
        return self._waveform_analysis

    @property
    def waveform_widget(self) -> WaveformWidget:
        return self._widget

    def showEvent(self, event: QShowEvent) -> None:
        """再表示時に現在sourceの位置・長さへ追従し直す。

        非表示中もsource変更とcache／解析結果の反映は続けているため、
        ここで復帰するのは追従位置と長さだけでよい。旧sourceの波形は
        ``_on_source_changed`` で既に破棄されている。
        """
        super().showEvent(event)
        if self._playback.source is not None:
            self._widget.set_position(max(0, self._playback.position_ms))
        self._widget.set_duration(self._effective_duration(self._playback.duration_ms))

    def _on_source_changed(self, source: object) -> None:
        self._active_path = None
        self._active_token = None
        self._waveform_data = None
        available = isinstance(source, Path)
        self._widget.reset_for_source(available)

    def _on_position_changed(self, position_ms: int) -> None:
        # 非表示中は追従描画を行わない（復帰時にshowEventで追い付く）。
        if self._playback.source is not None and self.isVisible():
            self._widget.set_position(max(0, position_ms))

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._widget.set_duration(self._effective_duration(duration_ms))

    def _on_analysis_started(self, path_value: object, token: int) -> None:
        if not isinstance(path_value, Path) or path_value != self._playback.source:
            return
        self._active_path = path_value
        self._active_token = token
        self._waveform_data = None
        self._widget.set_waveform_data(None)
        self._widget.set_status_text(ANALYZING_MESSAGE)

    def _on_partial_ready(self, path_value: object, token: int, data_value: object) -> None:
        if not self._matches_active(path_value, token) or not isinstance(data_value, WaveformData):
            return
        self._waveform_data = data_value
        self._widget.set_waveform_data(data_value)
        self._widget.set_status_text(ANALYZING_MESSAGE)
        self._widget.set_duration(self._effective_duration(self._playback.duration_ms))

    def _on_analysis_finished(
        self,
        path_value: object,
        token: int,
        data_value: object,
        from_cache: bool,
    ) -> None:
        del from_cache
        if not self._matches_active(path_value, token) or not isinstance(data_value, WaveformData):
            return
        self._waveform_data = data_value
        self._widget.set_waveform_data(data_value)
        self._widget.set_status_text("")
        self._widget.set_duration(self._effective_duration(self._playback.duration_ms))

    def _on_analysis_failed(self, path_value: object, token: int, message: str) -> None:
        del message
        if not self._matches_active(path_value, token):
            return
        self._widget.set_status_text(FAILED_MESSAGE)
        self._widget.set_duration(self._effective_duration(self._playback.duration_ms))

    def _on_analysis_cleared(self) -> None:
        self._active_path = None
        self._active_token = None
        self._waveform_data = None
        available = self._playback.source is not None
        self._widget.reset_for_source(available)

    def _matches_active(self, path_value: object, token: int) -> bool:
        return (
            isinstance(path_value, Path)
            and path_value == self._active_path
            and path_value == self._playback.source
            and token == self._active_token
        )

    def _effective_duration(self, controller_duration_ms: int) -> int:
        if controller_duration_ms > 0:
            return controller_duration_ms
        data = self._waveform_data
        if data is not None and data.complete and data.duration_ms > 0:
            return data.duration_ms
        return 0

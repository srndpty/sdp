"""オシロスコープ（瞬時波形）をQPainterで一括描画するWidget。

1回のpaintEventで波形ポリラインを描く。トリガー整列・窓切り出し・タイマーは
持たない（VisualizerPanelの責務）。
"""

from PySide6.QtCore import QEvent, QLineF, QPointF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.oscilloscope import OscilloscopeFrame

NO_SOURCE_MESSAGE = "音声を再生すると波形を表示します"

_MINIMUM_HEIGHT = 70


class OscilloscopeWidget(QWidget):
    """瞬時波形を中央線基準のポリラインで描く。マウス操作もフォーカスも持たない。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("oscilloscopeWidget")
        self.setAccessibleName("オシロスコープ")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: OscilloscopeFrame | None = None
        self._status_text = NO_SOURCE_MESSAGE
        self._last_point_count = 0

    @property
    def frame(self) -> OscilloscopeFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def last_point_count(self) -> int:
        """直前のpaintEventで描いた頂点数（性能・間引き確認用）。"""
        return self._last_point_count

    def set_frame(self, frame: OscilloscopeFrame | None) -> None:
        if frame is self._frame:
            return
        self._frame = frame
        self.update()

    def set_status_text(self, text: str) -> None:
        if text == self._status_text:
            return
        self._status_text = text
        self.update()

    def clear_frame(self, status_text: str = NO_SOURCE_MESSAGE) -> None:
        self._frame = None
        self._status_text = status_text
        self.update()

    # -- 描画 ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        width = float(self.width())
        height = float(self.height())
        middle = height / 2.0

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        painter.drawLine(QLineF(0.0, middle, width, middle))

        self._last_point_count = 0
        frame = self._frame
        if frame is not None and frame.sample_count > 0 and width >= 1.0:
            self._draw_wave(painter, frame, width, height, palette)

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                self.rect().adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                if self._last_point_count
                else Qt.AlignmentFlag.AlignCenter,
                self._status_text,
            )
        painter.end()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    # -- 内部 ---------------------------------------------------------------

    def _draw_wave(
        self,
        painter: QPainter,
        frame: OscilloscopeFrame,
        width: float,
        height: float,
        palette: QPalette,
    ) -> None:
        samples = frame.samples
        # pixel幅より多いsampleは、1 pixelにつき1頂点へ間引く。
        point_count = min(samples.size, max(2, int(width)))
        middle = height / 2.0
        amplitude = height / 2.0
        polygon = QPolygonF()
        for index in range(point_count):
            start = index * samples.size // point_count
            value = float(samples[start])
            x = width * index / max(1, point_count - 1)
            y = middle - value * amplitude
            polygon.append(QPointF(x, y))
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 1.5))
        painter.drawPolyline(polygon)
        self._last_point_count = polygon.size()

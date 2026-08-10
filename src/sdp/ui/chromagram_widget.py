"""クロマグラム（12音階の強度）をQPainterで描画するWidget。

クロマ計算・平滑化・タイマーは持たない（VisualizerPanelの責務）。
"""

from PySide6.QtCore import QEvent, QLineF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.chroma import PITCH_CLASS_NAMES, ChromaFrame

NO_SOURCE_MESSAGE = "音声を再生するとクロマグラムを表示します"

_MINIMUM_HEIGHT = 70
_MARGIN = 4.0
_LABEL_HEIGHT = 16.0
_BAR_GAP = 2.0


class ChromagramWidget(QWidget):
    """12音の強度を縦バーで描く。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chromagramWidget")
        self.setAccessibleName("クロマグラム")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: ChromaFrame | None = None
        self._status_text = NO_SOURCE_MESSAGE
        self._last_bar_count = 0

    @property
    def frame(self) -> ChromaFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def last_bar_count(self) -> int:
        return self._last_bar_count

    def set_frame(self, frame: ChromaFrame | None) -> None:
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

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        self._last_bar_count = 0
        frame = self._frame
        if frame is not None and self.width() >= 1:
            self._draw_bars(painter, frame, palette)

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                self.rect().adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                if self._last_bar_count
                else Qt.AlignmentFlag.AlignCenter,
                self._status_text,
            )
        painter.end()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    def _draw_bars(self, painter: QPainter, frame: ChromaFrame, palette: QPalette) -> None:
        width = float(self.width())
        height = float(self.height())
        bar_area_height = max(1.0, height - _MARGIN * 2.0 - _LABEL_HEIGHT)
        bar_width = width / len(PITCH_CLASS_NAMES)

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        painter.drawLine(QLineF(0.0, _MARGIN + bar_area_height, width, _MARGIN + bar_area_height))

        for index, (name, value) in enumerate(zip(PITCH_CLASS_NAMES, frame.values, strict=True)):
            left = index * bar_width
            value_float = float(value)
            if value_float > 0.0:
                fill_height = bar_area_height * min(1.0, max(0.0, value_float))
                painter.fillRect(
                    QRectF(
                        left + _BAR_GAP / 2.0,
                        _MARGIN + bar_area_height - fill_height,
                        max(1.0, bar_width - _BAR_GAP),
                        fill_height,
                    ),
                    palette.brush(QPalette.ColorRole.Highlight),
                )
                self._last_bar_count += 1
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                QRectF(left, _MARGIN + bar_area_height, bar_width, _LABEL_HEIGHT),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                name,
            )

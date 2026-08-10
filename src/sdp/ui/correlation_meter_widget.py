"""位相相関メーターをQPainterで描画するWidget。

相関値（-1〜+1）の計算・平滑化・タイマーは持たない（VisualizerPanelの責務）。
"""

import math

from PySide6.QtCore import QEvent, QLineF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

NO_SOURCE_MESSAGE = "音声を再生すると位相相関を表示します"

_MINIMUM_HEIGHT = 42
_MARGIN = 6.0


class CorrelationMeterWidget(QWidget):
    """-1〜+1の横バーで位相相関を描く。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("correlationMeterWidget")
        self.setAccessibleName("位相相関メーター")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._correlation: float | None = None
        self._status_text = NO_SOURCE_MESSAGE
        self._last_value_drawn = False

    @property
    def correlation(self) -> float | None:
        return self._correlation

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def last_value_drawn(self) -> bool:
        return self._last_value_drawn

    def set_correlation(self, value: float | None) -> None:
        if value is not None:
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError("correlationは有限値である必要があります")
            value = min(1.0, max(-1.0, float(value)))
        if value == self._correlation:
            return
        self._correlation = value
        self.update()

    def set_status_text(self, text: str) -> None:
        if text == self._status_text:
            return
        self._status_text = text
        self.update()

    def clear_value(self, status_text: str = NO_SOURCE_MESSAGE) -> None:
        self._correlation = None
        self._status_text = status_text
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        track = self._track_rect()

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        painter.drawRect(track)
        for value, label in ((-1.0, "-1"), (0.0, "0"), (1.0, "+1")):
            x = self._value_x(value, track)
            painter.drawLine(QLineF(x, track.top(), x, track.bottom()))
            painter.drawText(
                QRectF(x - 14.0, track.bottom() + 1.0, 28.0, 14.0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                label,
            )

        self._last_value_drawn = False
        if self._correlation is not None:
            x = self._value_x(self._correlation, track)
            painter.fillRect(
                QRectF(track.left(), track.top(), x - track.left(), track.height()),
                palette.brush(QPalette.ColorRole.Highlight),
            )
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Text), 2.0))
            painter.drawLine(QLineF(x, track.top() - 1.0, x, track.bottom() + 1.0))
            painter.drawText(
                self.rect().adjusted(6, 2, -6, -2),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                f"相関 {self._correlation:+.2f}",
            )
            self._last_value_drawn = True

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                self.rect().adjusted(6, 2, -6, -2),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                if self._last_value_drawn
                else Qt.AlignmentFlag.AlignCenter,
                self._status_text,
            )
        painter.end()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    def _track_rect(self) -> QRectF:
        return QRectF(
            _MARGIN,
            _MARGIN,
            max(1.0, float(self.width()) - 2.0 * _MARGIN),
            max(8.0, float(self.height()) - 22.0),
        )

    def _value_x(self, value: float, track: QRectF) -> float:
        ratio = (min(1.0, max(-1.0, value)) + 1.0) / 2.0
        return track.left() + track.width() * ratio

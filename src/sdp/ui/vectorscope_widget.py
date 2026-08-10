"""ステレオ・ベクトルスコープ（ゴニオメーター）をQPainterで描画するWidget。

L／Rから作った45度回転座標の点群を描く。点群計算・位相相関・タイマーは
持たない（VisualizerPanelの責務）。
"""

from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.stereo import VectorscopeFrame

NO_SOURCE_MESSAGE = "音声を再生するとステレオ位相を表示します"

_MINIMUM_HEIGHT = 110
_POINT_RADIUS = 1.4


class VectorscopeWidget(QWidget):
    """L／Rの位相関係をLissajous風の点群で描く。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("vectorscopeWidget")
        self.setAccessibleName("ベクトルスコープ")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: VectorscopeFrame | None = None
        self._status_text = NO_SOURCE_MESSAGE
        self._last_point_count = 0

    @property
    def frame(self) -> VectorscopeFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def last_point_count(self) -> int:
        return self._last_point_count

    def set_frame(self, frame: VectorscopeFrame | None) -> None:
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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        area = self._scope_rect()
        center = area.center()
        radius = min(area.width(), area.height()) / 2.0

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        painter.drawEllipse(center, radius, radius)
        painter.drawLine(QLineF(area.left(), center.y(), area.right(), center.y()))
        painter.drawLine(QLineF(center.x(), area.top(), center.x(), area.bottom()))
        # 同相／逆相の目安になる対角線も薄く描く。
        painter.drawLine(QLineF(area.left(), area.bottom(), area.right(), area.top()))
        painter.drawLine(QLineF(area.left(), area.top(), area.right(), area.bottom()))

        self._last_point_count = 0
        frame = self._frame
        if frame is not None and frame.point_count > 0 and radius >= 1.0:
            self._draw_points(painter, frame, center, radius, palette)

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

    def _scope_rect(self) -> QRectF:
        margin = 8.0
        width = float(self.width())
        height = float(self.height())
        side = max(1.0, min(width, height) - margin * 2.0)
        return QRectF((width - side) / 2.0, (height - side) / 2.0, side, side)

    def _draw_points(
        self,
        painter: QPainter,
        frame: VectorscopeFrame,
        center: QPointF,
        radius: float,
        palette: QPalette,
    ) -> None:
        # 点ごとに drawEllipse を呼ぶと30FPS × 最大1,024点でPython→Qt境界を跨ぎ続ける。
        # 丸い点はRoundCapのpen幅で表し、描画呼び出しは drawPoints の1回にまとめる。
        pen = QPen(palette.color(QPalette.ColorRole.Highlight), _POINT_RADIUS * 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # 座標計算はNumPy側でまとめて行い、Pythonのループを座標の組み立てだけに留める。
        x_values = (frame.x * radius).tolist()
        y_values = (frame.y * radius).tolist()
        origin_x = center.x()
        origin_y = center.y()
        points = QPolygonF(
            [
                QPointF(origin_x + x_value, origin_y - y_value)
                for x_value, y_value in zip(x_values, y_values, strict=True)
            ]
        )
        painter.drawPoints(points)
        self._last_point_count = points.size()

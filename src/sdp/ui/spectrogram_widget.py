"""スペクトログラムをQPainterで描画するWidget。

時間×周波数のdB履歴を固定グラデーションで塗る。履歴生成・FFT・タイマーは
持たない（VisualizerPanelの責務）。
"""

import math

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.spectrogram import SpectrogramFrame

NO_SOURCE_MESSAGE = "音声を再生するとスペクトログラムを表示します"

_MINIMUM_HEIGHT = 110


class SpectrogramWidget(QWidget):
    """スペクトログラムの履歴を矩形の集合で描く。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrogramWidget")
        self.setAccessibleName("スペクトログラム")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: SpectrogramFrame | None = None
        self._status_text = NO_SOURCE_MESSAGE
        self._last_cell_count = 0

    @property
    def frame(self) -> SpectrogramFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def last_cell_count(self) -> int:
        return self._last_cell_count

    def set_frame(self, frame: SpectrogramFrame | None) -> None:
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
        self._last_cell_count = 0

        frame = self._frame
        if frame is not None and self.width() >= 1 and self.height() >= 1:
            self._draw_spectrogram(painter, frame)

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                self.rect().adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                if self._last_cell_count
                else Qt.AlignmentFlag.AlignCenter,
                self._status_text,
            )
        painter.end()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    def _draw_spectrogram(self, painter: QPainter, frame: SpectrogramFrame) -> None:
        width = float(self.width())
        height = float(self.height())
        columns = frame.columns
        history = frame.history
        band_count = frame.band_count
        if history < 1 or band_count < 1:
            return

        column_count = min(history, max(1, int(width)))
        row_count = min(band_count, max(1, int(height)))
        cell_width = width / column_count
        cell_height = height / row_count
        db_floor = frame.db_floor
        span = -db_floor
        for x_index in range(column_count):
            source_column = x_index * history // column_count
            for y_index in range(row_count):
                start = y_index * band_count // row_count
                stop = max(start + 1, (y_index + 1) * band_count // row_count)
                # 高域を上へ描くため、source bandは上下反転して取る。
                level = float(columns[source_column, band_count - stop : band_count - start].max())
                ratio = (level - db_floor) / span if span > 0.0 else 0.0
                if ratio <= 0.0:
                    continue
                painter.fillRect(
                    QRectF(
                        x_index * cell_width,
                        y_index * cell_height,
                        math.ceil(cell_width),
                        math.ceil(cell_height),
                    ),
                    _heat_color(ratio),
                )
                self._last_cell_count += 1


def _heat_color(ratio: float) -> QColor:
    """0〜1の強度を黒→青→紫→橙→黄の固定グラデーションへ写す。"""
    value = min(1.0, max(0.0, ratio))
    if value < 0.33:
        local = value / 0.33
        return QColor(0, round(40 + local * 70), round(80 + local * 140))
    if value < 0.66:
        local = (value - 0.33) / 0.33
        return QColor(round(local * 180), round(110 - local * 40), round(220 - local * 80))
    local = (value - 0.66) / 0.34
    return QColor(180 + round(local * 75), 70 + round(local * 180), round(140 - local * 120))

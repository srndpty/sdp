"""リアルタイムスペクトラムをQPainterで一括描画するWidget。

band ごとに子WidgetやQGraphicsItemを作らず、1回のpaintEventで全barを描く。
FFT・平滑化・タイマーは持たない（SpectrumPanelの責務）。
"""

import math

from PySide6.QtCore import QEvent, QLineF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.spectrum import SPECTRUM_DB_FLOOR, SpectrumFrame

NO_SOURCE_MESSAGE = "音声を再生するとスペクトラムを表示します"

GRID_DB_STEPS = (-20.0, -40.0, -60.0, -80.0)
"""dB基準線。色だけでなく位置と文字で状態を伝える。"""

_MINIMUM_HEIGHT = 65
_BAR_GAP_PX = 1.0


class SpectrumWidget(QWidget):
    """band別dBを縦棒で描く。マウス操作もフォーカスも持たない。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumWidget")
        self.setAccessibleName("スペクトラム")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 可視化専用のためキーボード操作もマウス操作も受け取らない。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: SpectrumFrame | None = None
        self._db_floor = SPECTRUM_DB_FLOOR
        self._status_text = NO_SOURCE_MESSAGE
        self._last_bar_count = 0

    # -- 状態 ---------------------------------------------------------------

    @property
    def frame(self) -> SpectrumFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def db_floor(self) -> float:
        return self._db_floor

    @property
    def last_bar_count(self) -> int:
        """直前のpaintEventで描いたbarの数（性能テストと間引き確認用）。"""
        return self._last_bar_count

    def set_db_floor(self, db_floor: float) -> None:
        if isinstance(db_floor, bool) or not math.isfinite(db_floor) or db_floor >= 0.0:
            raise ValueError("db_floorは負の有限値である必要があります")
        if db_floor == self._db_floor:
            return
        self._db_floor = db_floor
        self.update()

    def set_frame(self, frame: SpectrumFrame | None) -> None:
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
        """source切替・停止時に前sourceのフレームを即時破棄する。"""
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

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        for level_db in GRID_DB_STEPS:
            if level_db <= self._db_floor:
                continue
            y = self._level_y(level_db, height)
            painter.drawLine(QLineF(0.0, y, width, y))

        self._last_bar_count = 0
        frame = self._frame
        if frame is not None and frame.band_count > 0 and width >= 1.0:
            self._draw_bars(painter, frame, width, height, palette)

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

    # -- 内部 ---------------------------------------------------------------

    def _draw_bars(
        self,
        painter: QPainter,
        frame: SpectrumFrame,
        width: float,
        height: float,
        palette: QPalette,
    ) -> None:
        # pixel幅に対してband数が多すぎる場合は、隣接bandの最大値へ間引く。
        bar_count = min(frame.band_count, max(1, int(width)))
        levels = frame.levels_db
        brush = palette.brush(QPalette.ColorRole.Highlight)
        bar_width = width / bar_count
        baseline = self._level_y(self._db_floor, height)
        for index in range(bar_count):
            start = index * frame.band_count // bar_count
            stop = max(start + 1, (index + 1) * frame.band_count // bar_count)
            level = float(levels[start:stop].max())
            if level <= self._db_floor:
                continue
            top = self._level_y(level, height)
            left = index * bar_width
            painter.fillRect(
                QRectF(left, top, max(1.0, bar_width - _BAR_GAP_PX), baseline - top),
                brush,
            )
            self._last_bar_count += 1

    def _level_y(self, level_db: float, height: float) -> float:
        """dB値を上端0dB・下端floorのy座標へ写す。"""
        span = -self._db_floor
        ratio = (level_db - self._db_floor) / span if span > 0.0 else 0.0
        return height * (1.0 - min(1.0, max(0.0, ratio)))

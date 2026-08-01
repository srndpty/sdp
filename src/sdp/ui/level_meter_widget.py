"""L／RのPeak・RMS・Peak holdをQPainterで一括描画するWidget。

チャンネルや目盛ごとに子Widgetを作らず、1回のpaintEventで全要素を描く。
Peak／RMSの計算、Peak hold、タイマーは持たない（SpectrumPanelの責務）。

表示するのは音量・ミュート適用**前**の入力信号レベルであり、出力音量計ではない。
"""

import math

from PySide6.QtCore import QEvent, QLineF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.level import LEVEL_DB_FLOOR, StereoLevelFrame

NO_SOURCE_MESSAGE = "音声を再生するとレベルを表示します"

GRID_DB_STEPS = (-60.0, -40.0, -20.0, -6.0)
"""dB基準線と目盛の文字。状態を色だけで伝えないため位置と数値を併記する。"""

_MINIMUM_HEIGHT = 39
_MARGIN = 3.0
_LABEL_WIDTH = 14.0
_ROW_GAP = 3.0
_SCALE_HEIGHT = 6.0
_MAX_BAR_HEIGHT = 10.0
_PEAK_PEN_WIDTH = 1.0
_PEAK_HOLD_PEN_WIDTH = 3.0


class LevelMeterWidget(QWidget):
    """左右2本の横バーでRMS（塗り）、Peak（細線）、Peak hold（太線）を描く。

    マウス操作もフォーカスも持たない（シークは波形側の責務）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("levelMeterWidget")
        self.setAccessibleName("レベルメーター")
        self.setMinimumHeight(_MINIMUM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 可視化専用のためキーボード操作もマウス操作も受け取らない。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._frame: StereoLevelFrame | None = None
        self._db_floor = LEVEL_DB_FLOOR
        self._status_text = NO_SOURCE_MESSAGE
        self._last_rms_bar_count = 0
        self._last_peak_mark_count = 0
        self._last_peak_hold_mark_count = 0

    # -- 状態 ---------------------------------------------------------------

    @property
    def frame(self) -> StereoLevelFrame | None:
        return self._frame

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def db_floor(self) -> float:
        return self._db_floor

    @property
    def last_rms_bar_count(self) -> int:
        """直前のpaintEventで描いたRMSバーの数（0〜2）。"""
        return self._last_rms_bar_count

    @property
    def last_peak_mark_count(self) -> int:
        """直前のpaintEventで描いたPeak線の数（0〜2）。"""
        return self._last_peak_mark_count

    @property
    def last_peak_hold_mark_count(self) -> int:
        """直前のpaintEventで描いたPeak hold線の数（0〜2）。"""
        return self._last_peak_hold_mark_count

    def set_db_floor(self, db_floor: float) -> None:
        if isinstance(db_floor, bool) or not math.isfinite(db_floor) or db_floor >= 0.0:
            raise ValueError("db_floorは負の有限値である必要があります")
        if db_floor == self._db_floor:
            return
        self._db_floor = db_floor
        self.update()

    def set_frame(self, frame: StereoLevelFrame | None) -> None:
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
        """source切替・停止時に前sourceのレベルとPeak holdを即時破棄する。"""
        self._frame = None
        self._status_text = status_text
        self.update()

    # -- 描画 ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        self._last_rms_bar_count = 0
        self._last_peak_mark_count = 0
        self._last_peak_hold_mark_count = 0

        track = self._track_rect()
        if track.width() >= 1.0 and track.height() >= 2.0:
            self._draw_scale(painter, track, palette)
            frame = self._frame
            if frame is not None:
                self._draw_channels(painter, track, palette, frame)

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(
                self.rect().adjusted(6, 2, -6, -2),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
                if self._frame is not None
                else Qt.AlignmentFlag.AlignCenter,
                self._status_text,
            )
        painter.end()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    # -- 内部 ---------------------------------------------------------------

    def _track_rect(self) -> QRectF:
        """L／Rバーを描く領域（ラベル列と目盛行を除く）。"""
        width = float(self.width())
        height = float(self.height())
        left = _MARGIN + _LABEL_WIDTH
        available = height - 2.0 * _MARGIN - _SCALE_HEIGHT - _ROW_GAP
        bar_height = min(_MAX_BAR_HEIGHT, max(2.0, available / 2.0))
        return QRectF(left, _MARGIN, max(0.0, width - left - _MARGIN), bar_height * 2.0 + _ROW_GAP)

    def _draw_scale(self, painter: QPainter, track: QRectF, palette: QPalette) -> None:
        """dB基準線と目盛の数値を描く。"""
        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        for level_db in GRID_DB_STEPS:
            if level_db <= self._db_floor:
                continue
            x = self._level_x(level_db, track)
            painter.drawLine(QLineF(x, track.top(), x, track.bottom()))

        font = painter.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() - 2.0))
        painter.setFont(font)
        painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
        scale_top = track.bottom() + 1.0
        for level_db in (*GRID_DB_STEPS, 0.0):
            if level_db < self._db_floor:
                continue
            x = self._level_x(level_db, track)
            text = f"{level_db:.0f}"
            painter.drawText(
                QRectF(x - 14.0, scale_top, 28.0, _SCALE_HEIGHT),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                text,
            )

    def _draw_channels(
        self,
        painter: QPainter,
        track: QRectF,
        palette: QPalette,
        frame: StereoLevelFrame,
    ) -> None:
        bar_height = (track.height() - _ROW_GAP) / 2.0
        channels = (
            ("L", frame.left_rms_db, frame.left_peak_db, frame.left_peak_hold_db),
            ("R", frame.right_rms_db, frame.right_peak_db, frame.right_peak_hold_db),
        )
        for index, (label, rms_db, peak_db, hold_db) in enumerate(channels):
            top = track.top() + index * (bar_height + _ROW_GAP)
            row = QRectF(track.left(), top, track.width(), bar_height)

            painter.setPen(QPen(palette.color(QPalette.ColorRole.Text), 1.0))
            painter.drawText(
                QRectF(_MARGIN, top, _LABEL_WIDTH, bar_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
            painter.drawRect(row)

            if rms_db > self._db_floor:
                # RMSは塗りつぶしバー（Peakと色だけで区別しない）。
                fill_width = self._level_x(rms_db, track) - row.left()
                painter.fillRect(
                    QRectF(row.left(), row.top(), max(1.0, fill_width), row.height()),
                    palette.brush(QPalette.ColorRole.Highlight),
                )
                self._last_rms_bar_count += 1

            if peak_db > self._db_floor:
                # Peakは細い縦線。
                painter.setPen(QPen(palette.color(QPalette.ColorRole.Text), _PEAK_PEN_WIDTH))
                x = self._level_x(peak_db, track)
                painter.drawLine(QLineF(x, row.top(), x, row.bottom()))
                self._last_peak_mark_count += 1

            if hold_db > self._db_floor:
                # Peak holdは太い短線。
                painter.setPen(QPen(palette.color(QPalette.ColorRole.Link), _PEAK_HOLD_PEN_WIDTH))
                x = self._level_x(hold_db, track)
                painter.drawLine(QLineF(x, row.top() + 1.0, x, row.bottom() - 1.0))
                self._last_peak_hold_mark_count += 1

    def _level_x(self, level_db: float, track: QRectF) -> float:
        """dB値を左端floor・右端0dBのx座標へ写す。"""
        span = -self._db_floor
        ratio = (level_db - self._db_floor) / span if span > 0.0 else 0.0
        return track.left() + track.width() * min(1.0, max(0.0, ratio))

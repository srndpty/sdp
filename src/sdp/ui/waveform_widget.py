"""中央固定の追従波形を描画し、マウス操作をシーク要求へ変換するWidget。"""

from PySide6.QtCore import QEvent, QLineF, Qt, Signal
from PySide6.QtGui import (
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.waveform import WaveformData
from sdp.core.analysis.waveform_projection import (
    WAVEFORM_WINDOW_MS,
    WaveformColumns,
    project_waveform,
    seek_position_from_x,
)

NO_SOURCE_MESSAGE = "音声ファイルを開くと波形を表示します"


class WaveformWidget(QWidget):
    """波形の描画と、release時に1回だけ確定するシーク入力を担当する。"""

    seek_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("waveformWidget")
        self.setAccessibleName("波形")
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._source_available = False
        self._data: WaveformData | None = None
        self._position_ms = 0
        self._duration_ms = 0
        self._status_text = NO_SOURCE_MESSAGE
        self._dragging = False
        self._drag_center_ms: int | None = None
        self._preview_x: float | None = None
        self._preview_position_ms: int | None = None
        self._projection_key: tuple[int, int, int] | None = None
        self._columns: WaveformColumns | None = None
        self._projection_count = 0
        self._last_waveform_line_count = 0

    @property
    def waveform_data(self) -> WaveformData | None:
        return self._data

    @property
    def position_ms(self) -> int:
        return self._position_ms

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def preview_position_ms(self) -> int | None:
        return self._preview_position_ms

    @property
    def projection_count(self) -> int:
        return self._projection_count

    @property
    def last_waveform_line_count(self) -> int:
        return self._last_waveform_line_count

    @property
    def status_text(self) -> str:
        return self._status_text

    def reset_for_source(self, available: bool) -> None:
        """source切替時に前sourceの表示・位置・操作を即時解除する。"""
        self.cancel_drag()
        self._source_available = available
        self._data = None
        self._position_ms = 0
        self._duration_ms = 0
        self._status_text = "波形を解析中…" if available else NO_SOURCE_MESSAGE
        self._invalidate_projection()
        self.update()

    def set_waveform_data(self, data: WaveformData | None) -> None:
        if data is self._data:
            return
        self._data = data
        self._invalidate_projection()
        self.update()

    def set_position(self, position_ms: int) -> None:
        position = max(0, position_ms)
        if position == self._position_ms:
            return
        self._position_ms = position
        if not self._dragging:
            self._invalidate_projection()
        self.update()

    def set_duration(self, duration_ms: int) -> None:
        duration = max(0, duration_ms)
        if duration == self._duration_ms:
            return
        self._duration_ms = duration
        if duration <= 0:
            self.cancel_drag()
        self.update()

    def set_status_text(self, text: str) -> None:
        if text == self._status_text:
            return
        self._status_text = text
        self.update()

    def cancel_drag(self) -> None:
        if not self._dragging and self._preview_x is None:
            return
        self._dragging = False
        self._drag_center_ms = None
        self._preview_x = None
        self._preview_position_ms = None
        self._invalidate_projection()
        self.update()

    def projected_columns(self) -> WaveformColumns | None:
        """現在の表示条件に対応する列を返す。描画と性能テストで共有する。"""
        if self._data is None or self.width() <= 0:
            return None
        center_ms = self._display_center_ms()
        key = (id(self._data), center_ms, self.width())
        if key != self._projection_key:
            self._columns = project_waveform(
                self._data,
                center_ms=center_ms,
                window_ms=WAVEFORM_WINDOW_MS,
                pixel_width=self.width(),
            )
            self._projection_key = key
            self._projection_count += 1
        return self._columns

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.brush(QPalette.ColorRole.Base))
        center_y = self.height() / 2.0

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1.0))
        painter.drawLine(QLineF(0.0, center_y, float(self.width()), center_y))

        columns = self.projected_columns()
        self._last_waveform_line_count = 0
        if columns is not None:
            amplitude_height = max(1.0, self.height() * 0.42)
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Text), 1.0))
            for x in range(columns.minimum.size):
                if not columns.valid[x]:
                    continue
                top = center_y - float(columns.maximum[x]) * amplitude_height
                bottom = center_y - float(columns.minimum[x]) * amplitude_height
                painter.drawLine(QLineF(x + 0.5, top, x + 0.5, bottom))
                self._last_waveform_line_count += 1

        painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 2.0))
        center_x = self.width() / 2.0
        painter.drawLine(QLineF(center_x, 0.0, center_x, float(self.height())))

        if self._preview_x is not None and self._preview_position_ms is not None:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Link), 2.0))
            painter.drawLine(QLineF(self._preview_x, 0.0, self._preview_x, float(self.height())))
            painter.drawText(
                self.rect().adjusted(6, 6, -6, -6),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                _format_position(self._preview_position_ms),
            )

        if self._status_text:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.PlaceholderText), 1.0))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status_text)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is not Qt.MouseButton.LeftButton or not self._can_seek():
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._dragging = True
        self._drag_center_ms = self._position_ms
        self._update_preview(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        self._update_preview(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() is not Qt.MouseButton.LeftButton or not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._update_preview(event.position().x())
        position = self._preview_position_ms
        self._dragging = False
        self._drag_center_ms = None
        self._preview_x = None
        self._preview_position_ms = None
        self._invalidate_projection()
        self.update()
        if position is not None and self._can_seek():
            self.seek_requested.emit(position)
        event.accept()

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._invalidate_projection()
        super().resizeEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_drag()
        super().hideEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() is QEvent.Type.EnabledChange and not self.isEnabled():
            self.cancel_drag()
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.PaletteChange):
            self.update()
        super().changeEvent(event)

    def _can_seek(self) -> bool:
        return (
            self._source_available
            and self.isEnabled()
            and self._duration_ms > 0
            and self.width() > 0
        )

    def _display_center_ms(self) -> int:
        return self._drag_center_ms if self._drag_center_ms is not None else self._position_ms

    def _update_preview(self, x: float) -> None:
        if not self._can_seek():
            self.cancel_drag()
            return
        center_ms = self._display_center_ms()
        self._preview_x = min(float(self.width()), max(0.0, x))
        self._preview_position_ms = seek_position_from_x(
            self._preview_x,
            self.width(),
            center_ms=center_ms,
            duration_ms=self._duration_ms,
        )
        self.update()

    def _invalidate_projection(self) -> None:
        self._projection_key = None
        self._columns = None


def _format_position(position_ms: int) -> str:
    total_seconds = max(0, position_ms) // 1_000
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

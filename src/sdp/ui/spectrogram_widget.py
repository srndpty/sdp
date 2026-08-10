"""スペクトログラムをQPainterで描画するWidget。

時間×周波数のdB履歴を固定グラデーションで塗る。履歴生成・FFT・タイマーは
持たない（VisualizerPanelの責務）。

セルごとに ``fillRect`` を呼ぶと30FPS × 数千セルでGUIスレッドを圧迫するため、
強度への間引きは :func:`~sdp.core.analysis.spectrogram.spectrogram_cells`
（NumPy、Qt非依存）へ任せ、ここでは256色のカラーテーブルを持つ ``QImage`` を
1枚作って ``drawImage`` を1回だけ呼ぶ。
"""

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from sdp.core.analysis.spectrogram import CELL_LEVEL_MAX, SpectrogramFrame, spectrogram_cells

NO_SOURCE_MESSAGE = "音声を再生するとスペクトログラムを表示します"

_MINIMUM_HEIGHT = 110


class SpectrogramWidget(QWidget):
    """スペクトログラムの履歴を1枚のQImageとして描く。"""

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
        """直近の描画で色を塗ったセル数（floor以下のセルは数えない）。"""
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
        width = self.width()
        height = self.height()
        cells = spectrogram_cells(frame, column_count=max(1, width), row_count=max(1, height))
        if cells.painted_count == 0:
            return

        # bytes 化した時点でNumPy配列と縁が切れるため、QImageが参照する寿命を気にしない。
        # 走査線は4byte境界へ揃っている必要があるため、行の埋め草込みの幅を渡す。
        data = cells.indices.tobytes()
        image = QImage(
            data,
            cells.columns,
            cells.rows,
            cells.row_stride,
            QImage.Format.Format_Indexed8,
        )
        image.setColorTable(_COLOR_TABLE)
        painter.drawImage(QRectF(0.0, 0.0, float(width), float(height)), image)
        self._last_cell_count = cells.painted_count


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


def _build_color_table() -> list[int]:
    """強度0〜255をARGB値へ写す固定テーブル（強度0は透明＝背景のまま）。"""
    table = [QColor(0, 0, 0, 0).rgba()]
    table.extend(
        _heat_color(level / CELL_LEVEL_MAX).rgba() for level in range(1, CELL_LEVEL_MAX + 1)
    )
    return table


_COLOR_TABLE = _build_color_table()
"""256色のカラーテーブル。1フレームごとにQColorを作らないよう、import時に1回だけ作る。"""

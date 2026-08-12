"""追加ビジュアライザーWidgetのQPainter描画・状態表示を検証する。"""

import numpy as np
import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget
from pytestqt.qtbot import QtBot

from sdp.core.analysis.chroma import compute_chroma
from sdp.core.analysis.oscilloscope import compute_oscilloscope
from sdp.core.analysis.spectrogram import SpectrogramProcessor
from sdp.core.analysis.stereo import VectorscopeFrame, compute_vectorscope
from sdp.ui.chromagram_widget import ChromagramWidget
from sdp.ui.correlation_meter_widget import CorrelationMeterWidget
from sdp.ui.oscilloscope_widget import OscilloscopeWidget
from sdp.ui.spectrogram_widget import SpectrogramWidget
from sdp.ui.vectorscope_widget import VectorscopeWidget

SAMPLE_RATE = 48_000


def tone(frequency: float = 1_000.0, frames: int = 4_096) -> np.ndarray:
    """検証用の正弦波を作る。"""
    t = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    return (0.5 * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def prepare[WidgetT: QWidget](
    widget: WidgetT, qtbot: QtBot, *, width: int = 400, height: int = 120
) -> WidgetT:
    """Widgetを表示してpaintEventが走れる状態にする。"""
    qtbot.addWidget(widget)
    widget.resize(width, height)
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def repaint(widget: QWidget, qtbot: QtBot) -> None:
    widget.repaint()
    qtbot.wait(20)


@pytest.mark.parametrize(
    "widget_type",
    [
        OscilloscopeWidget,
        VectorscopeWidget,
        CorrelationMeterWidget,
        SpectrogramWidget,
        ChromagramWidget,
    ],
)
def test_widget_common_contract(widget_type: type[QWidget], qtbot: QtBot) -> None:
    """追加Widgetは固定高・フォーカスなし・子QLabelなしで描画する。"""
    widget = widget_type()
    prepare(widget, qtbot)

    assert widget.sizePolicy().horizontalPolicy() is QSizePolicy.Policy.Expanding
    assert widget.sizePolicy().verticalPolicy() is QSizePolicy.Policy.Fixed
    assert widget.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert widget.findChildren(QLabel) == []
    repaint(widget, qtbot)


def test_oscilloscope_paints_polyline(qtbot: QtBot) -> None:
    """オシロスコープは波形頂点を描く。"""
    widget = prepare(OscilloscopeWidget(), qtbot)
    widget.set_frame(compute_oscilloscope(tone()))
    repaint(widget, qtbot)

    assert widget.last_point_count > 0
    assert widget.status_text

    widget.set_status_text("")
    widget.clear_frame("停止中")
    repaint(widget, qtbot)
    assert widget.last_point_count == 0


def test_vectorscope_paints_points(qtbot: QtBot) -> None:
    """ベクトルスコープはL／R点群を描く。"""
    widget = prepare(VectorscopeWidget(), qtbot)
    left = tone(1_000.0)
    right = tone(1_100.0)
    widget.set_frame(compute_vectorscope(left, right))
    repaint(widget, qtbot)

    assert widget.last_point_count > 0


def test_vectorscope_fits_a_square_into_a_wide_widget(qtbot: QtBot) -> None:
    """横長の領域でも、描画は短辺基準の正方形へ収める（横に引き延ばさない）。"""
    widget = prepare(VectorscopeWidget(), qtbot, width=240, height=80)
    # 左右の端に届く点（逆相成分が最大）を与える。
    widget.set_frame(
        VectorscopeFrame(
            x=np.array([-1.0, 1.0], dtype=np.float32),
            y=np.array([0.0, 0.0], dtype=np.float32),
        )
    )
    widget.set_status_text("")
    repaint(widget, qtbot)

    painted = _painted_bounds(widget)
    assert painted.width() <= widget.height()
    # 正方形は領域の中央にある（左右の余白がほぼ等しい）。
    assert painted.left() == pytest.approx(widget.width() - painted.right(), abs=2)


def _painted_bounds(widget: QWidget) -> QRect:
    """背景色と異なる pixel の外接矩形。"""
    image = widget.grab().toImage()
    background = image.pixel(0, 0)
    painted = [
        (x, y)
        for x in range(image.width())
        for y in range(image.height())
        if image.pixel(x, y) != background
    ]
    assert painted
    xs = [x for x, _ in painted]
    ys = [y for _, y in painted]
    return QRect(min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def test_correlation_meter_paints_value(qtbot: QtBot) -> None:
    """位相相関メーターは値を描く。"""
    widget = prepare(CorrelationMeterWidget(), qtbot, height=50)
    widget.set_correlation(0.5)
    repaint(widget, qtbot)

    assert widget.last_value_drawn
    assert widget.correlation == pytest.approx(0.5)

    widget.clear_value("停止中")
    repaint(widget, qtbot)
    assert not widget.last_value_drawn


def test_spectrogram_paints_cells(qtbot: QtBot) -> None:
    """スペクトログラムは履歴セルを描く。"""
    widget = prepare(SpectrogramWidget(), qtbot)
    processor = SpectrogramProcessor(history=8)
    frame = processor.process(tone(), SAMPLE_RATE)
    widget.set_frame(frame)
    repaint(widget, qtbot)

    assert widget.last_cell_count > 0


def test_chromagram_paints_bars(qtbot: QtBot) -> None:
    """クロマグラムは音名付きバーを描く。"""
    widget = prepare(ChromagramWidget(), qtbot)
    widget.set_frame(compute_chroma(tone(440.0), SAMPLE_RATE))
    repaint(widget, qtbot)

    assert widget.last_bar_count > 0


@pytest.mark.parametrize("width", [400, 401, 403])
def test_spectrogram_image_is_not_skewed_at_any_width(width: int, qtbot: QtBot) -> None:
    """4の倍数でない幅でも、画像の走査線がずれず一様な色で描かれる。

    強度が全セル同じフレームなので、走査線がずれていれば列ごとに色が変わる。
    """
    widget = prepare(SpectrogramWidget(), qtbot, width=width, height=64)
    processor = SpectrogramProcessor(history=8)
    frame = processor.process(tone(), SAMPLE_RATE)
    for _ in range(7):
        frame = processor.process(tone(), SAMPLE_RATE)
    widget.set_frame(frame)
    widget.set_status_text("")
    repaint(widget, qtbot)

    image = widget.grab().toImage()
    row = image.height() // 2
    colors = {image.pixelColor(x, row).rgb() for x in range(4, image.width() - 4)}

    assert widget.last_cell_count > 0
    assert len(colors) == 1

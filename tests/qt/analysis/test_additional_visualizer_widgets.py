"""追加ビジュアライザーWidgetのQPainter描画・状態表示を検証する。"""

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget
from pytestqt.qtbot import QtBot

from sdp.core.analysis.chroma import compute_chroma
from sdp.core.analysis.oscilloscope import compute_oscilloscope
from sdp.core.analysis.spectrogram import SpectrogramProcessor
from sdp.core.analysis.stereo import compute_vectorscope
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

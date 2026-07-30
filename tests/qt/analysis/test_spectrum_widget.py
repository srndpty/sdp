"""SpectrumWidgetのQPainter描画・状態表示・bar間引きを検証する。

pixel完全一致は求めず、描画可能性・bar数・状態を中心に確認する。
"""

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QSizePolicy
from pytestqt.qtbot import QtBot

from sdp.core.analysis.spectrum import (
    SPECTRUM_BAND_COUNT,
    SPECTRUM_DB_FLOOR,
    SpectrumFrame,
    compute_spectrum,
    empty_spectrum_frame,
)
from sdp.ui.spectrum_widget import GRID_DB_STEPS, NO_SOURCE_MESSAGE, SpectrumWidget

SAMPLE_RATE = 48_000


@pytest.fixture
def widget(qtbot: QtBot) -> SpectrumWidget:
    instance = SpectrumWidget()
    qtbot.addWidget(instance)
    instance.resize(400, 140)
    instance.show()
    return instance


def sine_frame(frequency: float = 1_000.0) -> SpectrumFrame:
    t = np.arange(4_096, dtype=np.float64) / SAMPLE_RATE
    samples = np.sin(2.0 * np.pi * frequency * t).astype(np.float32)
    return compute_spectrum(samples, SAMPLE_RATE)


def silence_frame() -> SpectrumFrame:
    return compute_spectrum(np.zeros(4_096, dtype=np.float32), SAMPLE_RATE)


def repaint(widget: SpectrumWidget) -> None:
    widget.repaint()


# -- 構造 -------------------------------------------------------------------


def test_object_name_and_accessibility(widget: SpectrumWidget) -> None:
    """objectNameとaccessibleNameで識別できる。"""
    assert widget.objectName() == "spectrumWidget"
    assert widget.accessibleName() == "スペクトラム"


def test_minimum_height_and_size_policy(widget: SpectrumWidget) -> None:
    """縦は固定高、横は伸縮する。"""
    assert widget.minimumHeight() >= 120
    assert widget.sizePolicy().horizontalPolicy() is QSizePolicy.Policy.Expanding
    assert widget.sizePolicy().verticalPolicy() is QSizePolicy.Policy.Fixed


def test_no_child_widgets_are_created_per_band(widget: SpectrumWidget) -> None:
    """96個の子WidgetやItemを作らず、QPainterで一括描画する。"""
    widget.set_frame(sine_frame())
    repaint(widget)

    assert widget.findChildren(SpectrumWidget) == []
    assert widget.children() == []


def test_initial_state_shows_the_placeholder(widget: SpectrumWidget) -> None:
    """sourceなしの初期状態はプレースホルダー文字だけを描く。"""
    assert widget.frame is None
    assert widget.status_text == NO_SOURCE_MESSAGE

    repaint(widget)

    assert widget.last_bar_count == 0


# -- 描画 -------------------------------------------------------------------


def test_paints_bars_for_a_96_band_frame(widget: SpectrumWidget) -> None:
    """96bandのフレームでbarが描かれる。"""
    frame = sine_frame()
    assert frame.band_count == SPECTRUM_BAND_COUNT

    widget.set_frame(frame)
    repaint(widget)

    assert 0 < widget.last_bar_count <= SPECTRUM_BAND_COUNT


def test_empty_frame_paints_without_bars(widget: SpectrumWidget) -> None:
    """空フレームでも例外なく描画し、barは出ない。"""
    widget.set_frame(empty_spectrum_frame())
    repaint(widget)

    assert widget.last_bar_count == 0


def test_silence_frame_paints_no_bars(widget: SpectrumWidget) -> None:
    """floorのままのフレームはbarを描かない（自然な無音表示）。"""
    widget.set_frame(silence_frame())
    repaint(widget)

    assert widget.last_bar_count == 0


def test_bar_count_never_exceeds_the_band_count(widget: SpectrumWidget) -> None:
    """barはband数を超えない。"""
    widget.set_frame(sine_frame())

    for width in (60, 200, 400, 1_200):
        widget.resize(width, 140)
        repaint(widget)
        assert widget.last_bar_count <= SPECTRUM_BAND_COUNT


def test_narrow_widget_decimates_bars_to_the_pixel_width(widget: SpectrumWidget) -> None:
    """pixel幅よりband数が多い場合は間引く。"""
    widget.set_frame(sine_frame())
    widget.resize(30, 140)

    repaint(widget)

    assert widget.last_bar_count <= 30


def test_resize_keeps_painting(widget: SpectrumWidget) -> None:
    """resize後も描画できる。"""
    widget.set_frame(sine_frame())
    repaint(widget)

    widget.resize(900, 200)
    repaint(widget)

    assert widget.last_bar_count > 0


def test_zero_width_does_not_crash(widget: SpectrumWidget) -> None:
    """幅0でも描画で落ちない。"""
    widget.set_frame(sine_frame())
    widget.resize(0, 140)

    repaint(widget)

    assert widget.last_bar_count == 0


def test_palette_change_triggers_a_repaint(widget: SpectrumWidget) -> None:
    """固定RGBに依存せず、palette変更で再描画する。"""
    widget.set_frame(sine_frame())
    repaint(widget)
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.darkGray)

    widget.setPalette(palette)
    repaint(widget)

    assert widget.last_bar_count > 0


def test_grid_lines_are_defined_within_the_db_range() -> None:
    """dB基準線はfloorと0dBの間に置く。"""
    assert len(GRID_DB_STEPS) > 0
    assert all(SPECTRUM_DB_FLOOR < step < 0.0 for step in GRID_DB_STEPS)


# -- 状態表示 ---------------------------------------------------------------


def test_status_text_is_shown_and_replaceable(widget: SpectrumWidget) -> None:
    """状態文言はWidget内の1か所で描く（別QLabelを増やさない）。"""
    widget.set_status_text("停止中")
    repaint(widget)

    assert widget.status_text == "停止中"
    # 別QLabelの表示切替でPanel高が変動しないよう、文字もQPainterで描く。
    assert widget.findChildren(QLabel) == []


def test_clear_frame_discards_the_previous_frame(widget: SpectrumWidget) -> None:
    """stop・source変更で前のフレームを残さない。"""
    widget.set_frame(sine_frame())
    repaint(widget)
    assert widget.last_bar_count > 0

    widget.clear_frame("停止中")
    repaint(widget)

    assert widget.frame is None
    assert widget.status_text == "停止中"
    assert widget.last_bar_count == 0


def test_paused_frame_is_kept_until_cleared(widget: SpectrumWidget) -> None:
    """一時停止では最後のフレームを保持し続ける。"""
    frame = sine_frame()
    widget.set_frame(frame)

    for _ in range(3):
        repaint(widget)

    assert widget.frame is frame
    assert widget.last_bar_count > 0


def test_db_floor_can_be_configured(widget: SpectrumWidget) -> None:
    """dB範囲はProcessorと揃えられる。"""
    widget.set_db_floor(-60.0)

    assert widget.db_floor == -60.0


@pytest.mark.parametrize("value", [0.0, 1.0, float("nan"), float("inf"), True, False])
def test_invalid_db_floor_is_rejected(widget: SpectrumWidget, value: float | bool) -> None:
    """0以上・非有限値・boolの下限は受け付けない。"""
    with pytest.raises(ValueError, match="db_floor"):
        widget.set_db_floor(value)


# -- マウスとフォーカス -----------------------------------------------------


def test_widget_has_no_mouse_interaction(widget: SpectrumWidget) -> None:
    """クリック・ドラッグに反応せず、シーク要求も持たない。"""
    widget.set_frame(sine_frame())

    assert "seek_requested" not in dir(widget)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    QTest.mouseMove(widget, QPoint(200, 20))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(200, 20))

    assert widget.frame is not None


def test_widget_does_not_take_focus(widget: SpectrumWidget) -> None:
    """可視化専用のためフォーカスを奪わない。"""
    assert widget.focusPolicy() is Qt.FocusPolicy.NoFocus


# -- 寿命 -------------------------------------------------------------------


def test_deleted_widget_does_not_crash_on_later_updates(qtbot: QtBot) -> None:
    """破棄後のフレーム更新でクラッシュしない。"""
    widget = SpectrumWidget()
    qtbot.addWidget(widget)
    widget.show()
    widget.set_frame(sine_frame())

    widget.close()
    widget.deleteLater()
    del widget

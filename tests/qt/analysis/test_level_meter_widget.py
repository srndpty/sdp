"""LevelMeterWidgetのQPainter描画・状態表示・L／R別描画を検証する。

pixel完全一致は求めず、描画可能性・要素数・状態を中心に確認する。
"""

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget
from pytestqt.qtbot import QtBot

from sdp.core.analysis.level import (
    LEVEL_DB_FLOOR,
    LevelProcessor,
    StereoLevelFrame,
    silent_level_frame,
)
from sdp.ui.level_meter_widget import GRID_DB_STEPS, NO_SOURCE_MESSAGE, LevelMeterWidget


@pytest.fixture
def widget(qtbot: QtBot) -> LevelMeterWidget:
    instance = LevelMeterWidget()
    qtbot.addWidget(instance)
    instance.resize(400, 90)
    instance.show()
    return instance


def frame(
    *,
    left_peak: float = -6.0,
    right_peak: float = -12.0,
    left_rms: float = -14.0,
    right_rms: float = -20.0,
    left_hold: float = -3.0,
    right_hold: float = -9.0,
) -> StereoLevelFrame:
    return StereoLevelFrame(
        left_peak_db=left_peak,
        right_peak_db=right_peak,
        left_rms_db=left_rms,
        right_rms_db=right_rms,
        left_peak_hold_db=left_hold,
        right_peak_hold_db=right_hold,
    )


# -- 構造 -------------------------------------------------------------------


def test_object_name_and_accessibility(widget: LevelMeterWidget) -> None:
    """objectNameとaccessibleNameで識別できる。"""
    assert widget.objectName() == "levelMeterWidget"
    assert widget.accessibleName() == "レベルメーター"


def test_minimum_height_and_size_policy(widget: LevelMeterWidget) -> None:
    """プレイリストを圧迫しない低い固定高で、横だけ伸縮する。"""
    assert 70 <= widget.minimumHeight() <= 100
    assert widget.sizePolicy().horizontalPolicy() is QSizePolicy.Policy.Expanding
    assert widget.sizePolicy().verticalPolicy() is QSizePolicy.Policy.Fixed


def test_widget_has_no_focus_and_no_mouse_handling(widget: LevelMeterWidget) -> None:
    """可視化専用でフォーカスもマウス操作も持たない。"""
    assert widget.focusPolicy() is Qt.FocusPolicy.NoFocus
    assert type(widget).mousePressEvent is QWidget.mousePressEvent
    assert type(widget).mouseMoveEvent is QWidget.mouseMoveEvent
    assert type(widget).mouseReleaseEvent is QWidget.mouseReleaseEvent


def test_no_child_widgets_are_created_per_channel(widget: LevelMeterWidget) -> None:
    """チャンネルや目盛ごとに子Widgetを作らない。"""
    widget.set_frame(frame())
    widget.repaint()

    assert widget.findChildren(QWidget) == []
    assert widget.findChildren(QLabel) == []


def test_db_floor_matches_the_level_module_default(widget: LevelMeterWidget) -> None:
    """既定の表示下限はlevelモジュールの定数と揃う。"""
    assert widget.db_floor == LEVEL_DB_FLOOR


# -- 初期状態 ---------------------------------------------------------------


def test_initial_placeholder(widget: LevelMeterWidget) -> None:
    """sourceなしではプレースホルダーだけを表示する。"""
    widget.repaint()

    assert widget.frame is None
    assert widget.status_text == NO_SOURCE_MESSAGE
    assert widget.last_rms_bar_count == 0
    assert widget.last_peak_mark_count == 0
    assert widget.last_peak_hold_mark_count == 0


def test_status_text_can_be_replaced(widget: LevelMeterWidget) -> None:
    """停止中・失敗などの状態文字をWidget内の1か所へ描く。"""
    widget.set_status_text("停止中")
    widget.repaint()

    assert widget.status_text == "停止中"
    assert widget.findChildren(QLabel) == []


# -- 描画 -------------------------------------------------------------------


def test_silent_frame_draws_nothing_above_the_floor(widget: LevelMeterWidget) -> None:
    """無音フレームでも落ちず、floor以下の要素は描かない。"""
    widget.set_frame(silent_level_frame())
    widget.repaint()

    assert widget.last_rms_bar_count == 0
    assert widget.last_peak_mark_count == 0
    assert widget.last_peak_hold_mark_count == 0


def test_frame_draws_two_rms_bars_two_peaks_and_two_holds(widget: LevelMeterWidget) -> None:
    """L／Rそれぞれについて、RMSバー・Peak線・Peak hold線を描く。"""
    widget.set_frame(frame())
    widget.repaint()

    assert widget.last_rms_bar_count == 2
    assert widget.last_peak_mark_count == 2
    assert widget.last_peak_hold_mark_count == 2


def test_left_and_right_are_drawn_from_different_values(widget: LevelMeterWidget) -> None:
    """左右で異なる値のフレームも描画できる。"""
    asymmetric = frame(
        left_peak=0.0,
        left_rms=-3.0,
        left_hold=0.0,
        right_peak=-40.0,
        right_rms=-52.0,
        right_hold=-30.0,
    )
    widget.set_frame(asymmetric)
    widget.repaint()

    assert widget.frame is asymmetric
    assert widget.last_rms_bar_count == 2
    assert widget.last_peak_mark_count == 2
    assert widget.last_peak_hold_mark_count == 2


def test_only_channels_above_the_floor_are_drawn(widget: LevelMeterWidget) -> None:
    """floorのチャンネルはバーも線も描かない。"""
    widget.set_frame(
        frame(
            left_peak=-6.0,
            left_rms=-10.0,
            left_hold=-6.0,
            right_peak=LEVEL_DB_FLOOR,
            right_rms=LEVEL_DB_FLOOR,
            right_hold=LEVEL_DB_FLOOR,
        )
    )
    widget.repaint()

    assert widget.last_rms_bar_count == 1
    assert widget.last_peak_mark_count == 1
    assert widget.last_peak_hold_mark_count == 1


def test_processor_output_can_be_drawn(widget: LevelMeterWidget) -> None:
    """LevelProcessorが返すフレームをそのまま描ける。"""
    import numpy as np

    processor = LevelProcessor()
    left = np.full(4_096, 0.5, dtype=np.float32)
    right = np.full(4_096, 0.25, dtype=np.float32)

    widget.set_frame(processor.process(left, right, elapsed_seconds=0.033))
    widget.repaint()

    assert widget.last_rms_bar_count == 2
    assert widget.last_peak_hold_mark_count == 2


def test_grid_steps_are_inside_the_display_range(widget: LevelMeterWidget) -> None:
    """dB目盛はfloorより上の値だけを使う。"""
    assert all(LEVEL_DB_FLOOR < step < 0.0 for step in GRID_DB_STEPS)


@pytest.mark.parametrize("size", [(120, 70), (400, 90), (1_920, 100), (0, 90), (400, 20)])
def test_resize_does_not_break_drawing(widget: LevelMeterWidget, size: tuple[int, int]) -> None:
    """極端な幅・高さでも描画で落ちない。"""
    widget.set_frame(frame())
    widget.resize(*size)

    widget.repaint()

    assert widget.last_rms_bar_count <= 2


def test_palette_change_triggers_a_repaint(widget: LevelMeterWidget) -> None:
    """固定RGBではなくpaletteで描くため、palette変更で再描画する。"""
    widget.set_frame(frame())
    widget.repaint()
    palette = QPalette(widget.palette())
    palette.setColor(QPalette.ColorRole.Highlight, Qt.GlobalColor.red)

    widget.setPalette(palette)
    widget.repaint()

    assert widget.last_rms_bar_count == 2


def test_db_floor_can_be_narrowed(widget: LevelMeterWidget) -> None:
    """表示下限を変えると再描画され、その下限が使われる。"""
    widget.set_frame(frame())

    widget.set_db_floor(-60.0)
    widget.repaint()

    assert widget.db_floor == -60.0
    assert widget.last_rms_bar_count == 2


@pytest.mark.parametrize("value", [0.0, 1.0, float("nan"), float("inf"), True, False])
def test_invalid_db_floor_is_rejected(widget: LevelMeterWidget, value: float | bool) -> None:
    """0以上・非有限値・boolの下限は受け付けない。"""
    with pytest.raises(ValueError, match="db_floor"):
        widget.set_db_floor(value)


def test_clear_frame_discards_the_previous_levels(widget: LevelMeterWidget) -> None:
    """source切替・停止で前sourceのレベルとPeak holdを即時破棄する。"""
    widget.set_frame(frame())
    widget.repaint()

    widget.clear_frame("停止中")
    widget.repaint()

    assert widget.frame is None
    assert widget.status_text == "停止中"
    assert widget.last_rms_bar_count == 0
    assert widget.last_peak_hold_mark_count == 0


def test_repaint_keeps_the_frame_for_a_paused_display(widget: LevelMeterWidget) -> None:
    """pause相当の再描画ではフレームを保持する。"""
    held = frame()
    widget.set_frame(held)

    widget.repaint()
    widget.repaint()

    assert widget.frame is held
    assert widget.last_rms_bar_count == 2


def test_mouse_clicks_do_not_change_the_state(widget: LevelMeterWidget) -> None:
    """マウス操作は受け付けない（シークは波形側の責務）。"""
    held = frame()
    widget.set_frame(held)

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
    QTest.mouseClick(widget, Qt.MouseButton.RightButton, pos=QPoint(200, 40))

    assert widget.frame is held
    assert widget.status_text == NO_SOURCE_MESSAGE


def test_deleted_widget_is_safe(qtbot: QtBot) -> None:
    """破棄後のWidgetでもクラッシュしない。"""
    instance = LevelMeterWidget()
    qtbot.addWidget(instance)
    instance.set_frame(frame())
    instance.repaint()

    instance.deleteLater()
    qtbot.wait(10)

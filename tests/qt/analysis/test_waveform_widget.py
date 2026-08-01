"""WaveformWidgetの描画、投影cache、クリック・ドラッグ契約を検証する。"""

from pathlib import Path

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QSignalSpy, QTest
from pytestqt.qtbot import QtBot

from sdp.core.analysis.waveform import WaveformData
from sdp.ui.waveform_widget import WaveformWidget


def waveform(*, complete: bool = True) -> WaveformData:
    minimum = np.linspace(-1.0, -0.1, 3_000, dtype=np.float32)
    maximum = -minimum
    return WaveformData(minimum, maximum, 20.0, 60_000, complete)


def ready_widget(qtbot: QtBot) -> WaveformWidget:
    widget = WaveformWidget()
    qtbot.addWidget(widget)
    widget.resize(600, 120)
    widget.reset_for_source(True)
    widget.set_duration(120_000)
    widget.set_position(60_000)
    widget.show()
    return widget


def test_initial_contract_and_empty_paint(qtbot: QtBot) -> None:
    """objectName・高さ・accessibleNameが安定し、初期描画できる。"""
    widget = WaveformWidget()
    qtbot.addWidget(widget)
    widget.resize(600, 120)
    widget.show()
    assert widget.objectName() == "waveformWidget"
    assert widget.minimumHeight() == 55
    assert widget.accessibleName() == "波形"
    assert not widget.grab().isNull()


def test_complete_partial_empty_resize_and_palette_can_paint(qtbot: QtBot) -> None:
    """complete／partial／空データ、resize、palette変更を安全に描画する。"""
    widget = ready_widget(qtbot)
    for data in (
        waveform(),
        waveform(complete=False),
        WaveformData(
            np.empty(0, dtype=np.float32),
            np.empty(0, dtype=np.float32),
            20.0,
            0,
            True,
        ),
    ):
        widget.set_waveform_data(data)
        assert not widget.grab().isNull()
    before = widget.projection_count
    widget.set_waveform_data(waveform())
    widget.grab()
    widget.resize(800, 120)
    widget.grab()
    assert widget.projection_count >= before + 2
    palette = QPalette(widget.palette())
    palette.setBrush(QPalette.ColorRole.Base, palette.brush(QPalette.ColorRole.Window))
    widget.setPalette(palette)
    assert not widget.grab().isNull()


def test_projection_cache_and_line_count_are_bounded_by_width(qtbot: QtBot) -> None:
    """同一条件では再投影せず、波形線数はpixel幅を超えない。"""
    widget = ready_widget(qtbot)
    widget.set_waveform_data(waveform())
    widget.grab()
    count = widget.projection_count
    widget.grab()
    assert widget.projection_count == count
    assert widget.last_waveform_line_count <= widget.width()
    widget.set_position(61_000)
    widget.grab()
    assert widget.projection_count == count + 1


def test_seek_is_disabled_without_source_or_duration(qtbot: QtBot) -> None:
    """sourceなし、またはduration未確定ならクリックをseekへ変換しない。"""
    widget = WaveformWidget()
    qtbot.addWidget(widget)
    widget.resize(600, 120)
    widget.show()
    spy = QSignalSpy(widget.seek_requested)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    widget.reset_for_source(True)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    assert spy.count() == 0


def test_left_click_maps_center_edges_and_clamps(qtbot: QtBot) -> None:
    """release位置を中央固定窓へ変換し、音源端へclampして1回通知する。"""
    widget = ready_widget(qtbot)
    spy = QSignalSpy(widget.seek_requested)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    assert spy.at(0)[0] == 60_000
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(0, 60))
    assert spy.at(1)[0] == 30_000
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(599, 60))
    assert 89_800 <= spy.at(2)[0] <= 90_000

    widget.set_position(1_000)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(0, 60))
    assert spy.at(3)[0] == 0
    widget.set_position(119_000)
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(599, 60))
    assert spy.at(4)[0] == 120_000


def test_non_left_buttons_do_not_seek(qtbot: QtBot) -> None:
    """右・中央ボタンはシーク操作として扱わない。"""
    widget = ready_widget(qtbot)
    spy = QSignalSpy(widget.seek_requested)
    for button in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
        QTest.mouseClick(widget, button, pos=QPoint(300, 60))
    assert spy.count() == 0


def test_drag_previews_without_seek_and_releases_once_with_frozen_center(qtbot: QtBot) -> None:
    """move中はpreviewだけ更新し、position通知で基準を動かさずreleaseで1回seekする。"""
    widget = ready_widget(qtbot)
    spy = QSignalSpy(widget.seek_requested)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    QTest.mouseMove(widget, QPoint(450, 60))
    assert spy.count() == 0
    assert widget.preview_position_ms == 75_000
    widget.set_position(70_000)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(450, 60))
    assert spy.count() == 1
    assert spy.at(0)[0] == 75_000
    assert widget.preview_position_ms is None


def test_clear_hide_and_disable_cancel_drag(qtbot: QtBot) -> None:
    """source切替相当のclear、hide、disableは古いreleaseを無効化する。"""
    widget = ready_widget(qtbot)
    spy = QSignalSpy(widget.seek_requested)

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    widget.reset_for_source(True)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(400, 60))

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    widget.hide()
    widget.show()
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(400, 60))

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(300, 60))
    widget.setEnabled(False)
    widget.setEnabled(True)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(400, 60))
    assert spy.count() == 0


def test_widget_has_no_controller_service_or_path_dependency() -> None:
    """描画WidgetのmoduleはController、解析Service、pathを所有しない。"""
    module = Path(__file__).parents[3] / "src" / "sdp" / "ui" / "waveform_widget.py"
    source = module.read_text(encoding="utf-8")
    assert "PlaybackController" not in source
    assert "WaveformAnalysisService" not in source
    assert "WaveformCache" not in source

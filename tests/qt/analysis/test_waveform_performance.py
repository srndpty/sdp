"""長尺波形の投影・描画が表示幅へ制限され、event loopを塞がないことを検証する。"""

import numpy as np
import pytest
from PySide6.QtCore import QTimer
from pytestqt.qtbot import QtBot

from sdp.core.analysis.waveform import WaveformData
from sdp.ui.waveform_widget import WaveformWidget


@pytest.fixture(scope="module")
def long_waveform() -> WaveformData:
    """20ms×180,000 bucket（60分）の決定的な波形を返す。"""
    phase = np.linspace(0.0, 100.0, 180_000, dtype=np.float32)
    amplitude = np.abs(np.sin(phase)).astype(np.float32)
    return WaveformData(-amplitude, amplitude, 20.0, 3_600_000, True)


@pytest.mark.parametrize("width", [800, 1_920, 3_840])
def test_long_waveform_projects_and_paints_at_most_pixel_width(
    long_waveform: WaveformData, width: int, qtbot: QtBot
) -> None:
    """全track分の線を作らず、出力列・描画線をWidget幅以内に制限する。"""
    widget = WaveformWidget()
    qtbot.addWidget(widget)
    widget.resize(width, 120)
    widget.reset_for_source(True)
    widget.set_duration(long_waveform.duration_ms)
    widget.set_position(long_waveform.duration_ms // 2)
    widget.set_waveform_data(long_waveform)
    widget.show()

    columns = widget.projected_columns()
    assert columns is not None
    assert columns.minimum.size == width
    assert not widget.grab().isNull()
    assert widget.last_waveform_line_count <= width


def test_repeated_position_updates_keep_gui_heartbeat(
    long_waveform: WaveformData, qtbot: QtBot
) -> None:
    """1,920pxで100回追従投影・描画している間もGUI eventを処理する。"""
    widget = WaveformWidget()
    qtbot.addWidget(widget)
    widget.resize(1_920, 120)
    widget.reset_for_source(True)
    widget.set_duration(long_waveform.duration_ms)
    widget.set_waveform_data(long_waveform)
    widget.show()
    heartbeats: list[int] = []
    updates: list[int] = []
    heartbeat_timer = QTimer(widget)
    heartbeat_timer.setInterval(0)
    heartbeat_timer.timeout.connect(lambda: heartbeats.append(1))
    update_timer = QTimer(widget)
    update_timer.setInterval(0)

    def update_position() -> None:
        index = len(updates)
        widget.set_position(1_800_000 + index * 20)
        widget.grab()
        updates.append(index)
        if len(updates) == 100:
            update_timer.stop()

    update_timer.timeout.connect(update_position)
    heartbeat_timer.start()
    update_timer.start()
    qtbot.waitUntil(lambda: len(updates) == 100, timeout=10_000)

    heartbeat_timer.stop()
    assert heartbeats
    assert widget.last_waveform_line_count <= widget.width()

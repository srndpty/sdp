"""WaveformPanelによるController・解析Service・Widget間の調停を検証する。"""

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.waveform import WaveformData
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.waveform_panel import ANALYZING_MESSAGE, FAILED_MESSAGE, WaveformPanel
from sdp.ui.waveform_widget import NO_SOURCE_MESSAGE, WaveformWidget


@pytest.fixture
def backend() -> FakePlaybackBackend:
    return FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> PlaybackController:
    return PlaybackController(backend)


@pytest.fixture
def service(controller: PlaybackController, tmp_path: Path) -> Iterator[WaveformAnalysisService]:
    instance = WaveformAnalysisService(controller, tmp_path / "cache")
    yield instance
    instance.shutdown()


@pytest.fixture
def panel(
    controller: PlaybackController,
    service: WaveformAnalysisService,
    qtbot: QtBot,
) -> WaveformPanel:
    instance = WaveformPanel(controller, service)
    qtbot.addWidget(instance)
    instance.resize(600, 140)
    instance.show()
    return instance


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "A.wav"
    second = tmp_path / "B.wav"
    first.write_bytes(b"A")
    second.write_bytes(b"B")
    return first.resolve(), second.resolve()


def data(*, complete: bool, duration_ms: int = 10_000) -> WaveformData:
    return WaveformData(
        np.full(500, -0.5, dtype=np.float32),
        np.full(500, 0.5, dtype=np.float32),
        20.0,
        duration_ms,
        complete,
    )


def status(panel: WaveformPanel) -> str:
    label = panel.findChild(QLabel, "waveformStatusLabel")
    assert label is not None
    return label.text()


def test_structure_and_initial_state(
    panel: WaveformPanel, service: WaveformAnalysisService
) -> None:
    """Panelは指定ServiceとWidgetを1つ持ち、sourceなし表示から始まる。"""
    assert panel.objectName() == "waveformPanel"
    assert panel.waveform_analysis is service
    assert len(panel.findChildren(WaveformWidget)) == 1
    assert status(panel) == NO_SOURCE_MESSAGE
    assert panel.waveform_widget.status_text == NO_SOURCE_MESSAGE


def test_source_started_partial_finished_and_cache_hit_states(
    panel: WaveformPanel,
    controller: PlaybackController,
    service: WaveformAnalysisService,
    sources: tuple[Path, Path],
) -> None:
    """source、開始、partial、通常完了、cache完了を現在path/tokenだけ反映する。"""
    first, _ = sources
    controller.load(first)
    assert status(panel) == ANALYZING_MESSAGE
    assert panel.waveform_widget.waveform_data is None
    service.analysis_started.emit(first, 1)
    partial = data(complete=False)
    service.partial_ready.emit(first, 1, partial)
    assert panel.waveform_widget.waveform_data is partial
    assert status(panel) == ANALYZING_MESSAGE
    complete = data(complete=True)
    service.analysis_finished.emit(first, 1, complete, False)
    assert panel.waveform_widget.waveform_data is complete
    assert status(panel) == ""

    service.analysis_started.emit(first, 2)
    service.analysis_finished.emit(first, 2, complete, True)
    assert panel.waveform_widget.waveform_data is complete
    assert status(panel) == ""


def test_path_and_token_mismatches_are_ignored(
    panel: WaveformPanel,
    controller: PlaybackController,
    service: WaveformAnalysisService,
    sources: tuple[Path, Path],
) -> None:
    """active path/tokenと異なるpartial・finished・failedを混在させない。"""
    first, second = sources
    controller.load(first)
    service.analysis_started.emit(first, 10)
    expected = data(complete=False)
    service.partial_ready.emit(first, 10, expected)
    other = data(complete=True)
    service.partial_ready.emit(first, 11, other)
    service.analysis_finished.emit(second, 10, other, False)
    service.analysis_failed.emit(first, 11, "生のdecoder error")
    assert panel.waveform_widget.waveform_data is expected
    assert status(panel) == ANALYZING_MESSAGE


def test_failure_is_generic_and_keeps_partial_seekable(
    panel: WaveformPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    service: WaveformAnalysisService,
    sources: tuple[Path, Path],
) -> None:
    """解析失敗は生詳細を表示せず、partialと再生用durationによるseekを維持する。"""
    first, _ = sources
    controller.load(first)
    backend.emit_duration(120_000)
    service.analysis_started.emit(first, 1)
    partial = data(complete=False)
    service.partial_ready.emit(first, 1, partial)
    service.analysis_failed.emit(first, 1, "QAudioDecoder raw error")
    assert status(panel) == FAILED_MESSAGE
    assert "QAudioDecoder" not in status(panel)
    assert panel.waveform_widget.waveform_data is partial
    assert panel.waveform_widget.duration_ms == 120_000


def test_duration_priority_and_complete_fallback(
    panel: WaveformPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    service: WaveformAnalysisService,
    sources: tuple[Path, Path],
) -> None:
    """Controller正値を優先し、未確定時だけcomplete波形durationへfallbackする。"""
    first, _ = sources
    controller.load(first)
    service.analysis_started.emit(first, 1)
    service.partial_ready.emit(first, 1, data(complete=False, duration_ms=30_000))
    assert panel.waveform_widget.duration_ms == 0
    service.analysis_finished.emit(first, 1, data(complete=True, duration_ms=90_000), False)
    assert panel.waveform_widget.duration_ms == 90_000
    backend.emit_duration(120_000)
    assert panel.waveform_widget.duration_ms == 120_000


def test_position_tracks_controller_without_feedback(
    panel: WaveformPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    sources: tuple[Path, Path],
) -> None:
    """position通知は表示中心だけを変え、seekや他setterを呼び返さない。"""
    first, _ = sources
    controller.load(first)
    backend.calls.clear()
    backend.emit_position(42_000)
    assert panel.waveform_widget.position_ms == 42_000
    assert backend.calls == []
    backend.emit_position(-10)
    assert panel.waveform_widget.position_ms == 0


def test_click_delegates_one_seek_without_reload_or_state_change(
    panel: WaveformPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    sources: tuple[Path, Path],
) -> None:
    """Widgetのseek要求をControllerへ1回だけ委譲し、sourceと再生状態を維持する。"""
    first, _ = sources
    controller.load(first)
    backend.emit_duration(120_000)
    backend.emit_position(60_000)
    backend.emit_state(PlaybackState.PAUSED)
    backend.calls.clear()
    widget = panel.waveform_widget
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(widget.width() // 2, 50))
    assert backend.call_args("seek") == [(60_000,)]
    assert "load" not in backend.call_names()
    assert controller.source == first
    assert controller.state is PlaybackState.PAUSED


def test_source_switch_and_clear_cancel_old_drag_and_waveform(
    panel: WaveformPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    service: WaveformAnalysisService,
    sources: tuple[Path, Path],
) -> None:
    """Aのdrag・波形をBへ持ち越さず、古いreleaseでBをseekしない。"""
    first, second = sources
    controller.load(first)
    backend.emit_duration(120_000)
    service.analysis_started.emit(first, 1)
    service.analysis_finished.emit(first, 1, data(complete=True), False)
    widget = panel.waveform_widget
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(100, 50))
    backend.calls.clear()
    controller.load(second)
    assert widget.waveform_data is None
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(500, 50))
    assert "seek" not in backend.call_names()
    service.analysis_finished.emit(first, 1, data(complete=True), False)
    assert widget.waveform_data is None
    service.analysis_cleared.emit()
    assert widget.waveform_data is None


def test_panel_has_no_backend_dependency() -> None:
    """Panelは具体Backendをimportしない。"""
    module = Path(__file__).parents[3] / "src" / "sdp" / "ui" / "waveform_panel.py"
    source = module.read_text(encoding="utf-8")
    assert "qt_backend" not in source
    assert "QMediaPlayer" not in source

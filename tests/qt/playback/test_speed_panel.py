"""SpeedPanelのController同期と操作契約を検証する。"""

import inspect
from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QLabel, QPushButton, QSlider
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.ui.speed_panel import (
    DEFAULT_PLAYBACK_RATE,
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    PLAYBACK_RATE_PRESETS,
    SpeedPanel,
    rate_to_slider_value,
    slider_value_to_rate,
)


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend(playback_rate=1.25, pitch_compensation=False)


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def panel(controller: PlaybackController, qtbot: QtBot) -> Iterator[SpeedPanel]:
    widget = SpeedPanel(controller)
    qtbot.addWidget(widget)
    yield widget


def slider(panel: SpeedPanel) -> QSlider:
    widget = panel.findChild(QSlider, "playbackRateSlider")
    assert widget is not None
    return widget


def spin_box(panel: SpeedPanel) -> QDoubleSpinBox:
    widget = panel.findChild(QDoubleSpinBox, "playbackRateSpinBox")
    assert widget is not None
    return widget


def pitch_check_box(panel: SpeedPanel) -> QCheckBox:
    widget = panel.findChild(QCheckBox, "pitchCompensationCheckBox")
    assert widget is not None
    return widget


def preset_name(rate: float) -> str:
    return f"ratePreset{rate_to_slider_value(rate):03d}Button"


@pytest.mark.parametrize(
    ("slider_value", "rate"),
    [(50, 0.5), (100, 1.0), (125, 1.25), (200, 2.0)],
)
def test_rate_conversion_boundaries(slider_value: int, rate: float) -> None:
    """整数スライダー値と速度を境界を含めて相互変換する。"""
    assert slider_value_to_rate(slider_value) == rate
    assert rate_to_slider_value(rate) == slider_value


def test_constructor_takes_only_controller_and_parent() -> None:
    """SpeedPanelはPlaybackController以外のアプリ層へ依存しない。"""
    assert list(inspect.signature(SpeedPanel.__init__).parameters) == [
        "self",
        "controller",
        "parent",
    ]


def test_widgets_and_initial_controller_state(panel: SpeedPanel) -> None:
    """必要なWidgetが存在し、初期値はControllerから取得する。"""
    rate_slider = slider(panel)
    rate_spin_box = spin_box(panel)
    reset = panel.findChild(QPushButton, "resetPlaybackRateButton")
    pitch = pitch_check_box(panel)
    mode = panel.findChild(QLabel, "pitchModeLabel")

    assert reset is not None
    assert mode is not None
    assert (rate_slider.minimum(), rate_slider.maximum()) == (50, 200)
    assert (rate_slider.singleStep(), rate_slider.pageStep()) == (5, 10)
    assert (rate_spin_box.minimum(), rate_spin_box.maximum()) == (
        MIN_PLAYBACK_RATE,
        MAX_PLAYBACK_RATE,
    )
    assert rate_spin_box.decimals() == 2
    assert rate_spin_box.singleStep() == 0.05
    assert rate_spin_box.suffix() == "×"
    assert rate_spin_box.keyboardTracking() is False
    assert rate_slider.value() == 125
    assert rate_spin_box.value() == 1.25
    assert pitch.isChecked() is False
    assert "varispeed" in mode.text()


@pytest.mark.parametrize(("value", "rate"), [(50, 0.5), (175, 1.75), (200, 2.0)])
def test_slider_updates_spin_box_and_controller_once(
    panel: SpeedPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    value: int,
    rate: float,
) -> None:
    """slider変更はSpinBoxとControllerへ即時に1回だけ反映する。"""
    backend.calls.clear()
    spy = QSignalSpy(controller.playback_rate_changed)

    slider(panel).setValue(value)

    assert spin_box(panel).value() == rate
    assert controller.playback_rate == rate
    assert backend.call_args("set_playback_rate") == [(rate,)]
    assert spy.count() == 1


@pytest.mark.parametrize("rate", [0.5, 1.3, 2.0])
def test_spin_box_updates_slider_and_controller_once(
    panel: SpeedPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    rate: float,
) -> None:
    """SpinBox変更はsliderとControllerへ1回だけ反映する。"""
    backend.calls.clear()
    spy = QSignalSpy(controller.playback_rate_changed)

    spin_box(panel).setValue(rate)

    assert slider(panel).value() == rate_to_slider_value(rate)
    assert controller.playback_rate == rate
    assert backend.call_args("set_playback_rate") == [(rate,)]
    assert spy.count() == 1


def test_controller_rate_notification_updates_widgets_without_calling_setter(
    panel: SpeedPanel, backend: FakePlaybackBackend
) -> None:
    """Controller由来の通知はWidgetだけを更新し、setterを呼び返さない。"""
    backend.calls.clear()

    backend.playback_rate_changed.emit(1.5)

    assert slider(panel).value() == 150
    assert spin_box(panel).value() == 1.5
    assert backend.call_names() == []


def test_float32_readback_does_not_move_requested_display(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """float32相当の同期読み戻しでも要求した2桁表示を維持する。"""
    backend.float32_rate_readback = True
    backend.calls.clear()

    spin_box(panel).setValue(1.35)

    assert backend.playback_rate != 1.35
    assert controller.playback_rate == 1.35
    assert slider(panel).value() == 135
    assert spin_box(panel).value() == 1.35
    assert backend.call_args("set_playback_rate") == [(1.35,)]


def test_rate_setter_failure_restores_controller_value(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend, qtbot: QtBot
) -> None:
    """Backendの速度設定失敗後はControllerのロールバック値を表示する。"""
    backend.setter_errors["set_playback_rate"] = RuntimeError("速度設定失敗")

    with qtbot.captureExceptions() as exceptions:
        slider(panel).setValue(150)

    assert len(exceptions) == 1
    assert isinstance(exceptions[0][1], RuntimeError)
    assert controller.playback_rate == 1.25
    assert slider(panel).value() == 125
    assert spin_box(panel).value() == 1.25


def test_pitch_setter_failure_restores_controller_value(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend, qtbot: QtBot
) -> None:
    """Backendのpitch設定失敗後はControllerのロールバック値を表示する。"""
    backend.setter_errors["set_pitch_compensation"] = RuntimeError("pitch設定失敗")

    with qtbot.captureExceptions() as exceptions:
        pitch_check_box(panel).click()

    assert len(exceptions) == 1
    assert isinstance(exceptions[0][1], RuntimeError)
    assert controller.pitch_compensation is False
    assert pitch_check_box(panel).isChecked() is False
    mode = panel.findChild(QLabel, "pitchModeLabel")
    assert mode is not None
    assert "varispeed" in mode.text()


def test_widget_ranges_clamp_user_values(panel: SpeedPanel) -> None:
    """WidgetからUI契約の0.50～2.00を超えない。"""
    slider(panel).setValue(0)
    spin_box(panel).setValue(9.0)

    assert slider(panel).value() >= 50
    assert spin_box(panel).value() <= 2.0


def test_all_presets_set_the_rate_once(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """一元定義した6プリセットが対応する速度を1回設定する。"""
    assert len(PLAYBACK_RATE_PRESETS) == 6
    for rate in PLAYBACK_RATE_PRESETS:
        button = panel.findChild(QPushButton, preset_name(rate))
        assert button is not None
        backend.calls.clear()

        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

        expected = [] if rate == controller.playback_rate and not backend.calls else [(rate,)]
        assert backend.call_args("set_playback_rate") == expected
        assert controller.playback_rate == rate
        assert slider(panel).value() == rate_to_slider_value(rate)
        assert spin_box(panel).value() == rate


def test_repeated_preset_and_reset_at_default_are_no_ops(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """同じプリセットと既定値でのresetはBackend呼出を増やさない。"""
    preset = panel.findChild(QPushButton, preset_name(1.0))
    reset = panel.findChild(QPushButton, "resetPlaybackRateButton")
    assert preset is not None
    assert reset is not None
    controller.set_playback_rate(DEFAULT_PLAYBACK_RATE)
    backend.calls.clear()

    QTest.mouseClick(preset, Qt.MouseButton.LeftButton)
    QTest.mouseClick(preset, Qt.MouseButton.LeftButton)
    QTest.mouseClick(reset, Qt.MouseButton.LeftButton)

    assert backend.call_names() == []


def test_reset_changes_only_rate(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """resetは速度だけを1.0へ戻し、pitch状態を変えない。"""
    reset = panel.findChild(QPushButton, "resetPlaybackRateButton")
    assert reset is not None
    backend.calls.clear()

    QTest.mouseClick(reset, Qt.MouseButton.LeftButton)

    assert controller.playback_rate == DEFAULT_PLAYBACK_RATE
    assert controller.pitch_compensation is False
    assert backend.call_args("set_playback_rate") == [(DEFAULT_PLAYBACK_RATE,)]
    assert backend.call_args("set_pitch_compensation") == []


def test_pitch_checkbox_updates_controller_once(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """pitchチェック操作を即時に1回だけControllerへ渡す。"""
    check_box = pitch_check_box(panel)
    backend.calls.clear()
    spy = QSignalSpy(controller.pitch_compensation_changed)

    check_box.click()

    assert controller.pitch_compensation is True
    assert backend.call_args("set_pitch_compensation") == [(True,)]
    assert spy.count() == 1
    mode = panel.findChild(QLabel, "pitchModeLabel")
    assert mode is not None
    assert "time-stretch" in mode.text()


def test_controller_pitch_notification_does_not_call_setter(
    panel: SpeedPanel, backend: FakePlaybackBackend
) -> None:
    """Controller由来のpitch更新をsetterへ返送しない。"""
    backend.calls.clear()

    backend.pitch_compensation_changed.emit(True)

    assert pitch_check_box(panel).isChecked() is True
    assert backend.call_names() == []


def test_operations_without_source_do_not_use_transport(
    panel: SpeedPanel, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """sourceなしでも操作でき、load・transport・seekを呼ばない。"""
    assert controller.source is None
    backend.calls.clear()

    slider(panel).setValue(150)
    pitch_check_box(panel).click()

    assert backend.call_names() == ["set_playback_rate", "set_pitch_compensation"]


def test_out_of_ui_range_controller_rate_is_shown_and_resettable(
    panel: SpeedPanel,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """範囲外の真値を明示し、通常入力を止めてresetから復帰できる。"""
    label = panel.findChild(QLabel, "outOfRangePlaybackRateLabel")
    reset = panel.findChild(QPushButton, "resetPlaybackRateButton")
    assert label is not None
    assert reset is not None

    with caplog.at_level("ERROR"):
        backend.playback_rate_changed.emit(3.0)

    assert controller.playback_rate == 3.0
    assert not slider(panel).isEnabled()
    assert not spin_box(panel).isEnabled()
    assert not label.isHidden()
    assert label.text() == "現在の速度: 3.00×（操作範囲外）"
    assert reset.isEnabled()
    assert "UI範囲外" in caplog.text

    reset.click()

    assert controller.playback_rate == DEFAULT_PLAYBACK_RATE
    assert slider(panel).isEnabled()
    assert spin_box(panel).isEnabled()
    assert label.isHidden()
    assert spin_box(panel).value() == DEFAULT_PLAYBACK_RATE


def test_destroyed_panel_is_disconnected_from_controller(
    controller: PlaybackController, qtbot: QtBot
) -> None:
    """Widget破棄後のController通知が破棄済みスロットへ届かない。"""
    panel = SpeedPanel(controller)
    assert isValid(panel)
    panel.deleteLater()
    qtbot.waitUntil(lambda: not isValid(panel))
    assert not isValid(panel)

    controller.playback_rate_changed.emit(1.5)
    controller.pitch_compensation_changed.emit(False)

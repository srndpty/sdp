"""再生速度とピッチ補正だけを操作するウィジェット。"""

import logging
import math

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QWidget,
)

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.preferences import (
    DEFAULT_PLAYBACK_RATE,
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    PLAYBACK_RATE_STEP,
)

_logger = logging.getLogger(__name__)

_RATE_SCALE = 100
_PITCH_TOOLTIP = (
    "オン: 速度を変えても音高を維持します。\n"
    "オフ: レコードの回転数変更のように速度と音高が一緒に変わります。"
)


def slider_value_to_rate(value: int) -> float:
    """スライダーの整数値を再生速度へ変換する。"""
    return value / _RATE_SCALE


def rate_to_slider_value(rate: float) -> int:
    """再生速度をスライダーの整数値へ変換する。"""
    return round(rate * _RATE_SCALE)


class SpeedPanel(QWidget):
    """PlaybackControllerを真値として速度・ピッチの表示と操作を同期する。"""

    def __init__(self, controller: PlaybackController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setObjectName("speedPanel")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self._rate_slider = QSlider(Qt.Orientation.Horizontal)
        self._rate_slider.setObjectName("playbackRateSlider")
        self._rate_slider.setRange(
            rate_to_slider_value(MIN_PLAYBACK_RATE),
            rate_to_slider_value(MAX_PLAYBACK_RATE),
        )
        self._rate_slider.setSingleStep(5)
        self._rate_slider.setPageStep(10)

        self._rate_spin_box = QDoubleSpinBox()
        self._rate_spin_box.setObjectName("playbackRateSpinBox")
        self._rate_spin_box.setRange(MIN_PLAYBACK_RATE, MAX_PLAYBACK_RATE)
        self._rate_spin_box.setDecimals(2)
        self._rate_spin_box.setSingleStep(PLAYBACK_RATE_STEP)
        self._rate_spin_box.setSuffix("×")
        # 編集途中の不完全な文字列を Controller へ送らず、確定時に反映する。
        self._rate_spin_box.setKeyboardTracking(False)

        self._reset_button = QPushButton("1.0倍に戻す")
        self._reset_button.setObjectName("resetPlaybackRateButton")

        self._out_of_range_label = QLabel()
        self._out_of_range_label.setObjectName("outOfRangePlaybackRateLabel")
        self._out_of_range_label.setVisible(False)

        self._pitch_check_box = QCheckBox("ピッチ維持")
        self._pitch_check_box.setObjectName("pitchCompensationCheckBox")
        self._pitch_check_box.setToolTip(_PITCH_TOOLTIP)

        self._build_layout()
        self._connect_signals()
        self._apply_controller_rate(controller.playback_rate)
        self._apply_controller_pitch(controller.pitch_compensation)

    def _build_layout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 2, 9, 2)
        layout.addWidget(QLabel("再生速度"))
        layout.addWidget(self._out_of_range_label)
        layout.addWidget(self._rate_slider, stretch=1)
        layout.addWidget(self._rate_spin_box)
        layout.addWidget(self._reset_button)
        layout.addWidget(self._pitch_check_box)

    def _connect_signals(self) -> None:
        self._rate_slider.valueChanged.connect(self._on_slider_changed)
        self._rate_spin_box.valueChanged.connect(self._on_spin_box_changed)
        self._reset_button.clicked.connect(self._on_reset_clicked)
        self._pitch_check_box.toggled.connect(self._on_pitch_toggled)
        self._controller.playback_rate_changed.connect(self._apply_controller_rate)
        self._controller.pitch_compensation_changed.connect(self._apply_controller_pitch)

    def _on_slider_changed(self, value: int) -> None:
        self._request_rate(slider_value_to_rate(value))

    def _on_spin_box_changed(self, rate: float) -> None:
        self._request_rate(rate)

    def _on_reset_clicked(self) -> None:
        self._request_rate(DEFAULT_PLAYBACK_RATE)

    def _on_pitch_toggled(self, enabled: bool) -> None:
        if enabled == self._controller.pitch_compensation:
            self._apply_controller_pitch(self._controller.pitch_compensation)
            return
        try:
            self._controller.set_pitch_compensation(enabled)
        except Exception:
            # Controllerはsetter失敗時に真値を戻すがSignalは発火しないため、
            # UIも公開propertyから明示的にロールバックする。
            self._apply_controller_pitch(self._controller.pitch_compensation)
            raise

    def _request_rate(self, rate: float) -> None:
        if rate == self._controller.playback_rate:
            self._apply_controller_rate(self._controller.playback_rate)
            return
        try:
            self._controller.set_playback_rate(rate)
        except Exception:
            # Controllerはsetter失敗時に真値を戻すがSignalは発火しないため、
            # UIも公開propertyから明示的にロールバックする。
            self._apply_controller_rate(self._controller.playback_rate)
            raise

    def _apply_controller_rate(self, rate: float) -> None:
        if not math.isfinite(rate) or not (MIN_PLAYBACK_RATE <= rate <= MAX_PLAYBACK_RATE):
            _logger.error("ControllerからUI範囲外の再生速度が通知されました: %r", rate)
            self._rate_slider.setEnabled(False)
            self._rate_spin_box.setEnabled(False)
            self._out_of_range_label.setText(
                f"現在の速度: {rate:.2f}×（操作範囲外）"
                if math.isfinite(rate)
                else f"現在の速度: {rate!r}（操作範囲外）"
            )
            self._out_of_range_label.setVisible(True)
            return
        self._rate_slider.setEnabled(True)
        self._rate_spin_box.setEnabled(True)
        self._out_of_range_label.setVisible(False)
        self._apply_rate_to_widgets(rate)

    def _apply_rate_to_widgets(self, rate: float) -> None:
        with QSignalBlocker(self._rate_slider), QSignalBlocker(self._rate_spin_box):
            self._rate_slider.setValue(rate_to_slider_value(rate))
            self._rate_spin_box.setValue(rate)

    def _apply_controller_pitch(self, enabled: bool) -> None:
        self._apply_pitch_to_widgets(enabled)

    def _apply_pitch_to_widgets(self, enabled: bool) -> None:
        with QSignalBlocker(self._pitch_check_box):
            self._pitch_check_box.setChecked(enabled)

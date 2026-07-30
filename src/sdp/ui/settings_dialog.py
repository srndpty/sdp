"""再生設定と可視化の表示ON/OFFを編集する設定ダイアログ。

設定ファイル（JSON）もschema versionも知らない。編集結果を
:class:`~sdp.services.settings.AppSettings` として要求するだけで、
実際の適用と保存は調停サービス（AppSettingsController／SettingsSession）が行う。

Apply／OK／Cancel は一般的なWindowsの設定ダイアログと同じ意味とする。

- Apply: 検証して適用し、ダイアログは閉じる**前**の状態を維持する
- OK: Applyと同じ処理のあと閉じる
- Cancel: **Apply後の変更は戻さず**、未適用の編集だけを破棄して閉じる
"""

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sdp.core.playback.preferences import (
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    PLAYBACK_RATE_STEP,
)
from sdp.services.settings import AppSettings, validate_settings

_logger = logging.getLogger(__name__)

DIALOG_TITLE = "設定"
INVALID_MESSAGE = "設定値が正しくないため適用できません。"

_PITCH_TOOLTIP = (
    "オン: 速度を変えても音高を維持します。\n"
    "オフ: レコードの回転数変更のように速度と音高が一緒に変わります。"
)
_VISUALIZATION_TOOLTIP = "非表示にすると、その可視化の解析と描画を停止します。"


class SettingsDialog(QDialog):
    """現在の適用済み設定を表示し、編集結果を要求として通知する。

    適用そのものは行わない（Controller・Panel・設定ファイルを操作しない）。
    """

    settings_requested = Signal(object)
    """ユーザーがApply／OKで適用を要求した（引数は :class:`AppSettings`）。"""

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle(DIALOG_TITLE)
        self.setAccessibleName("設定")
        # 再生を止めずに操作できるようモーダルにしない。
        self.setModal(False)

        self._applied = settings

        self._rate_spin_box = QDoubleSpinBox(self)
        self._rate_spin_box.setObjectName("settingsPlaybackRateSpinBox")
        self._rate_spin_box.setAccessibleName("再生速度")
        self._rate_spin_box.setRange(MIN_PLAYBACK_RATE, MAX_PLAYBACK_RATE)
        self._rate_spin_box.setSingleStep(PLAYBACK_RATE_STEP)
        self._rate_spin_box.setDecimals(2)
        self._rate_spin_box.setSuffix("×")

        self._pitch_check_box = self._make_check_box(
            "settingsPitchCompensationCheckBox",
            "ピッチを保ったまま速度を変更する(&P)",
            "ピッチ補正",
            _PITCH_TOOLTIP,
        )
        self._waveform_check_box = self._make_check_box(
            "settingsWaveformVisibleCheckBox",
            "波形を表示する(&W)",
            "波形を表示",
            _VISUALIZATION_TOOLTIP,
        )
        self._spectrum_check_box = self._make_check_box(
            "settingsSpectrumVisibleCheckBox",
            "スペクトラムを表示する(&S)",
            "スペクトラムを表示",
            _VISUALIZATION_TOOLTIP,
        )
        self._level_meter_check_box = self._make_check_box(
            "settingsLevelMeterVisibleCheckBox",
            "Peak／RMSレベルメーターを表示する(&L)",
            "レベルメーターを表示",
            _VISUALIZATION_TOOLTIP,
        )

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("settingsErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)

        playback_group = QGroupBox("再生", self)
        playback_form = QFormLayout(playback_group)
        playback_form.addRow("再生速度(&R)", self._rate_spin_box)
        playback_form.addRow("", self._pitch_check_box)

        display_group = QGroupBox("表示", self)
        display_layout = QVBoxLayout(display_group)
        display_layout.addWidget(self._waveform_check_box)
        display_layout.addWidget(self._spectrum_check_box)
        display_layout.addWidget(self._level_meter_check_box)

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply,
            self,
        )
        self._button_box.setObjectName("settingsButtonBox")
        self._name_buttons()

        layout = QVBoxLayout(self)
        layout.addWidget(playback_group)
        layout.addWidget(display_group)
        layout.addWidget(self._error_label)
        layout.addWidget(self._button_box)

        self._button_box.accepted.connect(self._on_accepted)
        self._button_box.rejected.connect(self.reject)
        apply_button = self._button_box.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.clicked.connect(self._on_apply_clicked)

        self.set_settings(settings)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def applied_settings(self) -> AppSettings:
        """このダイアログが最後に適用要求した（＝開いた時点からの）設定。"""
        return self._applied

    @property
    def error_text(self) -> str:
        return self._error_label.text()

    def set_settings(self, settings: AppSettings) -> None:
        """適用済み設定snapshotを各入力へ反映する。"""
        self._applied = settings
        self._rate_spin_box.setValue(settings.playback_rate)
        self._pitch_check_box.setChecked(settings.pitch_compensation)
        self._waveform_check_box.setChecked(settings.waveform_visible)
        self._spectrum_check_box.setChecked(settings.spectrum_visible)
        self._level_meter_check_box.setChecked(settings.level_meter_visible)
        self._clear_error()

    def current_input(self) -> AppSettings:
        """入力中の値を設定値オブジェクトへまとめる（適用はしない）。"""
        return AppSettings(
            playback_rate=self._rate_spin_box.value(),
            pitch_compensation=self._pitch_check_box.isChecked(),
            waveform_visible=self._waveform_check_box.isChecked(),
            spectrum_visible=self._spectrum_check_box.isChecked(),
            level_meter_visible=self._level_meter_check_box.isChecked(),
        )

    def apply_settings(self) -> bool:
        """入力を検証して適用を要求する。成功したら ``True``。

        通常操作ではWidgetの制約により不正値にならないが、プログラム経由で
        不正値が入った場合は適用せず、ダイアログ内へ短いエラーを表示する
        （例外をイベントループ外へ漏らさない）。
        """
        try:
            settings = self.current_input()
            validate_settings(settings)
        except (ValueError, TypeError):
            _logger.exception("設定ダイアログの入力が不正です")
            self._show_error(INVALID_MESSAGE)
            return False
        self._clear_error()
        # 同値でも適用済みsnapshotの更新として扱う（通知の抑制は調停側の責務）。
        self._applied = settings
        self.settings_requested.emit(settings)
        return True

    # -- 内部 ---------------------------------------------------------------

    def _make_check_box(
        self, object_name: str, text: str, accessible_name: str, tooltip: str
    ) -> QCheckBox:
        check_box = QCheckBox(text, self)
        check_box.setObjectName(object_name)
        check_box.setAccessibleName(accessible_name)
        check_box.setToolTip(tooltip)
        return check_box

    def _name_buttons(self) -> None:
        for standard, text, object_name in (
            (QDialogButtonBox.StandardButton.Ok, "OK", "settingsOkButton"),
            (QDialogButtonBox.StandardButton.Cancel, "キャンセル", "settingsCancelButton"),
            (QDialogButtonBox.StandardButton.Apply, "適用", "settingsApplyButton"),
        ):
            button = self._button_box.button(standard)
            button.setText(text)
            button.setObjectName(object_name)
            button.setAccessibleName(text)

    def _on_apply_clicked(self) -> None:
        self.apply_settings()

    def _on_accepted(self) -> None:
        # 不正入力のままOKで閉じない。
        if self.apply_settings():
            self.accept()

    def reject(self) -> None:
        """未適用の編集だけを破棄して閉じる（適用済みの変更は戻さない）。

        Esc も既定どおりここへ来る。
        """
        self.set_settings(self._applied)
        super().reject()

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(True)

    def _clear_error(self) -> None:
        self._error_label.setText("")
        self._error_label.setVisible(False)

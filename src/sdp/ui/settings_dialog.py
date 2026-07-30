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

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sdp.core.playback.preferences import (
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    PLAYBACK_RATE_STEP,
)
from sdp.services.settings import (
    MAX_VOLUME,
    MIN_VOLUME,
    AppSettings,
    RepeatModeSetting,
    validate_settings,
)

_logger = logging.getLogger(__name__)

DIALOG_TITLE = "設定"
INVALID_MESSAGE = "設定値が正しくないため適用できません。"
APPLY_FAILED_MESSAGE = "設定を適用できませんでした。"

_PITCH_TOOLTIP = (
    "オン: 速度を変えても音高を維持します。\n"
    "オフ: レコードの回転数変更のように速度と音高が一緒に変わります。"
)
_VISUALIZATION_TOOLTIP = "非表示にすると、その可視化の解析と描画を停止します。"

REPEAT_MODE_LABELS: tuple[tuple[RepeatModeSetting, str], ...] = (
    (RepeatModeSetting.OFF, "オフ"),
    (RepeatModeSetting.ALL, "全曲"),
    (RepeatModeSetting.ONE, "1曲"),
)
"""Repeatの表示名。保存用の文字列（off／all／one）はUIへ見せない。"""

_VOLUME_SCALE = 100
"""音量を%表示するための倍率（0.0〜1.0 ↔ 0〜100%）。"""


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
        self._request_succeeded = False

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

        self._volume_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._volume_slider.setObjectName("settingsVolumeSlider")
        self._volume_slider.setAccessibleName("音量")
        self._volume_slider.setRange(
            round(MIN_VOLUME * _VOLUME_SCALE), round(MAX_VOLUME * _VOLUME_SCALE)
        )
        self._volume_slider.setSingleStep(5)
        self._volume_slider.setPageStep(10)

        self._volume_spin_box = QSpinBox(self)
        self._volume_spin_box.setObjectName("settingsVolumeSpinBox")
        self._volume_spin_box.setAccessibleName("音量（％）")
        self._volume_spin_box.setRange(
            round(MIN_VOLUME * _VOLUME_SCALE), round(MAX_VOLUME * _VOLUME_SCALE)
        )
        self._volume_spin_box.setSuffix("％")

        self._muted_check_box = self._make_check_box(
            "settingsMutedCheckBox",
            "ミュートする(&M)",
            "ミュート",
            "オンのあいだ音を出しません（音量の値は保持します）。",
        )

        self._repeat_combo_box = QComboBox(self)
        self._repeat_combo_box.setObjectName("settingsRepeatModeComboBox")
        self._repeat_combo_box.setAccessibleName("リピート")
        for mode, label in REPEAT_MODE_LABELS:
            self._repeat_combo_box.addItem(label, mode)

        self._shuffle_check_box = self._make_check_box(
            "settingsShuffleCheckBox",
            "シャッフル再生する(&H)",
            "シャッフル",
            "プレイリストの表示順は変えず、再生順だけをランダムにします。",
        )

        self._error_label = QLabel("", self)
        self._error_label.setObjectName("settingsErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)

        volume_row = QWidget(self)
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.addWidget(self._volume_slider)
        volume_layout.addWidget(self._volume_spin_box)

        playback_group = QGroupBox("再生", self)
        playback_form = QFormLayout(playback_group)
        # ラベルはbuddyとして関連付ける（ニーモニックとスクリーンリーダー対応）。
        playback_form.addRow(
            self._make_label("再生速度(&R)", self._rate_spin_box), self._rate_spin_box
        )
        playback_form.addRow("", self._pitch_check_box)
        playback_form.addRow(self._make_label("音量(&V)", self._volume_slider), volume_row)
        playback_form.addRow("", self._muted_check_box)

        playlist_group = QGroupBox("プレイリスト", self)
        playlist_form = QFormLayout(playlist_group)
        playlist_form.addRow(
            self._make_label("リピート(&E)", self._repeat_combo_box), self._repeat_combo_box
        )
        playlist_form.addRow("", self._shuffle_check_box)

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
        layout.addWidget(playlist_group)
        layout.addWidget(display_group)
        layout.addWidget(self._error_label)
        layout.addWidget(self._button_box)

        # 見た目の並びどおりにTabで移動できるようにする（作成順に依存させない）。
        for previous, following in (
            (self._rate_spin_box, self._pitch_check_box),
            (self._pitch_check_box, self._volume_slider),
            (self._volume_slider, self._volume_spin_box),
            (self._volume_spin_box, self._muted_check_box),
            (self._muted_check_box, self._repeat_combo_box),
            (self._repeat_combo_box, self._shuffle_check_box),
            (self._shuffle_check_box, self._waveform_check_box),
            (self._waveform_check_box, self._spectrum_check_box),
            (self._spectrum_check_box, self._level_meter_check_box),
            (self._level_meter_check_box, self._button_box),
        ):
            QWidget.setTabOrder(previous, following)

        self._volume_slider.valueChanged.connect(self._on_volume_slider_changed)
        self._volume_spin_box.valueChanged.connect(self._on_volume_spin_box_changed)
        self._button_box.accepted.connect(self._on_accepted)
        self._button_box.rejected.connect(self.reject)
        apply_button = self._button_box.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.clicked.connect(self._on_apply_clicked)

        self.set_settings(settings)

    # -- 公開状態 -----------------------------------------------------------

    @property
    def applied_settings(self) -> AppSettings:
        """このダイアログが最後に適用成功通知を受けた設定。"""
        return self._applied

    @property
    def error_text(self) -> str:
        return self._error_label.text()

    def set_settings(self, settings: AppSettings) -> None:
        """適用済み設定snapshotを各入力へ反映する。"""
        self._applied = settings
        self._rate_spin_box.setValue(settings.playback_rate)
        self._pitch_check_box.setChecked(settings.pitch_compensation)
        self._set_volume(settings.volume)
        self._muted_check_box.setChecked(settings.muted)
        self._repeat_combo_box.setCurrentIndex(self._repeat_index(settings.repeat_mode))
        self._shuffle_check_box.setChecked(settings.shuffle_enabled)
        self._waveform_check_box.setChecked(settings.waveform_visible)
        self._spectrum_check_box.setChecked(settings.spectrum_visible)
        self._level_meter_check_box.setChecked(settings.level_meter_visible)
        self._clear_error()

    def refresh_if_unedited(self, settings: AppSettings) -> bool:
        """外部（PlayerControls等）の変更を、未編集のときだけ取り込む。

        編集中の入力を勝手に書き換えないため、未適用の編集があれば何もしない。
        取り込んだら ``True``。
        """
        if self.has_unapplied_edits():
            return False
        self.set_settings(settings)
        return True

    def has_unapplied_edits(self) -> bool:
        """入力が適用済みsnapshotと異なるか。"""
        return self.current_input() != self._applied

    def current_input(self) -> AppSettings:
        """入力中の値を設定値オブジェクトへまとめる（適用はしない）。"""
        return AppSettings(
            playback_rate=self._rate_spin_box.value(),
            pitch_compensation=self._pitch_check_box.isChecked(),
            waveform_visible=self._waveform_check_box.isChecked(),
            spectrum_visible=self._spectrum_check_box.isChecked(),
            level_meter_visible=self._level_meter_check_box.isChecked(),
            volume=self._volume_spin_box.value() / _VOLUME_SCALE,
            muted=self._muted_check_box.isChecked(),
            repeat_mode=self._current_repeat_mode(),
            shuffle_enabled=self._shuffle_check_box.isChecked(),
        )

    def mark_applied(self, settings: AppSettings) -> None:
        """調停サービスでの適用成功を反映する。"""
        self._request_succeeded = True
        self.set_settings(settings)

    def show_apply_error(self, message: str = APPLY_FAILED_MESSAGE) -> None:
        """調停サービスでの適用失敗を表示し、入力と適用済みsnapshotを維持する。"""
        self._request_succeeded = False
        self._show_error(message)

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
        self._request_succeeded = False
        self.settings_requested.emit(settings)
        if not self._request_succeeded:
            if not self.error_text:
                self._show_error(APPLY_FAILED_MESSAGE)
            return False
        return True

    # -- 内部 ---------------------------------------------------------------

    def _make_label(self, text: str, buddy: QWidget) -> QLabel:
        label = QLabel(text, self)
        label.setBuddy(buddy)
        return label

    def _set_volume(self, volume: float) -> None:
        value = round(volume * _VOLUME_SCALE)
        for widget in (self._volume_slider, self._volume_spin_box):
            # 双方向同期のSignalが再帰しないよう、反映中は通知を止める。
            blocker = QSignalBlocker(widget)
            widget.setValue(value)
            del blocker

    def _on_volume_slider_changed(self, value: int) -> None:
        if self._volume_spin_box.value() != value:
            self._volume_spin_box.setValue(value)

    def _on_volume_spin_box_changed(self, value: int) -> None:
        if self._volume_slider.value() != value:
            self._volume_slider.setValue(value)

    def _repeat_index(self, mode: RepeatModeSetting) -> int:
        for index, (candidate, _) in enumerate(REPEAT_MODE_LABELS):
            if candidate is mode:
                return index
        return 0

    def _current_repeat_mode(self) -> RepeatModeSetting:
        data: object = self._repeat_combo_box.currentData()
        if isinstance(data, RepeatModeSetting):
            return data
        return RepeatModeSetting.OFF

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

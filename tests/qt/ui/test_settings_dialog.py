"""SettingsDialogの表示・Apply／OK／Cancel契約・不正入力の扱いを検証する。

ダイアログは適用も保存も行わず、要求を通知するだけであることを確かめる。
"""

from collections.abc import Iterator

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
)
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from sdp.core.playback.preferences import MAX_PLAYBACK_RATE, MIN_PLAYBACK_RATE
from sdp.services.settings import AppSettings
from sdp.ui.settings_dialog import INVALID_MESSAGE, SettingsDialog

CURRENT = AppSettings(
    playback_rate=1.25,
    pitch_compensation=False,
    waveform_visible=True,
    spectrum_visible=False,
    level_meter_visible=True,
)


@pytest.fixture
def dialog(qtbot: QtBot) -> Iterator[SettingsDialog]:
    instance = SettingsDialog(CURRENT)
    qtbot.addWidget(instance)
    yield instance


def spin_box(dialog: SettingsDialog) -> QDoubleSpinBox:
    widget = dialog.findChild(QDoubleSpinBox, "settingsPlaybackRateSpinBox")
    assert widget is not None
    return widget


def check_box(dialog: SettingsDialog, name: str) -> QCheckBox:
    widget = dialog.findChild(QCheckBox, name)
    assert widget is not None
    return widget


def button(dialog: SettingsDialog, standard: QDialogButtonBox.StandardButton) -> QAbstractButton:
    box = dialog.findChild(QDialogButtonBox, "settingsButtonBox")
    assert box is not None
    widget: QAbstractButton | None = box.button(standard)
    assert widget is not None
    return widget


def requests_of(dialog: SettingsDialog) -> list[object]:
    received: list[object] = []
    dialog.settings_requested.connect(received.append)
    return received


# -- 構造 -------------------------------------------------------------------


def test_object_names_and_accessible_names(dialog: SettingsDialog) -> None:
    """設定項目をobjectNameとaccessibleNameで識別できる。"""
    assert dialog.objectName() == "settingsDialog"
    assert dialog.windowTitle() == "設定"
    assert spin_box(dialog).accessibleName() == "再生速度"
    for name in (
        "settingsPitchCompensationCheckBox",
        "settingsWaveformVisibleCheckBox",
        "settingsSpectrumVisibleCheckBox",
        "settingsLevelMeterVisibleCheckBox",
    ):
        assert check_box(dialog, name).accessibleName() != ""


def test_rate_input_matches_the_playback_range(dialog: SettingsDialog) -> None:
    """速度入力はSpeedPanelと同じ範囲・刻み・小数桁にする。"""
    widget = spin_box(dialog)

    assert widget.minimum() == pytest.approx(MIN_PLAYBACK_RATE)
    assert widget.maximum() == pytest.approx(MAX_PLAYBACK_RATE)
    assert widget.singleStep() == pytest.approx(0.05)
    assert widget.decimals() == 2


def test_dialog_has_ok_cancel_and_apply(dialog: SettingsDialog) -> None:
    """OK／Cancel／ApplyをQDialogButtonBoxで持つ。"""
    for standard in (
        QDialogButtonBox.StandardButton.Ok,
        QDialogButtonBox.StandardButton.Cancel,
        QDialogButtonBox.StandardButton.Apply,
    ):
        assert button(dialog, standard) is not None


def test_dialog_does_not_reference_the_settings_file(dialog: SettingsDialog) -> None:
    """ダイアログはJSONやschema versionを知らない。"""
    del dialog
    import sdp.ui.settings_dialog as dialog_module

    for forbidden in (
        "json",
        "load_settings",
        "save_settings",
        "SettingsSession",
        "SETTINGS_SCHEMA_VERSION",
        "PlaybackController",
    ):
        assert not hasattr(dialog_module, forbidden), forbidden


# -- 初期表示 ---------------------------------------------------------------


def test_opens_with_the_applied_settings(dialog: SettingsDialog) -> None:
    """開いた時点の適用済み設定を各入力へ反映する。"""
    assert spin_box(dialog).value() == pytest.approx(1.25)
    assert check_box(dialog, "settingsPitchCompensationCheckBox").isChecked() is False
    assert check_box(dialog, "settingsWaveformVisibleCheckBox").isChecked() is True
    assert check_box(dialog, "settingsSpectrumVisibleCheckBox").isChecked() is False
    assert check_box(dialog, "settingsLevelMeterVisibleCheckBox").isChecked() is True
    assert dialog.applied_settings == CURRENT
    assert dialog.error_text == ""


def test_editing_does_not_request_anything(dialog: SettingsDialog) -> None:
    """入力を変えただけでは適用要求を出さない。"""
    received = requests_of(dialog)

    spin_box(dialog).setValue(1.75)
    check_box(dialog, "settingsWaveformVisibleCheckBox").setChecked(False)

    assert received == []
    assert dialog.applied_settings == CURRENT


# -- Apply / OK / Cancel ----------------------------------------------------


def test_apply_requests_without_closing(dialog: SettingsDialog, qtbot: QtBot) -> None:
    """Applyは適用要求を出すが閉じない。"""
    dialog.show()
    qtbot.waitExposed(dialog)
    received = requests_of(dialog)
    spin_box(dialog).setValue(1.5)
    check_box(dialog, "settingsSpectrumVisibleCheckBox").setChecked(True)

    apply_button = button(dialog, QDialogButtonBox.StandardButton.Apply)
    apply_button.click()

    assert received == [
        AppSettings(
            playback_rate=1.5,
            pitch_compensation=False,
            waveform_visible=True,
            spectrum_visible=True,
            level_meter_visible=True,
        )
    ]
    assert dialog.isVisible()
    assert dialog.applied_settings.playback_rate == pytest.approx(1.5)


def test_ok_requests_and_closes(dialog: SettingsDialog, qtbot: QtBot) -> None:
    """OKはApplyと同じ適用要求のあとに閉じる。"""
    dialog.show()
    qtbot.waitExposed(dialog)
    received = requests_of(dialog)
    check_box(dialog, "settingsLevelMeterVisibleCheckBox").setChecked(False)

    ok_button = button(dialog, QDialogButtonBox.StandardButton.Ok)
    ok_button.click()

    assert len(received) == 1
    assert isinstance(received[0], AppSettings)
    assert received[0].level_meter_visible is False
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_cancel_discards_only_unapplied_edits(dialog: SettingsDialog, qtbot: QtBot) -> None:
    """CancelはApply後の変更を戻さず、未適用の編集だけを捨てる。"""
    dialog.show()
    qtbot.waitExposed(dialog)
    received = requests_of(dialog)
    spin_box(dialog).setValue(1.5)
    apply_button = button(dialog, QDialogButtonBox.StandardButton.Apply)
    apply_button.click()
    # Apply後にさらに編集してからCancelする。
    spin_box(dialog).setValue(2.0)

    cancel_button = button(dialog, QDialogButtonBox.StandardButton.Cancel)
    cancel_button.click()

    assert len(received) == 1
    assert dialog.applied_settings.playback_rate == pytest.approx(1.5)
    assert spin_box(dialog).value() == pytest.approx(1.5)
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_cancel_without_apply_requests_nothing(dialog: SettingsDialog, qtbot: QtBot) -> None:
    """一度もApplyしていなければ、Cancelで何も要求しない。"""
    dialog.show()
    qtbot.waitExposed(dialog)
    received = requests_of(dialog)
    spin_box(dialog).setValue(2.0)

    dialog.reject()

    assert received == []
    assert dialog.applied_settings == CURRENT


def test_escape_key_cancels(dialog: SettingsDialog, qtbot: QtBot) -> None:
    """Escでも未適用の編集だけを破棄して閉じる。"""
    dialog.show()
    qtbot.waitExposed(dialog)
    received = requests_of(dialog)
    check_box(dialog, "settingsWaveformVisibleCheckBox").setChecked(False)

    QTest.keyClick(dialog, Qt.Key.Key_Escape)

    assert received == []
    assert check_box(dialog, "settingsWaveformVisibleCheckBox").isChecked() is True


def test_applying_the_same_values_still_requests_once(dialog: SettingsDialog) -> None:
    """同値Applyでも要求は1回だけ（通知の抑制は調停サービスの責務）。"""
    received = requests_of(dialog)

    assert dialog.apply_settings() is True

    assert received == [CURRENT]


def test_set_settings_refreshes_the_inputs(dialog: SettingsDialog) -> None:
    """外部で設定が変わった場合も、開き直しで現在値へ揃う。"""
    dialog.set_settings(AppSettings(0.5, True, False, False, False))

    assert spin_box(dialog).value() == pytest.approx(0.5)
    assert check_box(dialog, "settingsSpectrumVisibleCheckBox").isChecked() is False
    assert dialog.applied_settings.playback_rate == pytest.approx(0.5)


# -- 不正入力 ---------------------------------------------------------------


def test_invalid_input_is_not_applied(
    dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """プログラム経由の不正値では適用要求を出さず、短いエラーを表示する。"""
    received = requests_of(dialog)

    def invalid_input() -> AppSettings:
        return AppSettings(9.9, True)

    monkeypatch.setattr(dialog, "current_input", invalid_input)

    assert dialog.apply_settings() is False

    assert received == []
    assert dialog.error_text == INVALID_MESSAGE
    assert dialog.applied_settings == CURRENT


def test_ok_with_invalid_input_does_not_close(
    dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot
) -> None:
    """不正値のままOKしても閉じない。"""
    dialog.show()
    qtbot.waitExposed(dialog)

    def invalid_input() -> AppSettings:
        return AppSettings(1.0, True, spectrum_visible=1)  # type: ignore[arg-type]

    monkeypatch.setattr(dialog, "current_input", invalid_input)
    ok_button = button(dialog, QDialogButtonBox.StandardButton.Ok)
    ok_button.click()

    assert dialog.isVisible()
    assert dialog.error_text == INVALID_MESSAGE


def test_error_is_cleared_after_a_successful_apply(
    dialog: SettingsDialog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """適用に成功すればエラー表示を消す。"""

    def invalid_input() -> AppSettings:
        return AppSettings(9.9, True)

    monkeypatch.setattr(dialog, "current_input", invalid_input)
    dialog.apply_settings()
    monkeypatch.undo()

    assert dialog.apply_settings() is True
    assert dialog.error_text == ""


# -- 後始末 -----------------------------------------------------------------


def test_deleted_dialog_leaves_no_callback(qtbot: QtBot) -> None:
    """破棄後にtimerやcallbackを残さない。"""
    instance = SettingsDialog(CURRENT)
    qtbot.addWidget(instance)
    received = requests_of(instance)

    instance.deleteLater()
    qtbot.waitUntil(lambda: not isValid(instance))

    assert received == []

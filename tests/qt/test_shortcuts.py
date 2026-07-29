"""QTest.keyClickでアプリ内ショートカットの入力経路を検証する。"""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.ui.shortcuts import (
    SHORTCUT_SPECS,
    ShortcutManager,
    ShortcutSpec,
    relative_seek_target,
)


class Harness:
    """ショートカット統合テストの構成一式。"""

    def __init__(self, qtbot: QtBot) -> None:
        self.backend = FakePlaybackBackend(duration_ms=120_000)
        self.playback = PlaybackController(self.backend)
        self.playlist = PlaylistModel()
        self.playlist_playback = PlaylistPlaybackController(self.playback, self.playlist)
        self.window = QWidget()
        self.window.resize(400, 200)
        self.manager = ShortcutManager(
            self.window, self.playback, self.playlist_playback, parent=self.window
        )
        qtbot.addWidget(self.window)
        self.window.show()
        self.window.activateWindow()


@pytest.fixture
def harness(qtbot: QtBot) -> Iterator[Harness]:
    yield Harness(qtbot)


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / "曲A.wav", tmp_path / "曲B.wav"]
    for path in paths:
        path.write_bytes(b"x")
    return paths


def press(
    harness: Harness,
    key: Qt.Key,
    modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    harness.window.setFocus()
    QTest.keyClick(harness.window, key, modifier)


def load_source(harness: Harness, source: Path) -> None:
    harness.playback.load(source)
    harness.backend.calls.clear()


def test_shortcut_specs_are_immutable_unique_and_described() -> None:
    """定義は不変で、操作ID・キーに重複や空説明がない。"""
    with pytest.raises(FrozenInstanceError):
        SHORTCUT_SPECS[0].sequence = "X"  # type: ignore[misc]
    assert len({spec.action_id for spec in SHORTCUT_SPECS}) == len(SHORTCUT_SPECS)
    assert len({spec.sequence for spec in SHORTCUT_SPECS}) == len(SHORTCUT_SPECS)
    assert all(spec.description for spec in SHORTCUT_SPECS)


def test_shortcut_spec_shape() -> None:
    """将来の一覧表示も同じ4項目を利用できる。"""
    assert ShortcutSpec.__match_args__ == (
        "action_id",
        "sequence",
        "description",
        "auto_repeat",
    )


def test_shortcuts_use_window_context_and_expected_auto_repeat(harness: Harness) -> None:
    """WindowShortcutで、連続値操作だけautoRepeatを許可する。"""
    shortcuts = harness.window.findChildren(QShortcut)
    assert len(shortcuts) == len(SHORTCUT_SPECS)
    by_name = {shortcut.objectName(): shortcut for shortcut in shortcuts}
    for spec in SHORTCUT_SPECS:
        shortcut = by_name[f"shortcut_{spec.action_id.name.lower()}"]
        assert shortcut.context() is Qt.ShortcutContext.WindowShortcut
        assert shortcut.autoRepeat() is spec.auto_repeat


def test_manager_constructor_has_no_backend_or_model_dependency() -> None:
    """入力アダプターは2つのControllerとWindowだけを受け取る。"""
    import inspect

    assert list(inspect.signature(ShortcutManager.__init__).parameters) == [
        "self",
        "window",
        "playback",
        "playlist_playback",
        "parent",
    ]


@pytest.mark.parametrize(
    ("position", "duration", "delta", "expected"),
    [
        (5_000, 120_000, -10_000, 0),
        (115_000, 120_000, 10_000, 120_000),
        (5_000, 0, 60_000, 65_000),
    ],
)
def test_relative_seek_boundaries(position: int, duration: int, delta: int, expected: int) -> None:
    """相対シークを0と既知durationへ収め、未知durationでは上限を設けない。"""
    assert relative_seek_target(position, duration, delta) == expected


def test_space_toggles_play_and_pause(harness: Harness, audio_files: list[Path]) -> None:
    """SpaceはSTOPPED→PLAYING→PAUSEDを切り替える。"""
    load_source(harness, audio_files[0])

    press(harness, Qt.Key.Key_Space)
    press(harness, Qt.Key.Key_Space)

    assert harness.backend.call_names() == ["play", "pause"]
    assert harness.playback.state is PlaybackState.PAUSED


def test_space_and_stop_without_source_are_no_ops(harness: Harness) -> None:
    """sourceなしでは再生・停止を勝手に開始しない。"""
    press(harness, Qt.Key.Key_Space)
    press(harness, Qt.Key.Key_S)
    assert harness.backend.call_names() == []


def test_stop_with_source_delegates_once(harness: Harness, audio_files: list[Path]) -> None:
    """Sはsourceがあるときだけstopへ委譲する。"""
    load_source(harness, audio_files[0])
    press(harness, Qt.Key.Key_S)
    assert harness.backend.call_names() == ["stop"]


@pytest.mark.parametrize(
    ("key", "modifier", "position", "expected"),
    [
        (Qt.Key.Key_J, Qt.KeyboardModifier.NoModifier, 30_000, 20_000),
        (Qt.Key.Key_L, Qt.KeyboardModifier.NoModifier, 30_000, 40_000),
        (Qt.Key.Key_J, Qt.KeyboardModifier.ShiftModifier, 30_000, 0),
        (Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier, 80_000, 120_000),
    ],
)
def test_seek_shortcuts(
    harness: Harness,
    audio_files: list[Path],
    key: Qt.Key,
    modifier: Qt.KeyboardModifier,
    position: int,
    expected: int,
) -> None:
    """J/LとShift付きJ/Lを境界内の相対位置へ1回だけseekする。"""
    load_source(harness, audio_files[0])
    harness.backend.emit_position(position)
    harness.backend.calls.clear()

    press(harness, key, modifier)

    assert harness.backend.call_args("seek") == [(expected,)]


def test_seek_without_source_is_no_op(harness: Harness) -> None:
    """sourceなしの相対seekは何もしない。"""
    press(harness, Qt.Key.Key_L)
    assert harness.backend.call_names() == []


def test_previous_and_next_delegate_to_playlist_controller(
    harness: Harness, audio_files: list[Path]
) -> None:
    """前後曲の探索を複製せずPlaylistPlaybackControllerへ委譲する。"""
    entry_ids = harness.playlist.add_paths(audio_files)
    assert harness.playlist_playback.play_entry(entry_ids[0])
    harness.backend.calls.clear()

    press(harness, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    press(harness, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)

    assert harness.backend.call_args("load") == [
        (audio_files[1].resolve(),),
        (audio_files[0].resolve(),),
    ]


def test_volume_shortcuts_clamp_and_do_not_change_mute(harness: Harness) -> None:
    """音量は0.05刻みで0～1へ収め、muteを変更しない。"""
    harness.playback.set_volume(0.95)
    harness.backend.calls.clear()
    press(harness, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    press(harness, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)
    assert harness.playback.volume == 1.0
    assert harness.playback.muted is False
    assert harness.backend.call_args("set_volume") == [(1.0,)]

    harness.playback.set_volume(0.05)
    harness.backend.calls.clear()
    press(harness, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
    press(harness, Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier)
    assert harness.playback.volume == 0.0
    assert harness.backend.call_args("set_volume") == [(0.0,)]


def test_mute_shortcut_changes_only_mute(harness: Harness) -> None:
    """Mは音量値を変えずmuteだけを反転する。"""
    volume = harness.playback.volume
    press(harness, Qt.Key.Key_M)
    assert harness.playback.muted is True
    assert harness.playback.volume == volume


def test_rate_shortcuts_clamp_reset_and_keep_pitch(harness: Harness) -> None:
    """X/Cは0.05刻みでUI範囲へ収め、Zは速度だけ1.0へ戻す。"""
    harness.playback.set_playback_rate(1.0)
    harness.playback.set_pitch_compensation(False)
    harness.backend.calls.clear()
    press(harness, Qt.Key.Key_X)
    press(harness, Qt.Key.Key_C)
    assert harness.backend.call_args("set_playback_rate") == [(0.95,), (1.0,)]

    harness.playback.set_playback_rate(2.0)
    harness.backend.calls.clear()
    press(harness, Qt.Key.Key_C)
    press(harness, Qt.Key.Key_Z)
    assert harness.backend.call_args("set_playback_rate") == [(1.0,)]
    assert harness.playback.pitch_compensation is False


def test_rate_adjustment_outside_ui_range_is_no_op_but_reset_works(harness: Harness) -> None:
    """範囲外の増減は曖昧にclampせず、Zだけで明示的に復帰する。"""
    harness.backend.playback_rate_changed.emit(3.0)
    harness.backend.calls.clear()
    press(harness, Qt.Key.Key_X)
    press(harness, Qt.Key.Key_C)
    assert harness.backend.call_names() == []
    press(harness, Qt.Key.Key_Z)
    assert harness.backend.call_args("set_playback_rate") == [(1.0,)]


def test_pitch_repeat_and_shuffle_shortcuts(harness: Harness) -> None:
    """P/R/Ctrl+Hは各Controllerの既存操作へ委譲する。"""
    press(harness, Qt.Key.Key_P)
    assert harness.playback.pitch_compensation is False

    for expected in (RepeatMode.ALL, RepeatMode.ONE, RepeatMode.OFF):
        press(harness, Qt.Key.Key_R)
        assert harness.playlist_playback.repeat_mode is expected

    press(harness, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
    assert harness.playlist_playback.shuffle_enabled is True
    press(harness, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
    assert harness.playlist_playback.shuffle_enabled is False


def test_shortcuts_do_not_load_or_reorder_playlist(harness: Harness) -> None:
    """単曲設定shortcutはsourceやプレイリスト順を変更しない。"""
    before = harness.playlist.entries()
    for key in (Qt.Key.Key_M, Qt.Key.Key_X, Qt.Key.Key_C, Qt.Key.Key_Z, Qt.Key.Key_P):
        press(harness, key)
    assert harness.backend.call_args("load") == []
    assert harness.playlist.entries() == before


def test_spinbox_focus_suppresses_character_shortcuts(harness: Harness, qtbot: QtBot) -> None:
    """数値編集中のC/Xや矢印をアプリ操作へ流さない。"""
    spin = QDoubleSpinBox(harness.window)
    spin.show()
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus)
    harness.backend.calls.clear()

    QTest.keyClick(spin, Qt.Key.Key_C)
    QTest.keyClick(spin, Qt.Key.Key_X)
    QTest.keyClick(spin, Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier)

    assert harness.backend.call_names() == []


@pytest.mark.parametrize(
    ("key", "modifier"),
    [
        (Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier),
        (
            Qt.Key.Key_O,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        ),
    ],
)
def test_spinbox_focus_keeps_unmanaged_window_shortcuts(
    harness: Harness,
    qtbot: QtBot,
    key: Qt.Key,
    modifier: Qt.KeyboardModifier,
) -> None:
    """SpinBox編集中も既存のCtrl+O系QActionを奪わない。"""
    spin = QDoubleSpinBox(harness.window)
    spin.show()
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus)
    triggered: list[bool] = []
    action = QAction(harness.window)
    action.setShortcut("Ctrl+Shift+O" if modifier & Qt.KeyboardModifier.ShiftModifier else "Ctrl+O")
    action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
    action.triggered.connect(lambda: triggered.append(True))
    harness.window.addAction(action)

    QTest.keyClick(spin, key, modifier)

    assert triggered == [True]


def test_line_edit_keeps_copy_and_paste_shortcuts(harness: Harness, qtbot: QtBot) -> None:
    """Manager管理外のCtrl+C／Ctrl+VはLineEdit自身へ渡す。"""
    edit = QLineEdit(harness.window)
    edit.setText("copy text")
    edit.show()
    edit.setFocus()
    edit.selectAll()
    qtbot.waitUntil(edit.hasFocus)
    QApplication.clipboard().clear()

    QTest.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    edit.clear()
    QTest.keyClick(edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)

    assert edit.text() == "copy text"
    assert harness.backend.call_names() == []


def test_button_space_clicks_only_the_button(harness: Harness, qtbot: QtBot) -> None:
    """ボタンfocus中のSpaceは再生切替と二重発火しない。"""
    button = QPushButton("操作", harness.window)
    clicks: list[bool] = []
    button.clicked.connect(lambda: clicks.append(True))
    button.show()
    button.setFocus()
    qtbot.waitUntil(button.hasFocus)

    QTest.keyClick(button, Qt.Key.Key_Space)

    assert clicks == [True]
    assert harness.backend.call_names() == []


def test_modal_dialog_suppresses_window_shortcuts(harness: Harness, qtbot: QtBot) -> None:
    """モーダル表示中は背後のMainWindow操作を発火しない。"""
    dialog = QDialog(harness.window)
    dialog.setModal(True)
    dialog.setLayout(QVBoxLayout())
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.activateWindow()
    qtbot.waitUntil(dialog.isActiveWindow)

    QTest.keyClick(dialog, Qt.Key.Key_M)

    assert harness.backend.call_names() == []


def test_manager_deletion_disconnects_shortcuts(harness: Harness, qtbot: QtBot) -> None:
    """ManagerのQObject削除後はShortcutが破棄済みslotを呼ばない。"""
    harness.manager.deleteLater()
    qtbot.waitUntil(lambda: not isValid(harness.manager))
    assert not isValid(harness.manager)

    press(harness, Qt.Key.Key_M)
    assert harness.backend.call_names() == []

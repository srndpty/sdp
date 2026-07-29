"""MainWindow内で有効なキーボードショートカット。"""

import logging
from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.preferences import (
    DEFAULT_PLAYBACK_RATE,
    MAX_PLAYBACK_RATE,
    MIN_PLAYBACK_RATE,
    PLAYBACK_RATE_STEP,
)
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.playback_controller import PlaylistPlaybackController

_logger = logging.getLogger(__name__)

SEEK_SHORT_MS = 10_000
SEEK_LONG_MS = 60_000
VOLUME_STEP = 0.05


class ShortcutAction(Enum):
    """ショートカットから要求できる操作。"""

    PLAY_PAUSE = auto()
    STOP = auto()
    SEEK_BACKWARD = auto()
    SEEK_FORWARD = auto()
    SEEK_BACKWARD_LONG = auto()
    SEEK_FORWARD_LONG = auto()
    PREVIOUS_TRACK = auto()
    NEXT_TRACK = auto()
    VOLUME_UP = auto()
    VOLUME_DOWN = auto()
    TOGGLE_MUTE = auto()
    RATE_DOWN = auto()
    RATE_UP = auto()
    RESET_RATE = auto()
    TOGGLE_PITCH = auto()
    CYCLE_REPEAT = auto()
    TOGGLE_SHUFFLE = auto()


@dataclass(frozen=True, slots=True)
class ShortcutSpec:
    """1操作のキー割当と入力特性。"""

    action_id: ShortcutAction
    sequence: str
    description: str
    auto_repeat: bool


SHORTCUT_SPECS = (
    ShortcutSpec(ShortcutAction.PLAY_PAUSE, "Space", "再生／一時停止", False),
    ShortcutSpec(ShortcutAction.STOP, "S", "停止", False),
    ShortcutSpec(ShortcutAction.SEEK_BACKWARD, "J", "10秒戻る", True),
    ShortcutSpec(ShortcutAction.SEEK_FORWARD, "L", "10秒進む", True),
    ShortcutSpec(ShortcutAction.SEEK_BACKWARD_LONG, "Shift+J", "60秒戻る", True),
    ShortcutSpec(ShortcutAction.SEEK_FORWARD_LONG, "Shift+L", "60秒進む", True),
    ShortcutSpec(ShortcutAction.PREVIOUS_TRACK, "Alt+Left", "前の曲", False),
    ShortcutSpec(ShortcutAction.NEXT_TRACK, "Alt+Right", "次の曲", False),
    ShortcutSpec(ShortcutAction.VOLUME_UP, "Ctrl+Up", "音量を5ポイント上げる", True),
    ShortcutSpec(ShortcutAction.VOLUME_DOWN, "Ctrl+Down", "音量を5ポイント下げる", True),
    ShortcutSpec(ShortcutAction.TOGGLE_MUTE, "M", "ミュート切替", False),
    ShortcutSpec(ShortcutAction.RATE_DOWN, "X", "再生速度を0.05下げる", True),
    ShortcutSpec(ShortcutAction.RATE_UP, "C", "再生速度を0.05上げる", True),
    ShortcutSpec(ShortcutAction.RESET_RATE, "Z", "再生速度を1.0倍へ戻す", False),
    ShortcutSpec(ShortcutAction.TOGGLE_PITCH, "P", "ピッチ補正切替", False),
    ShortcutSpec(ShortcutAction.CYCLE_REPEAT, "R", "リピートモード切替", False),
    ShortcutSpec(ShortcutAction.TOGGLE_SHUFFLE, "Ctrl+H", "シャッフル切替", False),
)


def relative_seek_target(position_ms: int, duration_ms: int, delta_ms: int) -> int:
    """現在位置から相対シーク先を計算し、既知の範囲へ収める。"""
    target = max(0, position_ms + delta_ms)
    return min(target, duration_ms) if duration_ms > 0 else target


class ShortcutManager(QObject):
    """QShortcutを既存Controllerの操作へ変換する小さな入力アダプター。"""

    def __init__(
        self,
        window: QWidget,
        playback: PlaybackController,
        playlist_playback: PlaylistPlaybackController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._playback = playback
        self._playlist_playback = playlist_playback
        self._managed_sequences = frozenset(
            QKeySequence(spec.sequence).toString(QKeySequence.SequenceFormat.PortableText)
            for spec in SHORTCUT_SPECS
        )
        app = QApplication.instance()
        if isinstance(app, QApplication):
            # QShortcutが入力を消費する前のShortcutOverrideで、編集Widgetと
            # ボタンのSpaceへキーを譲る。
            app.installEventFilter(self)
        self._actions: dict[QShortcut, ShortcutAction] = {}
        for spec in SHORTCUT_SPECS:
            shortcut = QShortcut(QKeySequence(spec.sequence), window)
            shortcut.setObjectName(f"shortcut_{spec.action_id.name.lower()}")
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.setAutoRepeat(spec.auto_repeat)
            self._actions[shortcut] = spec.action_id
            shortcut.activated.connect(self._on_activated)

    def _on_activated(self) -> None:
        shortcut = self.sender()
        if isinstance(shortcut, QShortcut):
            self._dispatch_if_allowed(self._actions[shortcut])

    def _dispatch_if_allowed(self, action: ShortcutAction) -> None:
        if QApplication.activeModalWidget() is not None or self._editing_widget_has_focus(action):
            return
        self._dispatch(action)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() is QEvent.Type.ShortcutOverride and self._should_override_shortcut(event):
            event.accept()
        return super().eventFilter(watched, event)

    def _should_override_shortcut(self, event: QEvent) -> bool:
        if not isinstance(event, QKeyEvent):
            return False
        # Ctrl+Oや編集Widget自身のCtrl+C等、Managerの管理外キーは奪わない。
        sequence = QKeySequence(event.keyCombination()).toString(
            QKeySequence.SequenceFormat.PortableText
        )
        if sequence not in self._managed_sequences:
            return False
        focused = QApplication.focusWidget()
        if focused is None or not self._window.isAncestorOf(focused):
            return False
        if isinstance(focused, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        if isinstance(focused, QComboBox) and focused.isEditable():
            return True
        return isinstance(focused, QAbstractButton) and event.key() == Qt.Key.Key_Space

    def _editing_widget_has_focus(self, action: ShortcutAction) -> bool:
        focused = QApplication.focusWidget()
        if focused is None or not self._window.isAncestorOf(focused):
            return False
        if isinstance(focused, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
            return True
        if isinstance(focused, QComboBox) and focused.isEditable():
            return True
        return action is ShortcutAction.PLAY_PAUSE and isinstance(focused, QAbstractButton)

    def _dispatch(self, action: ShortcutAction) -> None:
        if action is ShortcutAction.PLAY_PAUSE:
            self._toggle_play_pause()
        elif action is ShortcutAction.STOP:
            if self._playback.source is not None:
                self._playback.stop()
        elif action is ShortcutAction.SEEK_BACKWARD:
            self._seek_relative(-SEEK_SHORT_MS)
        elif action is ShortcutAction.SEEK_FORWARD:
            self._seek_relative(SEEK_SHORT_MS)
        elif action is ShortcutAction.SEEK_BACKWARD_LONG:
            self._seek_relative(-SEEK_LONG_MS)
        elif action is ShortcutAction.SEEK_FORWARD_LONG:
            self._seek_relative(SEEK_LONG_MS)
        elif action is ShortcutAction.PREVIOUS_TRACK:
            self._playlist_playback.play_previous()
        elif action is ShortcutAction.NEXT_TRACK:
            self._playlist_playback.play_next()
        elif action is ShortcutAction.VOLUME_UP:
            self._adjust_volume(VOLUME_STEP)
        elif action is ShortcutAction.VOLUME_DOWN:
            self._adjust_volume(-VOLUME_STEP)
        elif action is ShortcutAction.TOGGLE_MUTE:
            self._playback.set_muted(not self._playback.muted)
        elif action is ShortcutAction.RATE_DOWN:
            self._adjust_rate(-PLAYBACK_RATE_STEP)
        elif action is ShortcutAction.RATE_UP:
            self._adjust_rate(PLAYBACK_RATE_STEP)
        elif action is ShortcutAction.RESET_RATE:
            self._playback.set_playback_rate(DEFAULT_PLAYBACK_RATE)
        elif action is ShortcutAction.TOGGLE_PITCH:
            self._playback.set_pitch_compensation(not self._playback.pitch_compensation)
        elif action is ShortcutAction.CYCLE_REPEAT:
            self._playlist_playback.cycle_repeat_mode()
        elif action is ShortcutAction.TOGGLE_SHUFFLE:
            self._playlist_playback.set_shuffle_enabled(not self._playlist_playback.shuffle_enabled)

    def _toggle_play_pause(self) -> None:
        if self._playback.source is None or self._playback.state is PlaybackState.NO_MEDIA:
            return
        if self._playback.state is PlaybackState.PLAYING:
            self._playback.pause()
        else:
            self._playback.play()

    def _seek_relative(self, delta_ms: int) -> None:
        if self._playback.source is None:
            return
        self._playback.seek(
            relative_seek_target(
                self._playback.position_ms,
                self._playback.duration_ms,
                delta_ms,
            )
        )

    def _adjust_volume(self, delta: float) -> None:
        volume = round(min(1.0, max(0.0, self._playback.volume + delta)), 2)
        if volume != self._playback.volume:
            self._playback.set_volume(volume)

    def _adjust_rate(self, delta: float) -> None:
        current = self._playback.playback_rate
        if not MIN_PLAYBACK_RATE <= current <= MAX_PLAYBACK_RATE:
            _logger.warning("UI範囲外の再生速度では増減ショートカットを無視します: %r", current)
            return
        rate = round(min(MAX_PLAYBACK_RATE, max(MIN_PLAYBACK_RATE, current + delta)), 2)
        if rate != current:
            self._playback.set_playback_rate(rate)

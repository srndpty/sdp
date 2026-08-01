"""再生操作ウィジェット（再生ボタン群・シークバー・音量・時間表示）。

PlaybackController の公開 API とシグナルだけを使う。
再生実装（QMediaPlayer など）には触れない。
"""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from sdp.core.metadata.types import format_duration_ms
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.types import RepeatMode

__all__ = ["PlayerControls", "format_duration_ms"]
"""``format_duration_ms`` は core 側の実装をそのまま使う（実装を二重に持たない）。"""

_REPEAT_LABELS: dict[RepeatMode, str] = {
    RepeatMode.OFF: "リピート: オフ",
    RepeatMode.ALL: "リピート: 全曲",
    RepeatMode.ONE: "リピート: 1曲",
}

_STATE_LABELS: dict[PlaybackState, str] = {
    PlaybackState.NO_MEDIA: "ファイルが選択されていません",
    PlaybackState.STOPPED: "停止",
    PlaybackState.PLAYING: "再生中",
    PlaybackState.PAUSED: "一時停止",
}

_VOLUME_SLIDER_MAX = 100


class PlayerControls(QWidget):
    """単曲の再生操作 UI。

    Controller への操作と、Controller からの通知による表示更新だけを行う。
    状態に応じた表示・活性の更新は :meth:`_apply_state` の 1 か所へ集約する。

    前後曲は曲順を知らないと決められないため、このウィジェットは要求を
    シグナルで出すだけにして、プレイリスト側の Controller へは触れない。
    """

    previous_requested = Signal()
    """前の曲が要求された。"""

    next_requested = Signal()
    """次の曲が要求された。"""

    repeat_mode_requested = Signal()
    """リピートの切り替えが要求された。次のモードを決めるのは Controller。"""

    shuffle_toggled = Signal(bool)
    """シャッフルの ON/OFF が要求された。"""

    def __init__(self, controller: PlaybackController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        # ユーザーがシークバーをドラッグしている間は、Backend からの位置通知で
        # つまみを動かさない（操作が奪われるため）。
        self._is_seeking = False

        self._repeat_button = QPushButton(_REPEAT_LABELS[RepeatMode.OFF])
        self._repeat_button.setObjectName("repeatModeButton")
        self._repeat_button.setAccessibleName("リピート")
        self._repeat_button.setToolTip("リピートの種類を切り替えます（オフ → 全曲 → 1曲）")
        self._shuffle_button = QPushButton("シャッフル")
        self._shuffle_button.setObjectName("shuffleButton")
        self._shuffle_button.setAccessibleName("シャッフル")
        self._shuffle_button.setCheckable(True)
        self._shuffle_button.setToolTip("プレイリストをランダムな順で再生します")

        self._previous_button = QPushButton("前の曲")
        self._previous_button.setObjectName("previousTrackButton")
        self._previous_button.setAccessibleName("前の曲")
        self._previous_button.setEnabled(False)
        self._next_button = QPushButton("次の曲")
        self._next_button.setObjectName("nextTrackButton")
        self._next_button.setAccessibleName("次の曲")
        self._next_button.setEnabled(False)

        self._play_button = QPushButton("再生")
        self._play_button.setObjectName("playButton")
        self._play_button.setAccessibleName("再生")
        self._pause_button = QPushButton("一時停止")
        self._pause_button.setObjectName("pauseButton")
        self._pause_button.setAccessibleName("一時停止")
        self._stop_button = QPushButton("停止")
        self._stop_button.setObjectName("stopButton")
        self._stop_button.setAccessibleName("停止")

        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setObjectName("seekSlider")
        self._seek_slider.setAccessibleName("再生位置")
        self._seek_slider.setRange(0, 0)

        self._position_label = QLabel(format_duration_ms(0))
        self._position_label.setObjectName("positionLabel")
        self._duration_label = QLabel(format_duration_ms(0))
        self._duration_label.setObjectName("durationLabel")

        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setObjectName("volumeSlider")
        self._volume_slider.setAccessibleName("音量")
        self._volume_slider.setRange(0, _VOLUME_SLIDER_MAX)
        self._mute_button = QPushButton("ミュート")
        self._mute_button.setObjectName("muteButton")
        self._mute_button.setAccessibleName("ミュート")
        self._mute_button.setCheckable(True)

        self._state_label = QLabel(_STATE_LABELS[PlaybackState.NO_MEDIA])
        self._state_label.setObjectName("stateLabel")

        self._build_layout()
        self._connect_widgets()
        self._connect_controller()
        self._initialize_from_controller()

    # -- 構築 ---------------------------------------------------------------

    def _build_layout(self) -> None:
        seek_row = QHBoxLayout()
        seek_row.addWidget(self._position_label)
        seek_row.addWidget(self._seek_slider, stretch=1)
        seek_row.addWidget(self._duration_label)

        button_row = QHBoxLayout()
        button_row.addWidget(self._previous_button)
        button_row.addWidget(self._play_button)
        button_row.addWidget(self._pause_button)
        button_row.addWidget(self._stop_button)
        button_row.addWidget(self._next_button)
        button_row.addStretch(1)
        button_row.addWidget(self._state_label)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self._repeat_button)
        mode_row.addWidget(self._shuffle_button)
        mode_row.addStretch(1)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("音量"))
        volume_row.addWidget(self._volume_slider, stretch=1)
        volume_row.addWidget(self._mute_button)

        layout = QGridLayout(self)
        layout.addLayout(seek_row, 0, 0)
        layout.addLayout(button_row, 1, 0)
        layout.addLayout(mode_row, 2, 0)
        layout.addLayout(volume_row, 3, 0)

    def _connect_widgets(self) -> None:
        # 前後曲は再生実装へ触らず、要求としてだけ外へ出す（配線は MainWindow）。
        self._previous_button.clicked.connect(self.previous_requested)
        self._next_button.clicked.connect(self.next_requested)
        self._repeat_button.clicked.connect(self.repeat_mode_requested)
        self._shuffle_button.toggled.connect(self.shuffle_toggled)
        self._play_button.clicked.connect(self._on_play_clicked)
        self._pause_button.clicked.connect(self._on_pause_clicked)
        self._stop_button.clicked.connect(self._on_stop_clicked)
        self._seek_slider.sliderPressed.connect(self._on_seek_started)
        self._seek_slider.sliderMoved.connect(self._on_seek_moved)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._volume_slider.valueChanged.connect(self._on_volume_slider_changed)
        self._mute_button.toggled.connect(self._on_mute_toggled)

    def _connect_controller(self) -> None:
        self._controller.state_changed.connect(self._apply_state)
        self._controller.position_changed.connect(self._on_position_changed)
        self._controller.duration_changed.connect(self._on_duration_changed)
        self._controller.volume_changed.connect(self._on_controller_volume_changed)
        self._controller.muted_changed.connect(self._on_controller_muted_changed)
        self._controller.source_changed.connect(self._on_source_changed)

    def _initialize_from_controller(self) -> None:
        """初期表示を Controller の公開プロパティから作る。"""
        self._on_controller_volume_changed(self._controller.volume)
        self._on_controller_muted_changed(self._controller.muted)
        self._on_duration_changed(self._controller.duration_ms)
        self._on_position_changed(self._controller.position_ms)
        self._apply_state(self._controller.state)

    # -- 外部からの設定 -----------------------------------------------------

    def set_playlist_navigation_available(self, previous: bool, next_: bool) -> None:
        """前後曲ボタンの活性を設定する。判定はプレイリスト側の責務。"""
        self._previous_button.setEnabled(previous)
        self._next_button.setEnabled(next_)

    def set_repeat_mode(self, mode: RepeatMode) -> None:
        """リピートの表示を更新する。モードを決めるのは Controller。

        色ではなく文字列で区別する。未知の値は曖昧な表示へ丸めず ``KeyError``。
        """
        self._repeat_button.setText(_REPEAT_LABELS[mode])

    def set_shuffle_enabled(self, enabled: bool) -> None:
        """シャッフルボタンの状態を同期する。

        UI 更新が Controller への再設定を呼び戻さないよう、シグナルを止めて反映する。
        """
        with QSignalBlocker(self._shuffle_button):
            self._shuffle_button.setChecked(enabled)

    # -- ウィジェット操作 ---------------------------------------------------

    def _on_play_clicked(self) -> None:
        self._controller.play()

    def _on_pause_clicked(self) -> None:
        self._controller.pause()

    def _on_stop_clicked(self) -> None:
        self._controller.stop()

    def _on_seek_started(self) -> None:
        self._is_seeking = True

    def _on_seek_moved(self, position_ms: int) -> None:
        # ドラッグ中も現在時間ラベルだけは追従させる（seek はまだ行わない）。
        self._position_label.setText(format_duration_ms(position_ms))

    def _on_seek_released(self) -> None:
        if not self._is_seeking:
            return
        self._is_seeking = False
        if not self._seek_slider.isEnabled() or self._seek_slider.maximum() <= 0:
            return
        # ドラッグ中は毎イベント seek せず、離した時点で 1 回だけ転送する。
        # 範囲外の値は Controller が ValueError にする契約であり、ここでは握り潰さない。
        self._controller.seek(self._seek_slider.value())

    def _on_volume_slider_changed(self, value: int) -> None:
        self._controller.set_volume(value / _VOLUME_SLIDER_MAX)

    def _on_mute_toggled(self, checked: bool) -> None:
        self._controller.set_muted(checked)

    # -- Controller からの通知 ----------------------------------------------

    def _apply_state(self, state: PlaybackState) -> None:
        """再生状態に応じて表示とボタンの活性を更新する（唯一の更新経路）。"""
        self._play_button.setEnabled(state in {PlaybackState.STOPPED, PlaybackState.PAUSED})
        self._pause_button.setEnabled(state is PlaybackState.PLAYING)
        self._stop_button.setEnabled(state in {PlaybackState.PLAYING, PlaybackState.PAUSED})
        self._state_label.setText(_STATE_LABELS[state])
        self._update_seek_enabled()

    def _update_seek_enabled(self) -> None:
        has_media = self._controller.state is not PlaybackState.NO_MEDIA
        self._seek_slider.setEnabled(has_media and self._seek_slider.maximum() > 0)

    def _on_position_changed(self, position_ms: int) -> None:
        if self._is_seeking:
            return
        with QSignalBlocker(self._seek_slider):
            self._seek_slider.setValue(position_ms)
        self._position_label.setText(format_duration_ms(position_ms))

    def _on_duration_changed(self, duration_ms: int) -> None:
        with QSignalBlocker(self._seek_slider):
            # setMaximum は現在値を範囲内へ収める。duration が縮んでも範囲外にならない。
            self._seek_slider.setMaximum(max(duration_ms, 0))
            if duration_ms <= 0:
                self._seek_slider.setValue(0)
        self._duration_label.setText(format_duration_ms(duration_ms))
        if duration_ms <= 0:
            self._position_label.setText(format_duration_ms(0))
        self._update_seek_enabled()

    def _on_controller_volume_changed(self, volume: float) -> None:
        # UI 更新が Controller への再設定を呼び戻さないよう、シグナルを止めて反映する。
        with QSignalBlocker(self._volume_slider):
            self._volume_slider.setValue(round(volume * _VOLUME_SLIDER_MAX))

    def _on_controller_muted_changed(self, muted: bool) -> None:
        with QSignalBlocker(self._mute_button):
            self._mute_button.setChecked(muted)

    def _on_source_changed(self, source: object) -> None:
        del source  # 表示するファイル名は MainWindow の責務
        self._is_seeking = False
        self._on_duration_changed(0)

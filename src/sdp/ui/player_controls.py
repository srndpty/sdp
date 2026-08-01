"""再生操作ウィジェット（再生ボタン群・シークバー・音量・時間表示）。

PlaybackController の公開 API とシグナルだけを使う。
再生実装（QMediaPlayer など）には触れない。
"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from sdp.core.metadata.types import format_duration_ms
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.core.playlist.types import RepeatMode

__all__ = ["PlayerControls", "format_duration_ms"]
"""``format_duration_ms`` は core 側の実装をそのまま使う（実装を二重に持たない）。"""

_REPEAT_TOOLTIPS: dict[RepeatMode, str] = {
    RepeatMode.OFF: "リピート: オフ（クリックで全曲リピート）",
    RepeatMode.ALL: "リピート: 全曲（クリックで1曲リピート）",
    RepeatMode.ONE: "リピート: 1曲（クリックでオフ）",
}

_STATE_LABELS: dict[PlaybackState, str] = {
    PlaybackState.NO_MEDIA: "ファイルが選択されていません",
    PlaybackState.STOPPED: "停止",
    PlaybackState.PLAYING: "再生中",
    PlaybackState.PAUSED: "一時停止",
}

_VOLUME_SLIDER_MAX = 100
_ICON_SIZES = (16, 20, 24, 32)

_REPEAT_SVG = """
<path d="M5 6h13m-3-3 3 3-3 3M19 18H6m3-3-3 3 3 3
         M18 6c2 0 3 1 3 3v2M6 18c-2 0-3-1-3-3v-2"/>
"""

_REPEAT_ONE_SVG = (
    _REPEAT_SVG
    + """
<path d="M10.5 11.5 12 10v5m-1.5 0h3"/>
"""
)

_SHUFFLE_SVG = """
<path d="M4 7h2.5c4.5 0 6.5 10 11 10H20m-3-3 3 3-3 3
         M4 17h2.5c1.7 0 2.9-1.4 4-3.2M14 7h6m-3-3 3 3-3 3"/>
"""


def _white_standard_icon(widget: QWidget, standard_pixmap: QStyle.StandardPixmap) -> QIcon:
    """Qt標準アイコンをダークテーマでも読める白色へ統一する。"""
    source = widget.style().standardIcon(standard_pixmap)
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = source.pixmap(size, size)
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(Qt.GlobalColor.white))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


@lru_cache(maxsize=3)
def _svg_icon(body: str) -> QIcon:
    """自作SVGを複数DPIのpixmapへ描画し、配布assetなしでQIcon化する。"""
    source = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"
        fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"
        stroke-linejoin="round">{body}</svg>"""
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    if not renderer.isValid():
        raise RuntimeError("操作アイコンのSVGが不正です")
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _repeat_icon(mode: RepeatMode) -> QIcon:
    return _svg_icon(_REPEAT_ONE_SVG if mode is RepeatMode.ONE else _REPEAT_SVG)


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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._controller = controller
        # ユーザーがシークバーをドラッグしている間は、Backend からの位置通知で
        # つまみを動かさない（操作が奪われるため）。
        self._is_seeking = False

        self._repeat_button = QPushButton()
        self._repeat_button.setObjectName("repeatModeButton")
        self._repeat_button.setAccessibleName("リピート")
        self._repeat_button.setToolTip(_REPEAT_TOOLTIPS[RepeatMode.OFF])
        self._repeat_button.setCheckable(True)
        self._repeat_button.setIcon(_repeat_icon(RepeatMode.OFF))
        self._shuffle_button = QPushButton()
        self._shuffle_button.setObjectName("shuffleButton")
        self._shuffle_button.setAccessibleName("シャッフル")
        self._shuffle_button.setCheckable(True)
        self._shuffle_button.setToolTip("シャッフル切替（S）")
        self._shuffle_button.setIcon(_svg_icon(_SHUFFLE_SVG))

        self._previous_button = QPushButton()
        self._previous_button.setObjectName("previousTrackButton")
        self._previous_button.setAccessibleName("前の曲")
        self._previous_button.setToolTip("前の曲（Page Up）")
        self._previous_button.setIcon(
            _white_standard_icon(self, QStyle.StandardPixmap.SP_MediaSkipBackward)
        )
        self._previous_button.setEnabled(False)
        self._next_button = QPushButton()
        self._next_button.setObjectName("nextTrackButton")
        self._next_button.setAccessibleName("次の曲")
        self._next_button.setToolTip("次の曲（Page Down）")
        self._next_button.setIcon(
            _white_standard_icon(self, QStyle.StandardPixmap.SP_MediaSkipForward)
        )
        self._next_button.setEnabled(False)

        self._play_button = QPushButton()
        self._play_button.setObjectName("playButton")
        self._play_button.setAccessibleName("再生")
        self._play_button.setToolTip("再生（Space）")
        self._play_button.setIcon(_white_standard_icon(self, QStyle.StandardPixmap.SP_MediaPlay))
        self._stop_button = QPushButton()
        self._stop_button.setObjectName("stopButton")
        self._stop_button.setAccessibleName("停止")
        self._stop_button.setToolTip("停止")
        self._stop_button.setIcon(_white_standard_icon(self, QStyle.StandardPixmap.SP_MediaStop))

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
        self._mute_button = QPushButton()
        self._mute_button.setObjectName("muteButton")
        self._mute_button.setAccessibleName("ミュート")
        self._mute_button.setCheckable(True)
        self._mute_button.setToolTip("ミュート切替（M）")

        self._state_label = QLabel(_STATE_LABELS[PlaybackState.NO_MEDIA])
        self._state_label.setObjectName("stateLabel")

        mode_button_width = self._play_button.sizeHint().width()
        self._repeat_button.setFixedWidth(mode_button_width)
        self._shuffle_button.setFixedWidth(mode_button_width)

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

        controls_row = QHBoxLayout()
        controls_row.addWidget(self._previous_button)
        controls_row.addWidget(self._play_button)
        controls_row.addWidget(self._stop_button)
        controls_row.addWidget(self._next_button)
        controls_row.addWidget(self._repeat_button)
        controls_row.addWidget(self._shuffle_button)
        controls_row.addSpacing(8)
        controls_row.addWidget(QLabel("音量"))
        controls_row.addWidget(self._volume_slider, stretch=1)
        controls_row.addWidget(self._mute_button)
        controls_row.addSpacing(8)
        controls_row.addWidget(self._state_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 2, 9, 2)
        layout.setSpacing(2)
        layout.addLayout(seek_row)
        layout.addLayout(controls_row)

    def _connect_widgets(self) -> None:
        # 前後曲は再生実装へ触らず、要求としてだけ外へ出す（配線は MainWindow）。
        self._previous_button.clicked.connect(self.previous_requested)
        self._next_button.clicked.connect(self.next_requested)
        self._repeat_button.clicked.connect(self.repeat_mode_requested)
        self._shuffle_button.toggled.connect(self.shuffle_toggled)
        self._play_button.clicked.connect(self._on_play_clicked)
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

        アイコンだけでなくツールチップとアクセシビリティ文でも区別する。
        未知の値は曖昧な表示へ丸めず ``KeyError``。
        """
        tooltip = _REPEAT_TOOLTIPS[mode]
        icon = _repeat_icon(mode)
        with QSignalBlocker(self._repeat_button):
            self._repeat_button.setChecked(mode is not RepeatMode.OFF)
        self._repeat_button.setIcon(icon)
        self._repeat_button.setToolTip(tooltip)
        self._repeat_button.setAccessibleDescription(tooltip)

    def set_shuffle_enabled(self, enabled: bool) -> None:
        """シャッフルボタンの状態を同期する。

        UI 更新が Controller への再設定を呼び戻さないよう、シグナルを止めて反映する。
        """
        with QSignalBlocker(self._shuffle_button):
            self._shuffle_button.setChecked(enabled)

    # -- ウィジェット操作 ---------------------------------------------------

    def _on_play_clicked(self) -> None:
        if self._controller.state is PlaybackState.PLAYING:
            self._controller.pause()
        else:
            self._controller.play()

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
        self._play_button.setEnabled(state is not PlaybackState.NO_MEDIA)
        if state is PlaybackState.PLAYING:
            self._play_button.setIcon(
                _white_standard_icon(self, QStyle.StandardPixmap.SP_MediaPause)
            )
            self._play_button.setAccessibleName("一時停止")
            self._play_button.setToolTip("一時停止（Space）")
        else:
            self._play_button.setIcon(
                _white_standard_icon(self, QStyle.StandardPixmap.SP_MediaPlay)
            )
            self._play_button.setAccessibleName("再生")
            self._play_button.setToolTip("再生（Space）")
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
        icon = (
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        self._mute_button.setIcon(_white_standard_icon(self, icon))

    def _on_source_changed(self, source: object) -> None:
        del source  # 表示するファイル名は MainWindow の責務
        self._is_seeking = False
        self._on_duration_changed(0)

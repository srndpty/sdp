"""PlayerControls の表示・操作契約を FakeBackend + PlaybackController で検証する。

子ウィジェットは objectName で取得する。private フィールドは覗かない。
"""

import gc
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QPushButton, QSlider, QWidget
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import PlaybackState
from sdp.ui.player_controls import PlayerControls, format_duration_ms


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend()


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def controls(controller: PlaybackController, qtbot: QtBot) -> Iterator[PlayerControls]:
    widget = PlayerControls(controller)
    qtbot.addWidget(widget)
    yield widget


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "テスト 音源.wav"
    path.write_bytes(b"RIFF----WAVEfmt ")
    return path


def button(controls: PlayerControls, name: str) -> QPushButton:
    widget = controls.findChild(QPushButton, name)
    assert widget is not None, name
    return widget


def slider(controls: PlayerControls, name: str) -> QSlider:
    widget = controls.findChild(QSlider, name)
    assert widget is not None, name
    return widget


def label_text(controls: PlayerControls, name: str) -> str:
    widget = controls.findChild(QLabel, name)
    assert widget is not None, name
    return widget.text()


# -- 時間整形 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "0:00"),
        (999, "0:00"),
        (1_000, "0:01"),
        (5_000, "0:05"),
        (59_999, "0:59"),
        (60_000, "1:00"),
        (65_000, "1:05"),
        (3_599_000, "59:59"),
        (3_600_000, "1:00:00"),
        (3_665_000, "1:01:05"),
        (-1, "0:00"),
        (-60_000, "0:00"),
    ],
)
def test_format_duration_ms(milliseconds: int, expected: str) -> None:
    """ミリ秒が m:ss / h:mm:ss へ整形され、負値は 0 として表示される。"""
    assert format_duration_ms(milliseconds) == expected


# -- 状態ごとの表示 ---------------------------------------------------------


def test_initial_state_disables_playback_widgets(controls: PlayerControls) -> None:
    """source 未設定では再生系操作とシークが無効で、初期表示が 0:00。"""
    assert not button(controls, "playButton").isEnabled()
    assert not button(controls, "stopButton").isEnabled()
    assert not slider(controls, "seekSlider").isEnabled()
    assert label_text(controls, "positionLabel") == "0:00"
    assert label_text(controls, "durationLabel") == "0:00"
    assert label_text(controls, "stateLabel") == "ファイルが選択されていません"
    # 音量とミュートは source が無くても操作できる。
    assert slider(controls, "volumeSlider").isEnabled()
    assert button(controls, "muteButton").isEnabled()


def test_stopped_state_enables_play(controls: PlayerControls, backend: FakePlaybackBackend) -> None:
    """STOPPED では再生／一時停止トグルが再生操作として有効。"""
    backend.emit_state(PlaybackState.STOPPED)

    assert button(controls, "playButton").isEnabled()
    assert button(controls, "playButton").accessibleName() == "再生"
    assert label_text(controls, "stateLabel") == "停止"


def test_playing_state_enables_pause_and_stop(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """PLAYING ではトグルが一時停止操作になり、停止も有効。"""
    backend.emit_state(PlaybackState.PLAYING)

    assert button(controls, "playButton").isEnabled()
    assert button(controls, "playButton").accessibleName() == "一時停止"
    assert button(controls, "stopButton").isEnabled()
    assert label_text(controls, "stateLabel") == "再生中"


def test_paused_state_enables_play_and_stop(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """PAUSED ではトグルが再生操作になり、停止も有効。"""
    backend.emit_state(PlaybackState.PAUSED)

    assert button(controls, "playButton").isEnabled()
    assert button(controls, "playButton").accessibleName() == "再生"
    assert button(controls, "stopButton").isEnabled()
    assert label_text(controls, "stateLabel") == "一時停止"


# -- ボタン操作 -------------------------------------------------------------


def test_transport_buttons_call_the_controller(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """同じボタンの再生・一時停止トグルと停止がController経由で届く。"""
    backend.emit_state(PlaybackState.STOPPED)
    button(controls, "playButton").click()
    backend.emit_state(PlaybackState.PLAYING)
    button(controls, "playButton").click()
    button(controls, "stopButton").click()

    assert backend.call_names() == ["play", "pause", "stop"]


def test_transport_and_mute_controls_use_white_icons(controls: PlayerControls) -> None:
    """主要な再生操作はダークテーマでも読める白いアイコンで表示する。"""
    for name in (
        "previousTrackButton",
        "playButton",
        "stopButton",
        "nextTrackButton",
        "muteButton",
    ):
        control = button(controls, name)
        assert control.text() == ""
        assert not control.icon().isNull()
        image = control.icon().pixmap(20, 20).toImage()
        opaque_colors = [
            image.pixelColor(x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        ]
        assert opaque_colors
        assert all(color.red() == color.green() == color.blue() == 255 for color in opaque_colors)


def test_buttons_and_volume_share_one_row(controls: PlayerControls, qtbot: QtBot) -> None:
    """再生・モード・音量の操作を同じ横一列へ配置する。"""
    controls.show()
    qtbot.waitExposed(controls)
    centers: list[int] = []
    for name in (
        "previousTrackButton",
        "playButton",
        "stopButton",
        "nextTrackButton",
        "repeatModeButton",
        "shuffleButton",
        "volumeSlider",
        "muteButton",
    ):
        widget = controls.findChild(QWidget, name)
        assert widget is not None
        centers.append(widget.geometry().center().y())
    assert max(centers) - min(centers) <= 1
    play_width = button(controls, "playButton").width()
    assert button(controls, "repeatModeButton").width() == play_width
    assert button(controls, "shuffleButton").width() == play_width


# -- シークバー -------------------------------------------------------------


def test_duration_change_updates_seek_range(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """duration_changed でシーク範囲と総時間表示が更新され、シークが有効になる。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(65_000)

    assert slider(controls, "seekSlider").maximum() == 65_000
    assert slider(controls, "seekSlider").isEnabled()
    assert label_text(controls, "durationLabel") == "1:05"


def test_zero_duration_disables_seek(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """duration が 0 ならシークは無効で、位置は 0 へ戻る。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(60_000)
    backend.emit_position(30_000)

    backend.emit_duration(0)

    assert not slider(controls, "seekSlider").isEnabled()
    assert slider(controls, "seekSlider").value() == 0
    assert label_text(controls, "positionLabel") == "0:00"


def test_shorter_duration_keeps_position_in_range(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """duration が縮んでもシーク位置が範囲外にならない。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(60_000)
    backend.emit_position(50_000)

    backend.emit_duration(10_000)

    assert slider(controls, "seekSlider").value() <= 10_000


def test_position_change_updates_slider_and_label(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """position_changed でつまみと現在時間表示が更新される。"""
    backend.emit_duration(120_000)

    backend.emit_position(65_000)

    assert slider(controls, "seekSlider").value() == 65_000
    assert label_text(controls, "positionLabel") == "1:05"


def test_position_change_is_ignored_while_dragging(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """ドラッグ中は Backend からの位置通知でつまみを戻さない。"""
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.setValue(90_000)
    seek_slider.sliderPressed.emit()

    backend.emit_position(1_000)

    assert seek_slider.value() == 90_000


def test_slider_release_seeks_once(controls: PlayerControls, backend: FakePlaybackBackend) -> None:
    """sliderReleased で Controller.seek が 1 回だけ呼ばれる。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.sliderPressed.emit()
    seek_slider.setValue(30_000)
    backend.calls.clear()

    seek_slider.sliderReleased.emit()
    seek_slider.sliderReleased.emit()

    assert backend.call_args("seek") == [(30_000,)]


def test_slider_release_without_press_does_not_seek(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """対応するsliderPressedがないreleaseは古い操作として無視する。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.setValue(30_000)
    backend.calls.clear()

    seek_slider.sliderReleased.emit()

    assert backend.call_args("seek") == []


def test_source_change_during_drag_cancels_seek(
    controls: PlayerControls,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """ドラッグ中にsourceが変わった場合、古いreleaseを新しい音源へ適用しない。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.sliderPressed.emit()
    seek_slider.setValue(30_000)

    controller.load(audio_file)
    backend.calls.clear()
    seek_slider.sliderReleased.emit()

    assert backend.call_args("seek") == []


def test_zero_duration_during_drag_cancels_seek(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """ドラッグ中にdurationが0になった場合、releaseでseekしない。"""
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.sliderPressed.emit()
    seek_slider.setValue(30_000)

    backend.emit_duration(0)
    backend.calls.clear()
    seek_slider.sliderReleased.emit()

    assert backend.call_args("seek") == []


def test_dragging_does_not_seek_before_release(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """ドラッグ中は seek せず、現在時間ラベルだけ追従する。"""
    backend.emit_duration(120_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.sliderPressed.emit()
    backend.calls.clear()

    seek_slider.sliderMoved.emit(65_000)

    assert backend.call_args("seek") == []
    assert label_text(controls, "positionLabel") == "1:05"


def test_seek_beyond_duration_is_not_swallowed(
    controls: PlayerControls, backend: FakePlaybackBackend, qtbot: QtBot
) -> None:
    """Controller の ValueError を UI が握り潰さない（プログラミングエラーの検出）。

    Qt のシグナル経由で呼ばれるスロットの例外は呼び出し元へ伝播しないため、
    pytest-qt の例外捕捉で検出する。
    """
    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_duration(10_000)
    seek_slider = slider(controls, "seekSlider")
    seek_slider.setMaximum(20_000)
    seek_slider.setValue(20_000)
    seek_slider.sliderPressed.emit()

    with qtbot.capture_exceptions() as exceptions:
        seek_slider.sliderReleased.emit()

    assert [captured[0] for captured in exceptions] == [ValueError]


# -- 音量とミュート ---------------------------------------------------------


@pytest.mark.parametrize(("slider_value", "expected_volume"), [(0, 0.0), (50, 0.5), (100, 1.0)])
def test_volume_slider_converts_to_controller_range(
    controls: PlayerControls,
    backend: FakePlaybackBackend,
    slider_value: int,
    expected_volume: float,
) -> None:
    """0〜100 のスライダー値が 0.0〜1.0 へ変換されて Controller へ届く。"""
    volume_slider = slider(controls, "volumeSlider")
    # どの目標値とも異なる位置から動かす（同値の再設定は no-op のため）。
    volume_slider.setValue(7)
    backend.calls.clear()

    volume_slider.setValue(slider_value)

    assert backend.call_args("set_volume") == [(expected_volume,)]


def test_volume_change_from_controller_updates_slider(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """Controller 側の音量変更で UI が同期する。"""
    backend.volume_changed.emit(0.25)

    assert slider(controls, "volumeSlider").value() == 25


def test_volume_sync_does_not_loop_back(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """Controller 由来の UI 更新が Controller への再設定を呼び戻さない。"""
    backend.volume_changed.emit(0.25)
    backend.calls.clear()

    backend.volume_changed.emit(0.75)

    assert backend.call_names() == []
    assert slider(controls, "volumeSlider").value() == 75


def test_mute_button_reaches_the_controller(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """ミュート操作が Controller へ届く。"""
    button(controls, "muteButton").click()

    assert backend.call_args("set_muted") == [(True,)]
    assert button(controls, "muteButton").isChecked()


def test_mute_change_from_controller_updates_button(
    controls: PlayerControls, backend: FakePlaybackBackend
) -> None:
    """Controller 側のミュート変更で UI が同期し、呼び戻さない。"""
    backend.muted_changed.emit(True)
    backend.calls.clear()

    assert button(controls, "muteButton").isChecked()
    assert backend.call_names() == []


def test_initial_volume_and_muted_come_from_the_controller(
    qtbot: QtBot,
) -> None:
    """初期表示を Controller の公開プロパティから作る。"""
    backend = FakePlaybackBackend(volume=0.4, muted=True)
    controller = PlaybackController(backend)
    widget = PlayerControls(controller)
    qtbot.addWidget(widget)

    assert slider(widget, "volumeSlider").value() == 40
    assert button(widget, "muteButton").isChecked()
    assert backend.call_names() == []


# -- source 変更と寿命 -------------------------------------------------------


def test_new_source_resets_position_display(
    controls: PlayerControls,
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    audio_file: Path,
) -> None:
    """source が変わったら位置と総時間の表示を 0 へ戻す。"""
    backend.emit_duration(60_000)
    backend.emit_position(30_000)

    controller.load(audio_file)

    assert slider(controls, "seekSlider").value() == 0
    assert label_text(controls, "positionLabel") == "0:00"
    assert label_text(controls, "durationLabel") == "0:00"


def test_controls_are_released_after_deletion(qtbot: QtBot) -> None:
    """PlayerControls を破棄したあと参照が残らない。"""
    del qtbot
    backend = FakePlaybackBackend()
    controller = PlaybackController(backend)
    widget = PlayerControls(controller)
    reference = weakref.ref(widget)

    del widget
    gc.collect()

    assert reference() is None
    # 破棄後の通知でクラッシュしないこと。
    spy = QSignalSpy(controller.position_changed)
    backend.emit_position(100)
    assert spy.count() == 1

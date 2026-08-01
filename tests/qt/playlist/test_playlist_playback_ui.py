"""プレイリスト再生に関する UI の契約を検証する。

PlaylistView は再生を知らず、ユーザーの意図をシグナルで外へ出すだけ。
現在再生中の行は View（delegate）が保持して描画する。
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QStyleOptionViewItem, QTableView
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playlist.model import Column, PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.services.pcm_tap import PcmTap
from sdp.services.settings import AppSettingsController
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui import playlist_view as playlist_view_module
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls
from sdp.ui.playlist_view import PlaylistEntryDelegate, PlaylistView


@pytest.fixture
def model(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def view(model: PlaylistModel, qtbot: QtBot) -> Iterator[PlaylistView]:
    widget = PlaylistView(model)
    qtbot.addWidget(widget)
    yield widget


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def table_of(view: PlaylistView) -> QTableView:
    table = view.findChild(QTableView, "playlistTable")
    assert table is not None
    return table


def delegate_of(view: PlaylistView) -> PlaylistEntryDelegate:
    delegate = table_of(view).itemDelegate()
    assert isinstance(delegate, PlaylistEntryDelegate)
    return delegate


@dataclass(frozen=True, slots=True)
class RowAppearance:
    """delegate が決めた 1 行の見た目。

    QStyleOptionViewItem の font / palette は一時オブジェクトで、
    option を返した後に参照すると解放済みになるため、ここで値を取り出す。
    """

    bold: bool
    text: QColor
    highlighted_text: QColor
    disabled_text: QColor


def row_appearance(view: PlaylistView, row: int) -> RowAppearance:
    option = QStyleOptionViewItem()
    model = table_of(view).model()
    delegate_of(view).initStyleOption(option, model.index(row, Column.NAME))
    return RowAppearance(
        bold=option.font.bold(),
        text=QColor(option.palette.color(QPalette.ColorRole.Text)),
        highlighted_text=QColor(option.palette.color(QPalette.ColorRole.HighlightedText)),
        disabled_text=QColor(
            option.palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        ),
    )


# -- 行の実行 ---------------------------------------------------------------


def test_activated_emits_the_entry_id(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """Enter / ダブルクリックによる activated で entry_id を通知する。"""
    del qtbot
    entry_ids = model.add_paths(audio_files)
    table = table_of(view)
    activations: list[str] = []
    view.entry_activated.connect(activations.append)

    table.activated.emit(model.index(1, Column.NAME))

    assert activations == [entry_ids[1]]


def test_double_click_emits_once(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """実際のダブルクリックで 1 回だけ通知する。

    Qt は doubleClicked と activated の両方を出す。両方を繋ぐと 1 回の操作で
    2 度通知されてしまうため、View は activated だけを使っている。
    """
    model.add_paths(audio_files)
    view.resize(400, 200)
    view.show()
    qtbot.waitExposed(view)
    table = table_of(view)
    activations: list[str] = []
    double_clicks: list[int] = []

    def record_double_click(index: QModelIndex) -> None:
        double_clicks.append(index.row())

    view.entry_activated.connect(activations.append)
    table.doubleClicked.connect(record_double_click)
    center = table.visualRect(model.index(0, Column.NAME)).center()

    # pytest-qt の入力ヘルパーは型情報が不完全なため QTest を直接使う。
    QTest.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center
    )
    QTest.mouseDClick(
        table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center
    )

    assert double_clicks == [0]
    assert activations == [model.entry_at(0).entry_id]


def test_enter_key_emits_the_entry_id(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """Enter でも通知する。"""
    model.add_paths(audio_files)
    view.show()
    qtbot.waitExposed(view)
    table = table_of(view)
    activations: list[str] = []
    view.entry_activated.connect(activations.append)
    table.setCurrentIndex(model.index(1, Column.NAME))

    QTest.keyClick(table, Qt.Key.Key_Return)

    assert activations == [model.entry_at(1).entry_id]


def test_invalid_index_does_not_emit(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """無効な index では通知しない。"""
    model.add_paths(audio_files)
    activations: list[str] = []
    view.entry_activated.connect(activations.append)

    table_of(view).activated.emit(QModelIndex())

    assert activations == []


def test_duplicate_paths_report_the_right_entry_id(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じパスの行でも、実行した行の entry_id を通知する。"""
    entry_ids = model.add_paths([audio_files[0], audio_files[0]])
    activations: list[str] = []
    view.entry_activated.connect(activations.append)
    table = table_of(view)

    table.activated.emit(model.index(1, Column.NAME))

    assert activations == [entry_ids[1]]


def test_missing_entry_is_still_activated(
    view: PlaylistView, model: PlaylistModel, tmp_path: Path
) -> None:
    """欠損行でも通知はする（再生可否の判断は View の責務ではない）。"""
    entry_ids = model.add_paths([tmp_path / "ない曲.wav"])
    activations: list[str] = []
    view.entry_activated.connect(activations.append)

    table_of(view).activated.emit(model.index(0, Column.NAME))

    assert activations == [entry_ids[0]]


# -- 現在曲の強調 -----------------------------------------------------------


def test_current_entry_is_emphasised(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """現在 entry の行だけ太字になる。"""
    entry_ids = model.add_paths(audio_files)

    view.set_current_entry_id(entry_ids[1])

    assert not row_appearance(view, 0).bold
    assert row_appearance(view, 1).bold
    assert not row_appearance(view, 2).bold


def test_changing_the_current_entry_moves_the_emphasis(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """現在 entry が変わると旧行の強調が消える。"""
    entry_ids = model.add_paths(audio_files)
    view.set_current_entry_id(entry_ids[0])

    view.set_current_entry_id(entry_ids[2])

    assert not row_appearance(view, 0).bold
    assert row_appearance(view, 2).bold


def test_clearing_the_current_entry_removes_all_emphasis(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """None を渡すと強調が消える。"""
    entry_ids = model.add_paths(audio_files)
    view.set_current_entry_id(entry_ids[0])

    view.set_current_entry_id(None)

    assert all(not row_appearance(view, row).bold for row in range(3))


def test_setting_the_same_current_entry_is_a_no_op(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じ entry_id を再設定しても表示は変わらない。"""
    entry_ids = model.add_paths(audio_files)
    view.set_current_entry_id(entry_ids[1])

    view.set_current_entry_id(entry_ids[1])

    assert delegate_of(view).current_entry_id == entry_ids[1]
    assert row_appearance(view, 1).bold


def test_emphasis_follows_the_entry_after_reordering(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path]
) -> None:
    """並べ替えても同じ entry_id の行が強調される。"""
    entry_ids = model.add_paths(audio_files)
    view.set_current_entry_id(entry_ids[0])

    assert model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 3) is True

    assert model.row_of_entry_id(entry_ids[0]) == 2
    assert row_appearance(view, 2).bold
    assert not row_appearance(view, 0).bold


def test_missing_row_keeps_its_grey_text(
    view: PlaylistView, model: PlaylistModel, audio_files: list[Path], tmp_path: Path
) -> None:
    """欠損行のグレー表示は従来どおり。"""
    model.add_paths([audio_files[0], tmp_path / "ない曲.wav"])
    model.refresh_file_status()

    missing = row_appearance(view, 1)
    available = row_appearance(view, 0)

    assert missing.text == missing.disabled_text
    assert available.text != available.disabled_text


def test_missing_and_current_row_combines_both(
    view: PlaylistView, model: PlaylistModel, tmp_path: Path
) -> None:
    """欠損かつ現在 entry でも、グレーと太字が両立して破綻しない。"""
    entry_ids = model.add_paths([tmp_path / "ない曲.wav"])
    model.refresh_file_status()
    view.set_current_entry_id(entry_ids[0])

    appearance = row_appearance(view, 0)

    assert appearance.bold
    assert appearance.text == appearance.disabled_text
    # 選択時も文字が読める色になっている。
    assert appearance.highlighted_text == appearance.disabled_text


def test_view_does_not_know_the_playlist_playback_controller() -> None:
    """PlaylistView は再生制御を import も保持もしない。"""
    assert not hasattr(playlist_view_module, "PlaylistPlaybackController")
    assert not hasattr(playlist_view_module, "PlaybackController")


# -- 前後曲ボタン -----------------------------------------------------------


@pytest.fixture
def controls(qtbot: QtBot) -> Iterator[PlayerControls]:
    backend = FakePlaybackBackend()
    widget = PlayerControls(PlaybackController(backend))
    qtbot.addWidget(widget)
    yield widget


def control_button(controls: PlayerControls, name: str) -> QPushButton:
    button = controls.findChild(QPushButton, name)
    assert button is not None, name
    return button


def test_navigation_buttons_exist_and_start_disabled(controls: PlayerControls) -> None:
    """前後曲ボタンがあり、初期状態は無効。"""
    assert not control_button(controls, "previousTrackButton").isEnabled()
    assert not control_button(controls, "nextTrackButton").isEnabled()


def test_navigation_buttons_emit_requests_once(controls: PlayerControls, qtbot: QtBot) -> None:
    """ボタン押下で要求シグナルが 1 回ずつ出る。"""
    del qtbot
    controls.set_playlist_navigation_available(True, True)
    previous_requests: list[int] = []
    next_requests: list[int] = []
    controls.previous_requested.connect(lambda: previous_requests.append(1))
    controls.next_requested.connect(lambda: next_requests.append(1))

    control_button(controls, "previousTrackButton").click()
    control_button(controls, "nextTrackButton").click()

    assert previous_requests == [1]
    assert next_requests == [1]


@pytest.mark.parametrize(
    ("previous", "next_"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_navigation_availability_is_applied(
    controls: PlayerControls, previous: bool, next_: bool
) -> None:
    """利用可否の設定がボタンへ反映される。"""
    controls.set_playlist_navigation_available(previous, next_)

    assert control_button(controls, "previousTrackButton").isEnabled() is previous
    assert control_button(controls, "nextTrackButton").isEnabled() is next_


def test_playback_state_changes_do_not_touch_navigation_buttons(
    controls: PlayerControls, qtbot: QtBot
) -> None:
    """再生状態の更新で前後曲ボタンの活性を勝手に変えない（フィードバックなし）。"""
    del qtbot
    controls.set_playlist_navigation_available(True, False)
    control_button(controls, "playButton").click()

    assert control_button(controls, "previousTrackButton").isEnabled() is True
    assert control_button(controls, "nextTrackButton").isEnabled() is False


# -- MainWindow の配線 ------------------------------------------------------


@pytest.fixture
def wired_window(qtbot: QtBot, tmp_path: Path) -> Iterator[tuple[MainWindow, PlaylistModel]]:
    backend = FakePlaybackBackend()
    playback = PlaybackController(backend)
    model = PlaylistModel()
    playlist_playback = PlaylistPlaybackController(playback, model)
    waveform_analysis = WaveformAnalysisService(playback, tmp_path / "waveform-cache")
    window = MainWindow(
        playback,
        model,
        playlist_playback,
        waveform_analysis,
        PcmTap(playback),
        AppSettingsController(playback),
    )
    qtbot.addWidget(window)
    yield window, model
    waveform_analysis.shutdown()


def test_activation_reaches_the_playlist_playback_controller(
    wired_window: tuple[MainWindow, PlaylistModel], audio_files: list[Path]
) -> None:
    """行の実行が play_entry まで届く。"""
    window, model = wired_window
    entry_ids = model.add_paths(audio_files)
    table = window.findChild(QTableView, "playlistTable")
    assert table is not None

    table.activated.emit(model.index(1, Column.NAME))

    view = window.findChild(PlaylistView)
    assert view is not None
    assert delegate_of(view).current_entry_id == entry_ids[1]


def test_navigation_buttons_reach_the_controller(
    wired_window: tuple[MainWindow, PlaylistModel], audio_files: list[Path]
) -> None:
    """前後曲ボタンが曲送りまで届き、活性も配線されている。"""
    window, model = wired_window
    entry_ids = model.add_paths(audio_files)
    controls = window.findChild(PlayerControls)
    view = window.findChild(PlaylistView)
    assert controls is not None
    assert view is not None

    assert control_button(controls, "nextTrackButton").isEnabled() is True
    control_button(controls, "nextTrackButton").click()
    assert delegate_of(view).current_entry_id == entry_ids[0]

    control_button(controls, "nextTrackButton").click()
    assert delegate_of(view).current_entry_id == entry_ids[1]

    control_button(controls, "previousTrackButton").click()
    assert delegate_of(view).current_entry_id == entry_ids[0]


def test_window_applies_navigation_snapshot_from_prepopulated_model(
    qtbot: QtBot, audio_files: list[Path], tmp_path: Path
) -> None:
    """接続前に確定した前後曲可否もMainWindow構築時に反映する。"""
    backend = FakePlaybackBackend()
    playback = PlaybackController(backend)
    model = PlaylistModel()
    entry_ids = model.add_paths(audio_files)
    playlist_playback = PlaylistPlaybackController(playback, model)

    waveform_analysis = WaveformAnalysisService(playback, tmp_path / "waveform-cache")
    window = MainWindow(
        playback,
        model,
        playlist_playback,
        waveform_analysis,
        PcmTap(playback),
        AppSettingsController(playback),
    )
    qtbot.addWidget(window)
    controls = window.findChild(PlayerControls)
    assert controls is not None

    assert playlist_playback.current_entry_id is None
    assert control_button(controls, "previousTrackButton").isEnabled()
    assert control_button(controls, "nextTrackButton").isEnabled()

    control_button(controls, "nextTrackButton").click()
    assert playlist_playback.current_entry_id == entry_ids[0]
    waveform_analysis.shutdown()


def test_playlist_playback_messages_reach_the_status_bar(
    wired_window: tuple[MainWindow, PlaylistModel], tmp_path: Path
) -> None:
    """再生できない場合のメッセージがステータスバーへ出る。"""
    window, model = wired_window
    entry_ids = model.add_paths([tmp_path / "ない曲.wav"])
    table = window.findChild(QTableView, "playlistTable")
    assert table is not None

    table.activated.emit(model.index(0, Column.NAME))

    assert window.statusBar().currentMessage() == playlist_playback_missing_message()
    assert model.row_of_entry_id(entry_ids[0]) == 0


def playlist_playback_missing_message() -> str:
    from sdp.core.playlist.playback_controller import MISSING_FILE_MESSAGE

    return MISSING_FILE_MESSAGE


def test_main_window_has_no_track_search_logic(
    wired_window: tuple[MainWindow, PlaylistModel],
) -> None:
    """MainWindow は次曲探索や欠損スキップを持たない。"""
    window, _ = wired_window
    for forbidden in ("play_next", "play_previous", "play_entry", "_find_playable_row"):
        assert not hasattr(window, forbidden), forbidden


# -- リピート・シャッフル UI ------------------------------------------------


def test_repeat_and_shuffle_buttons_exist_with_initial_state(
    controls: PlayerControls,
) -> None:
    """リピートとシャッフルはフォント非依存アイコンで始まる。"""
    repeat = control_button(controls, "repeatModeButton")
    shuffle = control_button(controls, "shuffleButton")
    assert repeat.text() == ""
    assert not repeat.icon().isNull()
    assert (repeat.iconSize().width(), repeat.iconSize().height()) == (18, 18)
    assert repeat.isCheckable()
    assert not repeat.isChecked()
    assert "オフ" in repeat.toolTip()
    assert shuffle.text() == ""
    assert not shuffle.icon().isNull()
    assert (shuffle.iconSize().width(), shuffle.iconSize().height()) == (18, 18)
    assert not shuffle.isChecked()
    assert shuffle.isCheckable()


def test_repeat_button_emits_a_request_once(controls: PlayerControls) -> None:
    """リピートボタンは要求を 1 回出すだけで、自分では表示を変えない。"""
    requests: list[int] = []
    controls.repeat_mode_requested.connect(lambda: requests.append(1))

    control_button(controls, "repeatModeButton").click()

    assert requests == [1]
    assert not control_button(controls, "repeatModeButton").icon().isNull()


def test_shuffle_button_emits_a_bool_once(controls: PlayerControls) -> None:
    """シャッフルボタンは ON/OFF を bool で 1 回ずつ出す。"""
    toggles: list[bool] = []
    controls.shuffle_toggled.connect(toggles.append)

    control_button(controls, "shuffleButton").click()
    control_button(controls, "shuffleButton").click()

    assert toggles == [True, False]


@pytest.mark.parametrize(
    ("mode", "description"),
    [
        (RepeatMode.OFF, "オフ"),
        (RepeatMode.ALL, "全曲"),
        (RepeatMode.ONE, "1曲"),
    ],
)
def test_repeat_mode_has_an_icon_and_text_alternative(
    controls: PlayerControls, mode: RepeatMode, description: str
) -> None:
    """モードはアイコンに加えてツールチップとアクセシビリティ文でも区別できる。"""
    controls.set_repeat_mode(mode)

    button = control_button(controls, "repeatModeButton")
    assert button.text() == ""
    assert not button.icon().isNull()
    assert description in button.toolTip()
    assert description in button.accessibleDescription()


def test_repeat_modes_use_icon_and_checked_state(controls: PlayerControls) -> None:
    """OFF／ALLは背景状態で、ONEは大きな1を循環矢印で囲んで区別する。"""
    button = control_button(controls, "repeatModeButton")
    controls.set_repeat_mode(RepeatMode.OFF)
    base_icon_key = button.icon().cacheKey()
    assert not button.isChecked()

    controls.set_repeat_mode(RepeatMode.ALL)
    assert button.icon().cacheKey() == base_icon_key
    assert button.isChecked()

    controls.set_repeat_mode(RepeatMode.ONE)
    assert button.icon().cacheKey() != base_icon_key
    assert button.isChecked()


def test_unknown_repeat_mode_is_not_rounded(controls: PlayerControls) -> None:
    """未知の値を曖昧な表示へ丸めない。"""
    with pytest.raises(KeyError):
        controls.set_repeat_mode("ALL")  # pyright: ignore[reportArgumentType]


def test_shuffle_state_from_the_controller_does_not_loop_back(
    controls: PlayerControls,
) -> None:
    """Controller 由来の更新で checked が同期し、要求シグナルを出し直さない。"""
    toggles: list[bool] = []
    controls.shuffle_toggled.connect(toggles.append)

    controls.set_shuffle_enabled(True)

    assert control_button(controls, "shuffleButton").isChecked()
    assert toggles == []


def test_playback_state_changes_do_not_break_repeat_and_shuffle(
    controls: PlayerControls,
) -> None:
    """再生状態の更新でリピート表示やシャッフル状態が壊れない。"""
    controls.set_repeat_mode(RepeatMode.ONE)
    controls.set_shuffle_enabled(True)

    control_button(controls, "playButton").click()

    assert not control_button(controls, "repeatModeButton").icon().isNull()
    assert control_button(controls, "shuffleButton").isChecked()


def test_repeat_and_shuffle_requests_reach_the_controller(
    wired_window: tuple[MainWindow, PlaylistModel], audio_files: list[Path]
) -> None:
    """ボタン操作が PlaylistPlaybackController まで届き、状態が UI へ返る。"""
    window, model = wired_window
    model.add_paths(audio_files)
    controls = window.findChild(PlayerControls)
    assert controls is not None
    repeat_button = control_button(controls, "repeatModeButton")
    shuffle_button = control_button(controls, "shuffleButton")

    repeat_button.click()
    assert repeat_button.isChecked()
    base_icon_key = repeat_button.icon().cacheKey()
    repeat_button.click()
    assert repeat_button.icon().cacheKey() != base_icon_key
    repeat_button.click()
    assert not repeat_button.isChecked()

    shuffle_button.click()
    assert shuffle_button.isChecked()
    shuffle_button.click()
    assert not shuffle_button.isChecked()


def test_initial_repeat_and_shuffle_state_is_applied_on_wiring(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """接続直後に現在の repeat / shuffle 状態が UI へ反映される。"""
    playback = PlaybackController(FakePlaybackBackend())
    model = PlaylistModel()
    playlist_playback = PlaylistPlaybackController(playback, model)
    playlist_playback.set_repeat_mode(RepeatMode.ALL)
    playlist_playback.set_shuffle_enabled(True)

    waveform_analysis = WaveformAnalysisService(playback, tmp_path / "waveform-cache")
    window = MainWindow(
        playback,
        model,
        playlist_playback,
        waveform_analysis,
        PcmTap(playback),
        AppSettingsController(playback),
    )
    qtbot.addWidget(window)

    controls = window.findChild(PlayerControls)
    assert controls is not None
    assert "全曲" in control_button(controls, "repeatModeButton").toolTip()
    assert control_button(controls, "shuffleButton").isChecked()
    waveform_analysis.shutdown()


def test_playlist_view_does_not_know_repeat_or_shuffle() -> None:
    """PlaylistView はリピートもシャッフルも知らない。"""
    for forbidden in ("RepeatMode", "PlaylistPlaybackController"):
        assert not hasattr(playlist_view_module, forbidden), forbidden


def test_player_controls_does_not_hold_the_playlist_controller(
    controls: PlayerControls,
) -> None:
    """PlayerControls は PlaylistPlaybackController を保持しない。"""
    for forbidden in ("playlist_playback", "play_next", "play_previous", "cycle_repeat_mode"):
        assert not hasattr(controls, forbidden), forbidden

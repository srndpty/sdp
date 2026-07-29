"""リピート再生（OFF / ALL / ONE）の契約を検証する。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus, PlaybackError, PlaybackErrorCode
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import (
    END_OF_PLAYLIST_MESSAGE,
    PlaylistPlaybackController,
)
from sdp.core.playlist.types import REPEAT_MODE_CYCLE, RepeatMode, next_repeat_mode


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend(duration_ms=2_000)


@pytest.fixture
def playback(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def playlist(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def controller(
    playback: PlaybackController, playlist: PlaylistModel
) -> Iterator[PlaylistPlaybackController]:
    yield PlaylistPlaybackController(playback, playlist)


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def loaded_paths(backend: FakePlaybackBackend) -> list[Path]:
    return [args[0] for args in backend.call_args("load") if isinstance(args[0], Path)]


def finish_track(backend: FakePlaybackBackend, qtbot: QtBot, *, times: int = 1) -> None:
    """曲末まで進んだことにして END_OF_MEDIA を通知する。"""
    backend.emit_position(backend.duration_ms)
    for _ in range(times):
        backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    qtbot.wait(1)


# -- RepeatMode の基本 ------------------------------------------------------


def test_initial_repeat_mode_is_off(controller: PlaylistPlaybackController) -> None:
    """初期状態は OFF。"""
    assert controller.repeat_mode is RepeatMode.OFF


def test_setting_the_same_mode_is_a_no_op(controller: PlaylistPlaybackController) -> None:
    """同じモードの再設定では通知しない。"""
    spy = QSignalSpy(controller.repeat_mode_changed)

    controller.set_repeat_mode(RepeatMode.OFF)

    assert spy.count() == 0


def test_repeat_mode_cycles_off_all_one(controller: PlaylistPlaybackController) -> None:
    """OFF → ALL → ONE → OFF の順に切り替わる。"""
    spy = QSignalSpy(controller.repeat_mode_changed)
    observed = [controller.repeat_mode]

    for _ in range(3):
        controller.cycle_repeat_mode()
        observed.append(controller.repeat_mode)

    assert observed == [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE, RepeatMode.OFF]
    assert spy.count() == 3


def test_repeat_mode_cycle_helper() -> None:
    """切替順のヘルパーが一巡する。"""
    assert next_repeat_mode(RepeatMode.OFF) is RepeatMode.ALL
    assert next_repeat_mode(RepeatMode.ALL) is RepeatMode.ONE
    assert next_repeat_mode(RepeatMode.ONE) is RepeatMode.OFF
    assert set(REPEAT_MODE_CYCLE) == set(RepeatMode)


@pytest.mark.parametrize("value", ["ALL", 1, None, object()])
def test_invalid_repeat_mode_is_rejected(
    controller: PlaylistPlaybackController, value: object
) -> None:
    """不正な値を silent fallback せず TypeError にする。"""
    with pytest.raises(TypeError):
        controller.set_repeat_mode(value)  # pyright: ignore[reportArgumentType]

    assert controller.repeat_mode is RepeatMode.OFF


def test_changing_mode_does_not_touch_playback(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """モード変更だけでは load も play もせず、現在 entry も変えない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[1])
    backend.calls.clear()

    controller.cycle_repeat_mode()
    controller.cycle_repeat_mode()

    assert backend.call_names() == []
    assert controller.current_entry_id == entry_ids[1]


# -- Repeat OFF -------------------------------------------------------------


def test_off_does_not_wrap(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """OFF では末尾 next も先頭 previous も False。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    assert controller.play_next() is False

    controller.play_entry(entry_ids[0])
    assert controller.play_previous() is False


# -- Repeat ALL -------------------------------------------------------------


def test_all_wraps_forward(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ALL では末尾の次が先頭になる。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_entry(entry_ids[-1])

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[0]


def test_all_wraps_backward(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ALL では先頭の前が末尾になる。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_entry(entry_ids[0])

    assert controller.play_previous() is True

    assert controller.current_entry_id == entry_ids[-1]


def test_all_auto_advances_from_the_last_to_the_first(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """ALL では最後の曲が終わると先頭へ進む。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_entry(entry_ids[-1])

    finish_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[0]


def test_all_skips_missing_entries_when_wrapping(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """折り返した先が欠損なら、その次の利用可能 entry へ進む。"""
    entry_ids = playlist.add_paths([tmp_path / "ない.wav", audio_files[0], audio_files[1]])
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_entry(entry_ids[2])

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[1]


def test_all_with_every_entry_missing_terminates(
    controller: PlaylistPlaybackController, playlist: PlaylistModel, tmp_path: Path
) -> None:
    """全部欠損なら ALL でも有限時間で False。"""
    playlist.add_paths([tmp_path / f"ない{index}.wav" for index in range(30)])
    controller.set_repeat_mode(RepeatMode.ALL)

    assert controller.play_next() is False
    assert controller.play_previous() is False


def test_all_with_a_single_entry_replays_it(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """ALL で 1 件だけなら同じ entry を再実行できる。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_entry(entry_ids[0])

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[0]
    assert loaded_paths(backend) == [audio_files[0].resolve()] * 2


# -- Repeat ONE -------------------------------------------------------------


def test_one_replays_the_same_entry_on_end_of_media(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """ONE では曲が終わると同じ entry を読み込み直す。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[1])
    backend.calls.clear()

    finish_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[1]
    assert loaded_paths(backend) == [audio_files[1].resolve()]
    assert backend.call_names() == ["load", "play"]


def test_one_does_not_block_manual_next_and_previous(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ONE でも手動の次 / 前は通常どおり移動する。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[1])

    assert controller.play_next() is True
    assert controller.current_entry_id == entry_ids[2]

    assert controller.play_previous() is True
    assert controller.current_entry_id == entry_ids[1]


def test_one_does_not_apply_to_a_directly_opened_track(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """「開く...」で直接開いた単曲は ONE でも繰り返さない。"""
    playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    playback.load(audio_files[0])
    backend.calls.clear()

    finish_track(backend, qtbot)

    assert controller.current_entry_id is None
    assert backend.call_names() == []


def test_one_consumes_each_end_of_media_generation_once(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """ONE でも同じ source 世代の重複通知では 1 回しか読み込み直さない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])
    backend.calls.clear()

    finish_track(backend, qtbot, times=3)

    assert backend.call_names() == ["load", "play"]


def test_stale_end_of_media_after_one_replay_does_not_advance(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """ONE の再実行後に古い世代の通知が届いても、さらに進まない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])
    finish_track(backend, qtbot)
    backend.calls.clear()

    # 新しい source 世代はまだ再生が始まっていない（position が 0 のまま）。
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    qtbot.wait(1)

    assert backend.call_names() == []
    assert controller.current_entry_id == entry_ids[0]


def test_one_does_nothing_after_the_current_entry_is_removed(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """現在 entry を削除した後の終了通知では何もしない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])
    playlist.removeRows(0, 1)
    backend.calls.clear()

    finish_track(backend, qtbot)

    assert controller.current_entry_id is None
    assert backend.call_names() == []


def test_playback_error_does_not_trigger_repeat(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """再生エラーではリピート処理も自動スキップもしない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])
    backend.calls.clear()

    backend.emit_error(
        PlaybackError(
            code=PlaybackErrorCode.FORMAT_ERROR,
            message="この音声形式は再生できません。",
            detail="test",
        )
    )

    assert backend.call_names() == []
    assert controller.current_entry_id == entry_ids[0]


def test_end_of_playlist_message_is_emitted_once(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """最終曲で重複通知が来ても、終了メッセージは 1 回だけ。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    spy = QSignalSpy(controller.message_requested)

    finish_track(backend, qtbot, times=3)

    assert spy.count() == 1
    assert spy.at(0)[0] == END_OF_PLAYLIST_MESSAGE


# -- navigation availability ------------------------------------------------


def test_navigation_wraps_with_repeat_all(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ALL では端でも前後どちらも可能になる。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    assert controller.can_play_next is False

    controller.set_repeat_mode(RepeatMode.ALL)

    assert controller.can_play_next is True
    assert controller.can_play_previous is True


def test_repeat_one_does_not_change_manual_navigation(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ONE は手動ナビゲーションの可否を変えない（OFF と同じ）。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    before = (controller.can_play_previous, controller.can_play_next)

    controller.set_repeat_mode(RepeatMode.ONE)

    assert (controller.can_play_previous, controller.can_play_next) == before


def test_navigation_signal_is_not_repeated_for_the_same_value(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """可否が変わらないモード変更では通知しない。"""
    playlist.add_paths(audio_files)
    spy = QSignalSpy(controller.navigation_availability_changed)

    controller.set_repeat_mode(RepeatMode.ONE)

    assert spy.count() == 0

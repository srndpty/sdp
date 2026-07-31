"""PlaylistPlaybackController の契約を検証する。

FakePlaybackBackend + 実 PlaybackController + 実 PlaylistModel を使い、
Backend の呼び出し記録と公開プロパティ・シグナルで確認する。
"""

import gc
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.metadata.types import TrackMetadata
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus, PlaybackError, PlaybackErrorCode
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import (
    END_OF_PLAYLIST_MESSAGE,
    MISSING_FILE_MESSAGE,
    PlaylistPlaybackController,
)
from sdp.core.playlist.types import RepeatMode

WAIT_TIMEOUT_MS = 2_000


def process_deferred_events(qtbot: QtBot) -> None:
    """遅延させた自動次曲（QTimer.singleShot(0)）を処理させる。

    スレッドを止める sleep ではなく、Qt のイベントループを回す。
    """
    qtbot.wait(1)


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
    for index in range(4):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def loaded_paths(backend: FakePlaybackBackend) -> list[Path]:
    return [args[0] for args in backend.call_args("load") if isinstance(args[0], Path)]


def finish_current_track(
    backend: FakePlaybackBackend, qtbot: QtBot, *, at_end: bool = True
) -> None:
    """曲末まで再生されたことにして END_OF_MEDIA を通知する。"""
    if at_end:
        backend.emit_position(backend.duration_ms)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)


# -- 初期状態 ---------------------------------------------------------------


def test_initial_current_entry_is_none(controller: PlaylistPlaybackController) -> None:
    """最初は現在 entry を持たない。"""
    assert controller.current_entry_id is None
    assert controller.can_play_previous is False
    assert controller.can_play_next is False


def test_playback_controller_does_not_know_the_playlist(
    playback: PlaybackController,
) -> None:
    """PlaybackController はプレイリストを保持しない。"""
    for forbidden in ("playlist", "playlist_model", "current_entry_id", "play_next"):
        assert not hasattr(playback, forbidden), forbidden


def test_playlist_model_does_not_know_the_current_entry(playlist: PlaylistModel) -> None:
    """PlaylistModel は現在再生中の entry を持たない。"""
    for forbidden in ("current_entry_id", "set_current_entry_id", "current_row"):
        assert not hasattr(playlist, forbidden), forbidden


# -- play_entry -------------------------------------------------------------


def test_play_entry_loads_and_plays(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """存在する entry を再生すると load → play され、現在 entry になる。"""
    entry_ids = playlist.add_paths(audio_files)
    spy = QSignalSpy(controller.current_entry_changed)

    assert controller.play_entry(entry_ids[1]) is True

    assert backend.call_names() == ["load", "play"]
    assert loaded_paths(backend) == [audio_files[1].resolve()]
    assert controller.current_entry_id == entry_ids[1]
    assert spy.count() == 1
    assert spy.at(0)[0] == entry_ids[1]


def test_play_entry_with_unknown_id_does_nothing(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """未知の entry_id では何もしない。"""
    playlist.add_paths(audio_files)

    assert controller.play_entry("unknown") is False

    assert backend.call_names() == []
    assert controller.current_entry_id is None


def test_missing_entry_is_not_played_and_does_not_move_on(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """欠損 entry の直接再生は失敗し、勝手に別の曲へ移らない。"""
    entry_ids = playlist.add_paths([tmp_path / "ない曲.wav", *audio_files])
    spy = QSignalSpy(controller.message_requested)

    assert controller.play_entry(entry_ids[0]) is False

    assert backend.call_names() == []
    assert controller.current_entry_id is None
    assert spy.count() == 1
    assert spy.at(0)[0] == MISSING_FILE_MESSAGE


def test_play_entry_refreshes_file_status(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """再生直前に欠損が判明したら Model へ反映する（グレー表示へ波及）。"""
    entry_ids = playlist.add_paths(audio_files)
    assert not playlist.entry_at(0).is_missing
    audio_files[0].unlink()

    assert controller.play_entry(entry_ids[0]) is False

    assert playlist.entry_at(0).is_missing


def test_duplicate_paths_are_distinguished_by_entry_id(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """同じパスの行を entry_id で区別する。"""
    entry_ids = playlist.add_paths([audio_files[0], audio_files[0]])

    assert controller.play_entry(entry_ids[0]) is True
    assert controller.current_entry_id == entry_ids[0]

    assert controller.play_entry(entry_ids[1]) is True
    assert controller.current_entry_id == entry_ids[1]


def test_direct_single_track_load_clears_the_current_entry(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """「開く...」で同じパスを直接開いたら現在 entry を解除する。"""
    entry_ids = playlist.add_paths([audio_files[0]])
    controller.play_entry(entry_ids[0])
    spy = QSignalSpy(controller.current_entry_changed)

    playback.load(audio_files[0])

    assert controller.current_entry_id is None
    assert spy.count() == 1
    assert spy.at(0)[0] is None


def test_playback_error_does_not_skip_to_the_next_track(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """再生エラーで勝手に次曲へ飛ばさない（無限スキップとエラー隠蔽を避ける）。"""
    entry_ids = playlist.add_paths(audio_files)
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


# -- next / previous --------------------------------------------------------


def test_play_next_and_previous(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """次と前の曲へ移動する。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[1])

    assert controller.play_next() is True
    assert controller.current_entry_id == entry_ids[2]

    assert controller.play_previous() is True
    assert controller.current_entry_id == entry_ids[1]


def test_next_without_current_starts_from_the_first(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry が無ければ先頭から再生する。"""
    entry_ids = playlist.add_paths(audio_files)

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[0]


def test_previous_without_current_starts_from_the_last(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry が無ければ末尾から再生する。"""
    entry_ids = playlist.add_paths(audio_files)

    assert controller.play_previous() is True

    assert controller.current_entry_id == entry_ids[-1]


def test_next_skips_missing_entries(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """次の曲では欠損をスキップし、Model の状態も更新する。"""
    entry_ids = playlist.add_paths(
        [audio_files[0], tmp_path / "ない1.wav", tmp_path / "ない2.wav", audio_files[1]]
    )
    controller.play_entry(entry_ids[0])

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[3]
    assert playlist.entry_at(1).is_missing
    assert playlist.entry_at(2).is_missing


def test_previous_skips_missing_entries(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """前の曲でも欠損をスキップする。"""
    entry_ids = playlist.add_paths([audio_files[0], tmp_path / "ない.wav", audio_files[1]])
    controller.play_entry(entry_ids[2])

    assert controller.play_previous() is True

    assert controller.current_entry_id == entry_ids[0]


def test_next_detects_a_file_deleted_after_adding(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """追加後に消えたファイルも探索時に検出してスキップする。"""
    entry_ids = playlist.add_paths(audio_files[:3])
    controller.play_entry(entry_ids[0])
    audio_files[1].unlink()

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[2]
    assert playlist.entry_at(1).is_missing


def test_next_at_the_end_does_not_wrap_around(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """末尾で先頭へ折り返さない（Repeat ALL は P2-C2）。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])

    assert controller.play_next() is False

    assert controller.current_entry_id == entry_ids[-1]


def test_previous_at_the_beginning_does_not_wrap_around(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """先頭で末尾へ折り返さない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    assert controller.play_previous() is False

    assert controller.current_entry_id == entry_ids[0]


def test_search_terminates_when_every_entry_is_missing(
    controller: PlaylistPlaybackController, playlist: PlaylistModel, tmp_path: Path
) -> None:
    """全項目が欠損でも探索が有限で終わる。"""
    playlist.add_paths([tmp_path / f"ない{index}.wav" for index in range(50)])

    assert controller.play_next() is False
    assert controller.play_previous() is False
    assert controller.current_entry_id is None


# -- 自動次曲 ---------------------------------------------------------------


def test_end_of_media_advances_to_the_next_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """曲の終わりで次の曲へ進む。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    finish_current_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[1]
    assert loaded_paths(backend) == [audio_files[0].resolve(), audio_files[1].resolve()]


def test_end_of_media_of_a_directly_opened_track_does_not_start_the_playlist(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """「開く...」で直接開いた単曲の終了ではプレイリストへ移らない。"""
    playlist.add_paths(audio_files)
    playback.load(audio_files[3])
    backend.calls.clear()

    finish_current_track(backend, qtbot)

    assert controller.current_entry_id is None
    assert backend.call_names() == []


def test_duplicate_end_of_media_does_not_advance_twice(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """同じ終了通知が重なっても 2 曲進まない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    backend.emit_position(backend.duration_ms)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[1]


def test_scheduled_end_of_media_after_manual_switch_is_ignored(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """遅延中に手動で切り替えたら、古い終了通知を適用しない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    backend.emit_position(backend.duration_ms)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    # 遅延処理が走る前に手動で別の曲へ切り替える。
    controller.play_entry(entry_ids[3])
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[3]


def test_end_of_media_is_not_rejected_by_position_heuristics(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """Backendの終了通知をposition比率だけで誤って捨てない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    backend.emit_position(10)
    finish_current_track(backend, qtbot, at_end=False)

    assert controller.current_entry_id == entry_ids[1]


def test_late_duplicate_end_is_ignored_until_the_new_source_starts(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """A終了後の遅延重複ENDはBを飛ばさず、B自身の終了ならCへ進む。"""
    entry_ids = playlist.add_paths(audio_files[:3])
    controller.play_entry(entry_ids[0])
    # 実backendはsetSource直後に同期でLOADEDを返さない。B読み込み後・B無通知の
    # 時点へA由来のENDが届く順序を再現する。
    backend.defer_load_status = True

    finish_current_track(backend, qtbot)
    assert controller.current_entry_id == entry_ids[1]

    # Bがまだ何も通知していない間に届くA由来の重複通知。
    backend.emit_position(0)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)
    assert controller.current_entry_id == entry_ids[1]

    # Bの読み込みが進んだ後の正規の終了通知は、Bの世代として1回だけ消費する。
    backend.emit_media_status(MediaStatus.LOADED)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)
    assert controller.current_entry_id == entry_ids[2]


def test_zero_length_source_still_advances_to_the_next_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """正のpositionを一度も通知しない音源でも次曲へ進む。

    0ms扱いの音源や数msの効果音では、positionが0のまま終了しうる。
    読み込みが進んだstatusを世代開始の根拠にすることで、これを取りこぼさない。
    """
    entry_ids = playlist.add_paths(audio_files[:2])
    controller.play_entry(entry_ids[0])

    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[1]


def test_zero_length_source_repeats_with_repeat_one(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """Repeat ONEでも、正のpositionを通知しない音源が再読み込みされる。"""
    entry_ids = playlist.add_paths(audio_files[:2])
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])
    load_count = len(backend.call_args("load"))

    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[0]
    assert len(backend.call_args("load")) == load_count + 1


def test_buffered_status_alone_marks_the_source_as_started(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """LOADEDを取りこぼしてもBUFFEREDだけで世代開始とみなせる。"""
    entry_ids = playlist.add_paths(audio_files[:2])
    backend.defer_load_status = True
    controller.play_entry(entry_ids[0])

    backend.emit_media_status(MediaStatus.BUFFERED)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[1]


def test_late_duplicate_end_after_auto_advance_does_not_skip_another_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """イベントループをまたいだ前sourceの重複ENDでも1曲だけ進む。"""
    entry_ids = playlist.add_paths(audio_files[:3])
    controller.play_entry(entry_ids[0])
    backend.defer_load_status = True

    finish_current_track(backend, qtbot)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert controller.current_entry_id == entry_ids[1]


def test_consecutive_duplicate_paths_advance_one_by_one(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """同じパスが連続していても 1 曲ずつしか進まない。"""
    entry_ids = playlist.add_paths([audio_files[0], audio_files[0], audio_files[0]])
    controller.play_entry(entry_ids[0])

    finish_current_track(backend, qtbot)
    assert controller.current_entry_id == entry_ids[1]

    finish_current_track(backend, qtbot)
    assert controller.current_entry_id == entry_ids[2]


def test_end_of_media_on_the_last_entry_keeps_the_current_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """最後の曲が終わっても current は最後の entry のまま、新しい load もしない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    backend.calls.clear()
    spy = QSignalSpy(controller.message_requested)

    finish_current_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[-1]
    assert backend.call_names() == []
    assert spy.count() == 1
    assert spy.at(0)[0] == END_OF_PLAYLIST_MESSAGE


def test_duplicate_end_on_last_entry_reports_the_end_once(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """末尾のENDをイベントループ後に再通知されてもメッセージは1回。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    controller.play_entry(entry_ids[0])
    spy = QSignalSpy(controller.message_requested)

    finish_current_track(backend, qtbot)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    process_deferred_events(qtbot)

    assert spy.count() == 1
    assert spy.at(0)[0] == END_OF_PLAYLIST_MESSAGE


# -- Model の変更 -----------------------------------------------------------


def test_current_entry_survives_reordering(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """並べ替えても現在 entry は entry_id で追跡される。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])

    assert playlist.moveRows(QModelIndex(), 0, 1, QModelIndex(), 4) is True

    assert controller.current_entry_id == entry_ids[0]
    assert playlist.row_of_entry_id(entry_ids[0]) == 3
    # 新しい行順で前後曲の可否が計算される。
    assert controller.can_play_next is False
    assert controller.can_play_previous is True


def test_removing_another_entry_keeps_the_current_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry 以外の削除では現在 entry を維持する。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[2])

    assert playlist.removeRows(0, 1) is True

    assert controller.current_entry_id == entry_ids[2]


def test_removing_the_current_entry_only_clears_the_association(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """現在 entry の削除では関連付けだけ解除し、再生は止めない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[1])
    backend.calls.clear()
    spy = QSignalSpy(controller.current_entry_changed)

    assert playlist.removeRows(1, 1) is True

    assert controller.current_entry_id is None
    assert spy.count() == 1
    assert spy.at(0)[0] is None
    assert "stop" not in backend.call_names()
    assert backend.call_names() == []


def test_end_of_media_after_removing_the_current_entry_does_not_advance(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """現在 entry を削除した後は自動次曲もしない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[1])
    playlist.removeRows(1, 1)
    backend.calls.clear()

    finish_current_track(backend, qtbot)

    assert controller.current_entry_id is None
    assert backend.call_names() == []


def test_clearing_the_playlist_clears_the_association_without_stopping(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """全消去でも関連付けだけ解除し、再生は継続する。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[0])
    backend.calls.clear()

    playlist.clear()

    assert controller.current_entry_id is None
    assert backend.call_names() == []


def test_navigation_availability_follows_model_changes(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """追加・削除・移動で前後曲の可否が更新される。"""
    spy = QSignalSpy(controller.navigation_availability_changed)
    assert (controller.can_play_previous, controller.can_play_next) == (False, False)

    entry_ids = playlist.add_paths(audio_files[:2])
    assert (controller.can_play_previous, controller.can_play_next) == (True, True)
    assert spy.count() == 1

    controller.play_entry(entry_ids[1])
    assert (controller.can_play_previous, controller.can_play_next) == (True, False)

    playlist.removeRows(0, 1)
    assert (controller.can_play_previous, controller.can_play_next) == (False, False)


def test_navigation_availability_ignores_missing_entries(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """欠損しか残っていなければ次の曲は不可。"""
    entry_ids = playlist.add_paths([audio_files[0], tmp_path / "ない.wav"])
    playlist.refresh_file_status()
    controller.play_entry(entry_ids[0])

    assert controller.can_play_next is False


# -- 寿命 -------------------------------------------------------------------


def test_controller_is_released_after_deletion(qtbot: QtBot, tmp_path: Path) -> None:
    """破棄したあと参照が残らず、以後の通知でもクラッシュしない。"""
    del qtbot
    backend = FakePlaybackBackend()
    playback = PlaybackController(backend)
    playlist = PlaylistModel()
    controller = PlaylistPlaybackController(playback, playlist)
    reference = weakref.ref(controller)

    del controller
    gc.collect()

    assert reference() is None
    path = tmp_path / "曲.wav"
    path.write_bytes(b"x")
    playlist.add_paths([path])
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    assert playlist.rowCount() == 1


def test_empty_playlist_navigation_returns_false(
    controller: PlaylistPlaybackController,
) -> None:
    """空のプレイリストでは前後どちらにも進めない。"""
    assert controller.play_next() is False
    assert controller.play_previous() is False


def test_file_vanishing_between_check_and_load_does_not_play(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存在確認の直後に消えた場合、load は失敗し play もしない。"""
    entry_ids = playlist.add_paths(audio_files)
    original_refresh = PlaylistModel.refresh_entry_status

    def refresh_then_delete(model: PlaylistModel, entry_id: str) -> bool:
        result = original_refresh(model, entry_id)
        audio_files[0].unlink(missing_ok=True)
        return result

    monkeypatch.setattr(PlaylistModel, "refresh_entry_status", refresh_then_delete)

    assert controller.play_entry(entry_ids[0]) is False

    assert "play" not in backend.call_names()
    assert controller.current_entry_id is None


@pytest.mark.parametrize("forward", [True, False])
def test_navigation_continues_after_candidate_vanishes_before_load(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    forward: bool,
) -> None:
    """候補が確認直後に消えても、同じ方向の次候補まで探索する。"""
    entry_ids = playlist.add_paths(audio_files[:3])
    current_index, vanishing_index, expected_index = (0, 1, 2) if forward else (2, 1, 0)
    controller.play_entry(entry_ids[current_index])
    original_refresh = PlaylistModel.refresh_entry_status
    deleted = False

    def refresh_then_delete(model: PlaylistModel, entry_id: str) -> bool:
        nonlocal deleted
        result = original_refresh(model, entry_id)
        if entry_id == entry_ids[vanishing_index] and not deleted:
            audio_files[vanishing_index].unlink()
            deleted = True
        return result

    monkeypatch.setattr(PlaylistModel, "refresh_entry_status", refresh_then_delete)

    result = controller.play_next() if forward else controller.play_previous()

    assert result is True
    assert controller.current_entry_id == entry_ids[expected_index]
    assert playlist.entry_at(vanishing_index).is_missing


def test_auto_advance_continues_after_candidate_vanishes_before_load(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """自動次曲でもTOCTOU欠損を飛ばして後続曲へ進む。"""
    entry_ids = playlist.add_paths(audio_files[:3])
    controller.play_entry(entry_ids[0])
    original_refresh = PlaylistModel.refresh_entry_status
    deleted = False

    def refresh_then_delete(model: PlaylistModel, entry_id: str) -> bool:
        nonlocal deleted
        result = original_refresh(model, entry_id)
        if entry_id == entry_ids[1] and not deleted:
            audio_files[1].unlink()
            deleted = True
        return result

    monkeypatch.setattr(PlaylistModel, "refresh_entry_status", refresh_then_delete)

    finish_current_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[2]
    assert playlist.entry_at(1).is_missing


# -- メタデータ更新との分離 --------------------------------------------------


def test_metadata_only_changes_do_not_touch_playback_state(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """メタデータだけの dataChanged では再生制御が何もしない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[1])
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    before = (
        controller.current_entry_id,
        controller.repeat_mode,
        controller.shuffle_enabled,
        controller.can_play_previous,
        controller.can_play_next,
    )
    backend.calls.clear()
    navigation_spy = QSignalSpy(controller.navigation_availability_changed)
    current_spy = QSignalSpy(controller.current_entry_changed)

    for entry_id in entry_ids:
        playlist.mark_metadata_loading(entry_id)
        playlist.apply_metadata(entry_id, TrackMetadata(title="曲", duration_ms=1234))
    playlist.mark_metadata_failed(entry_ids[0])

    assert (
        controller.current_entry_id,
        controller.repeat_mode,
        controller.shuffle_enabled,
        controller.can_play_previous,
        controller.can_play_next,
    ) == before
    assert navigation_spy.count() == 0
    assert current_spy.count() == 0
    assert backend.call_names() == []


def test_metadata_changes_do_not_disturb_the_shuffle_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """メタデータ更新でシャッフル履歴が乱れない。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    first = controller.current_entry_id
    controller.play_next()

    for entry in playlist.entries():
        playlist.apply_metadata(entry.entry_id, TrackMetadata(title="曲"))

    assert controller.play_previous() is True
    assert controller.current_entry_id == first


def test_file_status_changes_still_update_navigation(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """FILE_STATUS_ROLE の変化では従来どおり可否を更新する。"""
    entry_ids = playlist.add_paths(audio_files[:2])
    controller.play_entry(entry_ids[0])
    assert controller.can_play_next is True

    audio_files[1].unlink()
    playlist.refresh_entry_status(entry_ids[1])

    assert controller.can_play_next is False


def test_metadata_failure_does_not_change_playability(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """メタデータ読み取りに失敗しても再生できる。"""
    entry_ids = playlist.add_paths(audio_files)
    playlist.mark_metadata_failed(entry_ids[0])

    assert controller.play_entry(entry_ids[0]) is True
    assert controller.current_entry_id == entry_ids[0]


def test_entry_can_be_played_while_metadata_is_loading(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """メタデータ読み取り中でも再生できる。"""
    entry_ids = playlist.add_paths(audio_files)
    playlist.mark_metadata_loading(entry_ids[0])

    assert controller.play_entry(entry_ids[0]) is True

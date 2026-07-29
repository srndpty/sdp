"""シャッフル再生と再生履歴の契約を検証する。

乱数は seed 付きの ``random.Random`` を注入して決定的にする。
検証は「重複しない」「候補集合が正しい」「entry_id で識別される」といった
不変条件を中心にし、特定の Python 実装が返す並び自体には依存しない。
"""

import random
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus, PlaybackError, PlaybackErrorCode
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode

SEED = 20260729


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
    yield PlaylistPlaybackController(playback, playlist, rng=random.Random(SEED))


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(5):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def finish_track(backend: FakePlaybackBackend, qtbot: QtBot) -> None:
    backend.emit_position(backend.duration_ms)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    qtbot.wait(1)


def play_until_exhausted(controller: PlaylistPlaybackController, limit: int) -> list[str | None]:
    """next が False になるまで進み、通った entry_id を返す。"""
    played: list[str | None] = []
    for _ in range(limit):
        if not controller.play_next():
            break
        played.append(controller.current_entry_id)
    return played


# -- 基本 -------------------------------------------------------------------


def test_initial_shuffle_is_disabled(controller: PlaylistPlaybackController) -> None:
    assert controller.shuffle_enabled is False


def test_setting_the_same_value_is_a_no_op(controller: PlaylistPlaybackController) -> None:
    """同じ値の再設定では通知しない。"""
    spy = QSignalSpy(controller.shuffle_enabled_changed)

    controller.set_shuffle_enabled(False)

    assert spy.count() == 0


@pytest.mark.parametrize("value", ["False", 1, None])
def test_invalid_shuffle_value_is_rejected(
    controller: PlaylistPlaybackController, value: object
) -> None:
    """bool以外を暗黙変換せずTypeErrorにする。"""
    with pytest.raises(TypeError, match="bool"):
        controller.set_shuffle_enabled(value)  # pyright: ignore[reportArgumentType]

    assert controller.shuffle_enabled is False


def test_toggling_shuffle_emits_once_and_does_not_play(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """切替だけでは load も play もしない。"""
    playlist.add_paths(audio_files)
    spy = QSignalSpy(controller.shuffle_enabled_changed)

    controller.set_shuffle_enabled(True)

    assert spy.count() == 1
    assert spy.at(0)[0] is True
    assert backend.call_names() == []


def test_toggling_shuffle_keeps_the_current_entry_and_audio(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """ON/OFF どちらでも現在 entry と再生中の音声を維持する。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[2])
    backend.calls.clear()

    controller.set_shuffle_enabled(True)
    assert controller.current_entry_id == entry_ids[2]
    assert backend.call_names() == []

    controller.set_shuffle_enabled(False)
    assert controller.current_entry_id == entry_ids[2]
    assert backend.call_names() == []


def test_shuffle_does_not_change_the_model_order(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """シャッフルは再生順だけで、Model の行順を変えない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    play_until_exhausted(controller, 10)

    assert [entry.entry_id for entry in playlist.entries()] == list(entry_ids)


def test_next_without_current_picks_a_random_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry が無くても next で候補から 1 件選ぶ。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)

    assert controller.play_next() is True

    assert controller.current_entry_id in entry_ids


def test_previous_without_current_is_false(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """履歴が無ければ previous は False。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)

    assert controller.play_previous() is False


# -- サイクル ---------------------------------------------------------------


def test_one_cycle_visits_each_entry_once(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """1 サイクルで各 entry を 1 回ずつ再生し、消化後は終了する（Repeat OFF）。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)

    played = play_until_exhausted(controller, 20)

    assert sorted(str(entry_id) for entry_id in played) == sorted(entry_ids)
    assert len(played) == len(set(played))
    assert controller.play_next() is False


def test_shuffle_is_reproducible_with_the_same_seed(
    playback: PlaybackController, playlist: PlaylistModel, audio_files: list[Path]
) -> None:
    """同じ seed なら同じ順序になる。"""
    entry_ids = playlist.add_paths(audio_files)

    orders: list[list[str | None]] = []
    for _ in range(2):
        controller = PlaylistPlaybackController(playback, playlist, rng=random.Random(SEED))
        controller.set_shuffle_enabled(True)
        orders.append(play_until_exhausted(controller, 20))

    assert orders[0] == orders[1]
    assert sorted(str(entry_id) for entry_id in orders[0]) == sorted(entry_ids)


def test_duplicate_paths_are_separate_shuffle_candidates(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """同じパスの 3 行はそれぞれ別の候補になる。"""
    entry_ids = playlist.add_paths([audio_files[0]] * 3)
    controller.set_shuffle_enabled(True)

    played = play_until_exhausted(controller, 10)

    assert sorted(str(entry_id) for entry_id in played) == sorted(entry_ids)


def test_repeat_all_starts_a_new_cycle(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """Repeat ALL では全候補を消化したあと新しいサイクルへ入る。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    first_cycle = play_until_exhausted(controller, len(entry_ids))
    assert len(first_cycle) == len(entry_ids)

    second_cycle = play_until_exhausted(controller, len(entry_ids))

    assert sorted(str(entry_id) for entry_id in second_cycle) == sorted(entry_ids)


def test_repeat_all_does_not_replay_the_same_entry_at_a_cycle_boundary(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """候補が 2 件以上なら、サイクル境界で直前の曲を即座に選び直さない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    play_until_exhausted(controller, len(entry_ids))
    last_of_cycle = controller.current_entry_id

    assert controller.play_next() is True

    assert controller.current_entry_id != last_of_cycle
    assert controller.current_entry_id in entry_ids


def test_repeat_all_with_a_single_entry_replays_it(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """候補が 1 件だけならサイクル境界で同じ entry を選び直してよい。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    assert controller.play_next() is True

    assert controller.play_next() is True

    assert controller.current_entry_id == entry_ids[0]


def test_repeat_one_does_not_consume_the_cycle(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """ONE の自動繰り返しではサイクルの訪問済み状態が進まない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ONE)
    assert controller.play_next() is True
    first = controller.current_entry_id

    finish_track(backend, qtbot)
    assert controller.current_entry_id == first

    # 残りの候補はすべて再生できる（ONE の繰り返しで消化されていない）。
    played = play_until_exhausted(controller, 10)
    assert sorted(str(entry_id) for entry_id in [first, *played]) == sorted(entry_ids)


def test_repeat_one_manual_next_moves_to_an_unvisited_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ONE でも手動 next は未訪問候補へ進む。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ONE)
    controller.play_entry(entry_ids[0])

    assert controller.play_next() is True

    assert controller.current_entry_id != entry_ids[0]


# -- 欠損 -------------------------------------------------------------------


def test_missing_candidates_are_skipped(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """欠損は候補から外れる。"""
    missing = [tmp_path / f"ない{index}.wav" for index in range(3)]
    entry_ids = playlist.add_paths([*audio_files[:2], *missing])
    controller.set_shuffle_enabled(True)

    played = play_until_exhausted(controller, 10)

    assert sorted(str(entry_id) for entry_id in played) == sorted(entry_ids[:2])


def test_candidate_deleted_after_the_check_is_skipped(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選択後に消えた候補は飛ばして、同じ操作内で別候補を探す（TOCTOU）。"""
    playlist.add_paths(audio_files[:2])
    controller.set_shuffle_enabled(True)
    original_refresh = PlaylistModel.refresh_entry_status
    deleted: list[str] = []

    def refresh_then_delete(model: PlaylistModel, entry_id: str) -> bool:
        result = original_refresh(model, entry_id)
        row = model.row_of_entry_id(entry_id)
        if row is not None and not deleted:
            deleted.append(entry_id)
            model.entry_at(row).path.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(PlaylistModel, "refresh_entry_status", refresh_then_delete)

    assert controller.play_next() is True

    assert controller.current_entry_id is not None
    assert controller.current_entry_id != deleted[0]


def test_all_candidates_missing_terminates(
    controller: PlaylistPlaybackController, playlist: PlaylistModel, tmp_path: Path
) -> None:
    """全候補が欠損でも有限時間で終わる（Repeat ALL でも）。"""
    playlist.add_paths([tmp_path / f"ない{index}.wav" for index in range(50)])
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)

    assert controller.play_next() is False
    assert controller.play_previous() is False


def test_decode_error_does_not_skip_to_another_candidate(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """デコードエラーでは次候補へ自動移動しない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_entry(entry_ids[0])
    backend.calls.clear()

    backend.emit_media_status(MediaStatus.INVALID_MEDIA)

    assert controller.current_entry_id == entry_ids[0]
    assert backend.call_names() == []


# -- 履歴 -------------------------------------------------------------------


def test_previous_walks_back_through_the_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """previous は再抽選せず、実際に通った順を戻る。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    assert len(visited) == 3

    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[1]

    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[0]


def test_next_after_previous_replays_the_future_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """previous したあとの next は、まず履歴の未来側へ戻る。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    controller.play_previous()
    controller.play_previous()

    assert controller.play_next() is True
    assert controller.current_entry_id == visited[1]

    assert controller.play_next() is True
    assert controller.current_entry_id == visited[2]

    # 未来履歴を使い切って初めて新しい候補を選ぶ。
    assert controller.play_next() is True
    assert controller.current_entry_id not in visited


def test_direct_play_truncates_the_future_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """戻った状態で別 entry を直接再生すると、未来履歴を捨てる。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    controller.play_previous()
    controller.play_previous()

    direct = next(entry_id for entry_id in entry_ids if entry_id not in visited)
    assert controller.play_entry(direct) is True

    # 未来履歴は捨てられ、戻る先は最初に通った entry。
    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[0]
    assert controller.play_next() is True
    assert controller.current_entry_id == direct


def test_replaying_the_current_entry_does_not_duplicate_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry を直接再生し直しても履歴が伸びない。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    first = controller.current_entry_id
    controller.play_next()
    second = controller.current_entry_id
    assert second is not None

    controller.play_entry(second)
    controller.play_entry(second)

    assert controller.play_previous() is True
    assert controller.current_entry_id == first


def test_replaying_current_entry_keeps_future_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """previous後に現在entryを再実行してもnextで元の未来へ戻る。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    assert len(visited) == 3
    assert controller.play_previous() is True
    current = visited[1]
    assert controller.current_entry_id == current

    assert isinstance(current, str)
    assert controller.play_entry(current) is True
    assert controller.play_next() is True

    assert controller.current_entry_id == visited[2]


def test_failed_direct_play_does_not_change_the_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """欠損で再生できなかった entry は履歴へ入らない。"""
    entry_ids = playlist.add_paths([*audio_files[:2], tmp_path / "ない.wav"])
    controller.set_shuffle_enabled(True)
    controller.play_entry(entry_ids[0])
    controller.play_entry(entry_ids[1])

    assert controller.play_entry(entry_ids[2]) is False

    assert controller.current_entry_id == entry_ids[1]
    assert controller.play_previous() is True
    assert controller.current_entry_id == entry_ids[0]


def test_history_skips_removed_entries(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """履歴の途中の entry が削除されても安全に飛ばす。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    middle = visited[1]
    assert middle is not None
    row = playlist.row_of_entry_id(middle)
    assert row is not None

    playlist.removeRows(row, 1)

    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[0]


def test_history_skips_entries_that_became_missing(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """履歴の entry が欠損しても安全に飛ばす。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    middle = visited[1]
    assert middle is not None
    row = playlist.row_of_entry_id(middle)
    assert row is not None
    playlist.entry_at(row).path.unlink()

    assert controller.play_previous() is True

    assert controller.current_entry_id == visited[0]


def test_removing_another_entry_keeps_the_cursor_on_the_current_entry(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry 以外を削除しても cursor は現在 entry を指したまま。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    current = controller.current_entry_id
    unrelated = next(
        entry.entry_id for entry in playlist.entries() if entry.entry_id not in visited
    )

    row = playlist.row_of_entry_id(unrelated)
    assert row is not None
    playlist.removeRows(row, 1)

    assert controller.current_entry_id == current
    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[1]


def test_pruning_repeated_history_keeps_the_actual_cursor(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """Repeat ALLの複数サイクル履歴でも削除後のcursorが過去へ飛ばない。"""
    entry_ids = playlist.add_paths(audio_files[:4])
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    played: list[str] = []
    for _ in range(6):
        assert controller.play_next() is True
        assert controller.current_entry_id is not None
        played.append(controller.current_entry_id)

    assert played[-1] in played[:-1]
    expected_previous = played[-2]
    removable = next(
        entry_id for entry_id in entry_ids if entry_id not in {played[-1], expected_previous}
    )
    row = playlist.row_of_entry_id(removable)
    assert row is not None

    playlist.removeRows(row, 1)

    assert controller.play_previous() is True
    assert controller.current_entry_id == expected_previous


def test_pruning_keeps_visited_entries_outside_truncated_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """未来履歴を切り捨てても訪問済みentryを同じサイクルで再抽選しない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]
    assert len(visited) == 3
    assert controller.play_previous() is True
    assert controller.play_previous() is True

    unvisited = [entry_id for entry_id in entry_ids if entry_id not in visited]
    assert len(unvisited) == 2
    direct, remove_unvisited = unvisited
    assert controller.play_entry(direct) is True

    for entry_id in (remove_unvisited, visited[0]):
        assert isinstance(entry_id, str)
        row = playlist.row_of_entry_id(entry_id)
        assert row is not None
        playlist.removeRows(row, 1)

    assert controller.play_next() is False


def test_auto_advance_rejection_does_not_report_end_of_playlist(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """シャッフル次候補の同期拒否を末尾到達として表示しない。"""
    entry_ids = playlist.add_paths(audio_files[:2])
    controller.set_shuffle_enabled(True)
    assert controller.play_entry(entry_ids[0]) is True
    messages = QSignalSpy(controller.message_requested)
    errors = QSignalSpy(playback.error_occurred)
    error = PlaybackError(
        code=PlaybackErrorCode.FORMAT_ERROR,
        message="この音声形式は再生できません。",
        detail="test",
    )

    def reject_load(path: Path) -> None:
        del path
        playback.error_occurred.emit(error)

    monkeypatch.setattr(playback, "load", reject_load)

    finish_track(backend, qtbot)

    assert controller.current_entry_id == entry_ids[0]
    assert errors.count() == 1
    assert messages.count() == 0


def test_moving_rows_does_not_change_the_history_order(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """行の並べ替えで履歴の順序は変わらない。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    visited = [controller.current_entry_id for _ in range(3) if controller.play_next()]

    assert playlist.moveRows(QModelIndex(), 0, 2, QModelIndex(), 5) is True

    assert controller.play_previous() is True
    assert controller.current_entry_id == visited[1]


def test_new_entries_join_the_current_cycle(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
) -> None:
    """シャッフル中に追加された entry は未訪問候補になる。"""
    playlist.add_paths(audio_files[:2])
    controller.set_shuffle_enabled(True)
    play_until_exhausted(controller, 5)
    assert controller.play_next() is False
    added = tmp_path / "追加曲.wav"
    added.write_bytes(b"x")

    new_ids = playlist.add_paths([added])

    assert controller.play_next() is True
    assert controller.current_entry_id == new_ids[0]


def test_removing_the_current_entry_clears_the_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    backend: FakePlaybackBackend,
    audio_files: list[Path],
) -> None:
    """現在 entry の削除で履歴を捨てる。音声は止めず、設定は維持する。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    for _ in range(3):
        controller.play_next()
    current = controller.current_entry_id
    assert current is not None
    row = playlist.row_of_entry_id(current)
    assert row is not None
    backend.calls.clear()

    playlist.removeRows(row, 1)

    assert controller.current_entry_id is None
    assert controller.shuffle_enabled is True
    assert backend.call_names() == []
    assert controller.play_previous() is False
    # 新しいシャッフルセッションとして候補を選べる。
    assert controller.play_next() is True


def test_clearing_the_playlist_clears_the_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """全消去で履歴を捨て、設定は維持する。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_next()

    playlist.clear()

    assert controller.current_entry_id is None
    assert controller.shuffle_enabled is True
    assert controller.repeat_mode is RepeatMode.ALL
    assert controller.play_previous() is False


def test_direct_single_track_load_clears_the_history(
    controller: PlaylistPlaybackController,
    playback: PlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """「開く...」による直接読み込みで履歴を捨てる。設定は維持する。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    controller.play_next()

    playback.load(audio_files[0])

    assert controller.current_entry_id is None
    assert controller.shuffle_enabled is True
    assert controller.play_previous() is False


def test_disabling_shuffle_discards_the_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """OFF にすると履歴を捨て、行順のナビゲーションへ戻る。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_entry(entry_ids[4])
    controller.play_entry(entry_ids[1])

    controller.set_shuffle_enabled(False)

    assert controller.play_previous() is True
    assert controller.current_entry_id == entry_ids[0]


def test_re_enabling_shuffle_starts_a_new_session(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """再度 ON にしても古い履歴は復元しない。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_entry(entry_ids[0])
    controller.play_entry(entry_ids[1])
    controller.set_shuffle_enabled(False)

    controller.set_shuffle_enabled(True)

    assert controller.play_previous() is False


def test_repeat_all_does_not_wrap_shuffle_history_backwards(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """Repeat ALL でも shuffle の previous は履歴の先頭より前へ行かない。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.set_repeat_mode(RepeatMode.ALL)
    controller.play_next()
    controller.play_next()

    assert controller.play_previous() is True
    assert controller.play_previous() is False


def test_enabling_shuffle_treats_the_current_entry_as_visited(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """ON にした時点の現在 entry は訪問済みとして扱われ、履歴の起点になる。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[2])

    controller.set_shuffle_enabled(True)

    played = play_until_exhausted(controller, 10)
    assert entry_ids[2] not in played
    assert sorted(str(entry_id) for entry_id in played) == sorted(
        entry_id for entry_id in entry_ids if entry_id != entry_ids[2]
    )
    assert controller.play_previous() is True


# -- navigation availability ------------------------------------------------


def test_navigation_without_history(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """履歴が無ければ previous 不可、候補があれば next 可。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)

    assert controller.can_play_previous is False
    assert controller.can_play_next is True


def test_navigation_follows_the_history_cursor(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """履歴の位置に応じて前後の可否が変わる。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    assert controller.can_play_previous is False

    controller.play_next()
    assert controller.can_play_previous is True

    controller.play_previous()
    assert controller.can_play_next is True


def test_navigation_after_the_cycle_is_exhausted(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """OFF では消化後に next 不可、ALL では可のまま。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    play_until_exhausted(controller, 10)
    assert controller.can_play_next is False

    controller.set_repeat_mode(RepeatMode.ALL)

    assert controller.can_play_next is True


def test_navigation_is_false_after_the_current_entry_is_removed(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """現在 entry の削除後は previous 不可。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    controller.play_next()
    current = controller.current_entry_id
    assert current is not None
    row = playlist.row_of_entry_id(current)
    assert row is not None

    playlist.removeRows(row, 1)

    assert controller.can_play_previous is False


def test_navigation_signal_is_emitted_on_shuffle_toggle(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
) -> None:
    """シャッフル切替で可否が変われば通知される。"""
    entry_ids = playlist.add_paths(audio_files)
    controller.play_entry(entry_ids[-1])
    assert (controller.can_play_previous, controller.can_play_next) == (True, False)
    spy = QSignalSpy(controller.navigation_availability_changed)

    controller.set_shuffle_enabled(True)

    assert (controller.can_play_previous, controller.can_play_next) == (False, True)
    assert spy.count() == 1


def test_navigation_does_not_touch_the_filesystem(
    controller: PlaylistPlaybackController,
    playlist: PlaylistModel,
    audio_files: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可否の計算でファイルシステムを走査しない。"""
    playlist.add_paths(audio_files)
    controller.set_shuffle_enabled(True)
    controller.play_next()
    calls: list[str] = []

    def counted_refresh(model: PlaylistModel, entry_id: str) -> bool:
        calls.append(entry_id)
        return False

    def counted_refresh_all(model: PlaylistModel) -> int:
        del model
        calls.append("all")
        return 0

    monkeypatch.setattr(PlaylistModel, "refresh_entry_status", counted_refresh)
    monkeypatch.setattr(PlaylistModel, "refresh_file_status", counted_refresh_all)

    controller.set_repeat_mode(RepeatMode.ALL)
    playlist.add_paths(audio_files[:1])

    assert calls == []


# -- 大量データ -------------------------------------------------------------


def test_shuffle_with_1000_entries(
    playback: PlaybackController, playlist: PlaylistModel, tmp_path: Path
) -> None:
    """1000 件でも 1 サイクルで全件を 1 回ずつ消化し、実用的な時間で終わる。"""
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")
    entry_ids = playlist.add_paths(paths)
    controller = PlaylistPlaybackController(playback, playlist, rng=random.Random(SEED))
    controller.set_shuffle_enabled(True)

    start = time.perf_counter()
    played: list[str | None] = []
    for _ in range(1000):
        assert controller.play_next() is True
        played.append(controller.current_entry_id)
    forward_seconds = time.perf_counter() - start

    assert controller.play_next() is False
    assert len(set(played)) == 1000
    assert sorted(str(entry_id) for entry_id in played) == sorted(entry_ids)
    assert [entry.entry_id for entry in playlist.entries()] == list(entry_ids)

    start = time.perf_counter()
    for _ in range(999):
        assert controller.play_previous() is True
    for _ in range(999):
        assert controller.play_next() is True
    history_seconds = time.perf_counter() - start

    # 明らかな O(n^2) ではないことの確認（CI では厳しい上限を課さない）。
    assert forward_seconds < 30.0
    assert history_seconds < 30.0

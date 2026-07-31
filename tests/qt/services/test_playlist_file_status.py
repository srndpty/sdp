"""ファイル状態の背景確認サービスの契約を検証する。

エントリ生成でファイルへ触れない代わりに、ここが少しずつ状態を確定させる。
GUIスレッドを止めないことが目的なので、バッチ単位で直列に走ること、
古い世代の結果を捨てること、Modelを他スレッドから触らないことを確かめる。
"""

from collections.abc import Iterator
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QThreadPool
from pytestqt.qtbot import QtBot

from sdp.core.playlist.entry import FileStatus
from sdp.core.playlist.model import PlaylistModel
from sdp.services.playlist_file_status import PlaylistFileStatusChecker

WAIT_TIMEOUT_MS = 5_000


@pytest.fixture
def pool() -> Iterator[QThreadPool]:
    """テスト間で共有しない専用のスレッドプール。"""
    created = QThreadPool()
    created.setMaxThreadCount(1)
    yield created
    created.waitForDone(WAIT_TIMEOUT_MS)


@pytest.fixture
def playlist(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(5):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def wait_until_checked(qtbot: QtBot, playlist: PlaylistModel) -> None:
    qtbot.waitUntil(lambda: playlist.unchecked_entries(1) == (), timeout=WAIT_TIMEOUT_MS)


def test_existing_entries_are_checked_after_construction(
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
    pool: QThreadPool,
    qtbot: QtBot,
) -> None:
    """構築時点で既にある行も確認される。"""
    playlist.add_paths([audio_files[0], tmp_path / "ない曲.wav"])
    checker = PlaylistFileStatusChecker(playlist, pool=pool)

    wait_until_checked(qtbot, playlist)

    assert playlist.entry_at(0).file_status is FileStatus.AVAILABLE
    assert playlist.entry_at(1).file_status is FileStatus.MISSING
    checker.shutdown()


def test_added_rows_are_checked_in_the_background(
    playlist: PlaylistModel,
    audio_files: list[Path],
    tmp_path: Path,
    pool: QThreadPool,
    qtbot: QtBot,
) -> None:
    """追加された行も背景で確認され、欠損だけがグレー対象になる。"""
    checker = PlaylistFileStatusChecker(playlist, pool=pool)

    playlist.add_paths([*audio_files, tmp_path / "ない曲.wav"])
    # 追加直後は未確認。ここでGUIスレッドがstatを実行していない。
    assert all(entry.file_status is FileStatus.UNKNOWN for entry in playlist.entries())

    wait_until_checked(qtbot, playlist)

    assert playlist.missing_entry_ids() == (playlist.entry_at(len(audio_files)).entry_id,)
    checker.shutdown()


def test_more_entries_than_one_batch_are_all_checked(
    playlist: PlaylistModel, tmp_path: Path, pool: QThreadPool, qtbot: QtBot
) -> None:
    """バッチ境界をまたいでも全件が確定する。"""
    paths: list[Path] = []
    for index in range(10):
        path = tmp_path / f"曲{index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    playlist.add_paths(paths)
    checker = PlaylistFileStatusChecker(playlist, batch_size=3, pool=pool)

    wait_until_checked(qtbot, playlist)

    assert all(entry.file_status is FileStatus.AVAILABLE for entry in playlist.entries())
    checker.shutdown()


def test_shutdown_stops_further_batches(
    playlist: PlaylistModel, audio_files: list[Path], pool: QThreadPool, qtbot: QtBot
) -> None:
    """shutdown後は新しいバッチを始めず、結果も反映しない。"""
    checker = PlaylistFileStatusChecker(playlist, batch_size=1, pool=pool)
    checker.shutdown()

    playlist.add_paths(audio_files)
    qtbot.wait(50)

    assert all(entry.file_status is FileStatus.UNKNOWN for entry in playlist.entries())
    assert checker.is_running is False


def test_removed_entries_do_not_break_the_update(
    playlist: PlaylistModel, audio_files: list[Path], pool: QThreadPool, qtbot: QtBot
) -> None:
    """確認中に行が消えても、残った行だけが更新される。"""
    playlist.add_paths(audio_files)
    checker = PlaylistFileStatusChecker(playlist, pool=pool)
    playlist.removeRows(0, 1)

    wait_until_checked(qtbot, playlist)

    assert playlist.rowCount() == len(audio_files) - 1
    assert all(entry.file_status is FileStatus.AVAILABLE for entry in playlist.entries())
    checker.shutdown()


def test_run_pending_now_resolves_synchronously(
    playlist: PlaylistModel, audio_files: list[Path], tmp_path: Path, pool: QThreadPool
) -> None:
    """同期経路は待たずに全件を確定させる。"""
    playlist.add_paths([audio_files[0], tmp_path / "ない曲.wav"])
    checker = PlaylistFileStatusChecker(playlist, pool=pool)
    checker.shutdown()

    assert checker.run_pending_now() == 2

    assert playlist.entry_at(0).file_status is FileStatus.AVAILABLE
    assert playlist.entry_at(1).file_status is FileStatus.MISSING
    assert checker.run_pending_now() == 0


def test_batch_size_must_be_positive(playlist: PlaylistModel, pool: QThreadPool) -> None:
    """0以下のバッチサイズは無限ループになるため拒否する。"""
    with pytest.raises(ValueError):
        PlaylistFileStatusChecker(playlist, batch_size=0, pool=pool)


def test_shutdown_waits_for_the_running_batch_but_not_forever(
    playlist: PlaylistModel, audio_files: list[Path], pool: QThreadPool
) -> None:
    """実行中バッチの完了を待つが、上限を超えたら諦めて戻る。"""
    playlist.add_paths(audio_files)
    checker = PlaylistFileStatusChecker(playlist, pool=pool)

    assert checker.shutdown() is True
    # 冪等。2回目も即座に戻る。
    assert checker.shutdown() is True


def test_shutdown_returns_even_when_the_batch_does_not_finish(
    playlist: PlaylistModel, audio_files: list[Path], pool: QThreadPool
) -> None:
    """バッチが戻らなくても、待機上限で制御を返す（終了処理を固めない）。"""
    playlist.add_paths(audio_files)
    checker = PlaylistFileStatusChecker(playlist, pool=pool)
    # 完了イベントを未設定のものへ差し替え、「終わらないバッチ」を模す。
    checker._batch_done = Event()  # pyright: ignore[reportPrivateUsage]

    assert checker.shutdown(wait_ms=20) is False

"""MetadataReader の非同期契約を検証する。

読み取り関数を注入して決定的にする。待機は threading.Event と
qtbot.waitUntil / waitSignal で行い、固定 sleep は使わない。
"""

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, QTimer
from pytestqt.qtbot import QtBot

from sdp.core.metadata.reader import MetadataReader, MetadataResult
from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playlist.model import PlaylistModel

WAIT_TIMEOUT_MS = 5_000
SAMPLE = TrackMetadata(title="曲名", artist="奏者", album="盤", duration_ms=1000)


class RecordingReader:
    """呼び出しを記録し、必要なら結果を保留できるテスト用の読み取り関数。"""

    def __init__(self, metadata: TrackMetadata | None = SAMPLE) -> None:
        self.metadata = metadata
        self.calls: list[Path] = []
        self._lock = threading.Lock()
        self.released = threading.Event()
        self.released.set()
        self.started = threading.Event()
        self.error: Exception | None = None

    def __call__(self, path: Path) -> TrackMetadata:
        with self._lock:
            self.calls.append(path)
        self.started.set()
        self.released.wait(timeout=10.0)
        if self.error is not None:
            raise self.error
        assert self.metadata is not None
        return self.metadata

    def hold(self) -> None:
        self.released.clear()

    def release(self) -> None:
        self.released.set()

    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)


@pytest.fixture
def playlist(qtbot: QtBot) -> Iterator[PlaylistModel]:
    del qtbot
    yield PlaylistModel()


@pytest.fixture
def read_function() -> Iterator[RecordingReader]:
    yield RecordingReader()


@pytest.fixture
def reader(playlist: PlaylistModel, read_function: RecordingReader) -> Iterator[MetadataReader]:
    instance = MetadataReader(playlist, read_function=read_function, max_threads=2)
    yield instance
    instance.shutdown(timeout_ms=2_000)


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(3):
        path = tmp_path / f"曲 {index}.wav"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def wait_for_status(
    qtbot: QtBot, playlist: PlaylistModel, row: int, status: MetadataStatus
) -> None:
    qtbot.waitUntil(
        lambda: playlist.entry_at(row).metadata_status is status, timeout=WAIT_TIMEOUT_MS
    )


# -- ライフサイクル ---------------------------------------------------------


def test_start_is_idempotent(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """start を複数回呼んでも二重にスケジュールしない。"""
    playlist.add_paths(audio_files)

    reader.start()
    reader.start()

    qtbot.waitUntil(lambda: read_function.call_count() == len(audio_files), timeout=WAIT_TIMEOUT_MS)
    assert read_function.call_count() == len(audio_files)


def test_existing_entries_are_scheduled_on_start(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """start 時点の既存エントリを読み取る。"""
    playlist.add_paths(audio_files)

    reader.start()

    qtbot.waitUntil(lambda: read_function.call_count() == 3, timeout=WAIT_TIMEOUT_MS)
    assert sorted(read_function.calls) == sorted(path.resolve() for path in audio_files)


def test_inserted_rows_are_scheduled(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """あとから追加されたエントリも読み取る。"""
    reader.start()

    playlist.add_paths(audio_files)

    qtbot.waitUntil(lambda: read_function.call_count() == 3, timeout=WAIT_TIMEOUT_MS)


def test_missing_entries_are_not_scheduled(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    tmp_path: Path,
    qtbot: QtBot,
) -> None:
    """欠損エントリは読み取らない。"""
    playlist.add_paths([tmp_path / "ない曲.wav"])
    playlist.refresh_file_status()

    reader.start()
    qtbot.wait(50)

    assert read_function.calls == []
    assert playlist.entry_at(0).metadata_status is MetadataStatus.NOT_REQUESTED


def test_successful_read_becomes_loaded(
    reader: MetadataReader,
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """成功したら LOADED になり、値が入る。"""
    playlist.add_paths(audio_files[:1])

    reader.start()

    wait_for_status(qtbot, playlist, 0, MetadataStatus.LOADED)
    assert playlist.entry_at(0).metadata == SAMPLE


def test_failed_read_becomes_failed_and_is_logged(
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """失敗したら FAILED になり、技術詳細をログへ残す。"""
    read_function = RecordingReader()
    read_function.error = OSError("読めません")
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(audio_files[:1])

    with caplog.at_level("INFO"):
        reader.start()
        wait_for_status(qtbot, playlist, 0, MetadataStatus.FAILED)

    assert playlist.entry_at(0).metadata is None
    assert "メタデータを読み取れませんでした" in caplog.text
    reader.shutdown(timeout_ms=2_000)


def test_unexpected_read_error_is_logged_with_traceback(
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """予期しない例外は FAILED にし、traceback 付きで記録する。"""
    read_function = RecordingReader()
    read_function.error = AttributeError("壊れた抽出処理")
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(audio_files[:1])

    with caplog.at_level(logging.ERROR):
        reader.start()
        wait_for_status(qtbot, playlist, 0, MetadataStatus.FAILED)

    assert "メタデータ読み取りで予期しない例外" in caplog.text
    assert "Traceback" in caplog.text
    assert "AttributeError: 壊れた抽出処理" in caplog.text
    reader.shutdown(timeout_ms=2_000)


def test_model_is_updated_on_the_gui_thread(
    reader: MetadataReader,
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """Model の更新は GUI スレッドで行われる。"""
    gui_thread = threading.current_thread()
    update_threads: list[threading.Thread] = []

    def on_changed(top_left: QModelIndex, bottom_right: QModelIndex, roles: list[int]) -> None:
        del top_left, bottom_right, roles
        update_threads.append(threading.current_thread())

    playlist.dataChanged.connect(on_changed)
    playlist.add_paths(audio_files[:1])

    reader.start()
    wait_for_status(qtbot, playlist, 0, MetadataStatus.LOADED)

    assert update_threads
    assert all(thread is gui_thread for thread in update_threads)


def test_real_files_are_read_asynchronously(
    playlist: PlaylistModel, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """実ファイルでも非同期に完了する（既定の read 関数を使う）。"""
    reader = MetadataReader(playlist, max_threads=2)
    playlist.add_paths([test_audio_dir / "sine440.wav", test_audio_dir / "sine440.mp3"])

    reader.start()

    qtbot.waitUntil(
        lambda: all(entry.metadata_status is MetadataStatus.LOADED for entry in playlist.entries()),
        timeout=WAIT_TIMEOUT_MS,
    )
    assert all(entry.metadata is not None for entry in playlist.entries())
    reader.shutdown(timeout_ms=2_000)


# -- 古い結果の防止 ---------------------------------------------------------


def test_duplicate_paths_are_updated_separately(
    reader: MetadataReader,
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """同じパスの 2 行がそれぞれ更新される。"""
    playlist.add_paths([audio_files[0], audio_files[0]])

    reader.start()

    qtbot.waitUntil(
        lambda: all(entry.metadata_status is MetadataStatus.LOADED for entry in playlist.entries()),
        timeout=WAIT_TIMEOUT_MS,
    )
    assert playlist.entry_at(0).entry_id != playlist.entry_at(1).entry_id


def test_result_follows_the_entry_after_a_move(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """読み取り中に行が動いても、正しい entry へ適用する。"""
    entry_ids = playlist.add_paths(audio_files)
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    assert playlist.moveRows(QModelIndex(), 0, 1, QModelIndex(), 3) is True
    read_function.release()

    qtbot.waitUntil(
        lambda: playlist.entry_at(playlist.row_of_entry_id(entry_ids[0]) or 0).metadata is not None,
        timeout=WAIT_TIMEOUT_MS,
    )
    assert playlist.row_of_entry_id(entry_ids[0]) == 2
    assert playlist.entry_at(2).metadata == SAMPLE


def test_result_for_a_removed_entry_is_discarded(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """読み取り中に削除された entry の結果は捨てる。"""
    playlist.add_paths(audio_files[:1])
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    playlist.removeRows(0, 1)
    read_function.release()
    qtbot.wait(50)

    assert playlist.rowCount() == 0


def test_removing_many_loading_entries_reclaims_tokens(
    playlist: PlaylistModel, tmp_path: Path, qtbot: QtBot
) -> None:
    """大量削除でtokenを回収し、遅れて届く結果も適用しない。"""
    paths = [tmp_path / f"削除曲 {index:03d}.wav" for index in range(100)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(paths)
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)
    # 投入は上限つき。tokenも投入済みの件数までしか作らない。
    assert len(reader._tokens) <= reader.max_in_flight  # pyright: ignore[reportPrivateUsage]
    assert reader.pending_count > 0

    assert playlist.removeRows(0, 100) is True

    assert reader._tokens == {}  # pyright: ignore[reportPrivateUsage]
    assert reader.pending_count == 0
    read_function.release()
    qtbot.wait(50)
    assert playlist.rowCount() == 0
    reader.shutdown(timeout_ms=2_000)


def test_result_from_before_a_reset_is_discarded(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """reset 前の結果を reset 後へ適用しない。"""
    playlist.add_paths(audio_files[:1])
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)
    stale_entry_id = playlist.entry_at(0).entry_id

    playlist.replace_entries([])
    read_function.release()
    qtbot.wait(50)

    assert playlist.rowCount() == 0
    assert playlist.row_of_entry_id(stale_entry_id) is None


def test_stale_token_is_ignored(
    reader: MetadataReader,
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """同じ entry の古い要求は無視する。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    reader.start()
    wait_for_status(qtbot, playlist, 0, MetadataStatus.LOADED)
    other = TrackMetadata(title="古い結果")

    reader._on_result(  # pyright: ignore[reportPrivateUsage]
        MetadataResult(
            entry_id=entry_ids[0],
            path=playlist.entry_at(0).path,
            token=-1,
            metadata=other,
        )
    )

    assert playlist.entry_at(0).metadata == SAMPLE


def test_result_with_a_different_path_is_ignored(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """同じ entry_id でもパスが違う結果は適用しない。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    # 別パスの結果を（トークンが一致していても）持ち込む。
    reader._on_result(  # pyright: ignore[reportPrivateUsage]
        MetadataResult(
            entry_id=entry_ids[0],
            path=audio_files[1].resolve(),
            token=reader._tokens[entry_ids[0]],  # pyright: ignore[reportPrivateUsage]
            metadata=TrackMetadata(title="別ファイル"),
        )
    )
    assert entry_ids[0] not in reader._tokens  # pyright: ignore[reportPrivateUsage]
    assert playlist.entry_at(0).metadata_status is MetadataStatus.FAILED
    assert playlist.entry_at(0).metadata is None
    read_function.release()
    qtbot.wait(50)

    # 同じtokenの遅延結果も復活させない。
    assert playlist.entry_at(0).metadata_status is MetadataStatus.FAILED


def test_result_for_a_file_that_disappeared_is_ignored(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """読み取り中に欠損したら結果を捨てる。"""
    entry_ids = playlist.add_paths(audio_files[:1])
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    audio_files[0].unlink()
    playlist.refresh_entry_status(entry_ids[0])
    assert entry_ids[0] not in reader._tokens  # pyright: ignore[reportPrivateUsage]
    read_function.release()
    qtbot.wait(50)

    entry = playlist.entry_at(0)
    assert entry.is_missing
    assert entry.metadata is None
    assert entry.metadata_status is MetadataStatus.NOT_REQUESTED


def test_restored_file_is_read_again(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    tmp_path: Path,
    qtbot: QtBot,
) -> None:
    """欠損から復活したら再読み取りする。"""
    path = tmp_path / "戻る曲.wav"
    entry_ids = playlist.add_paths([path])
    playlist.refresh_file_status()
    reader.start()
    qtbot.wait(20)
    assert read_function.calls == []

    path.write_bytes(b"x")
    playlist.refresh_entry_status(entry_ids[0])

    qtbot.waitUntil(lambda: read_function.call_count() == 1, timeout=WAIT_TIMEOUT_MS)
    wait_for_status(qtbot, playlist, 0, MetadataStatus.LOADED)


def test_metadata_updates_do_not_reschedule(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """メタデータ更新の dataChanged で再スケジュールしない（無限ループ防止）。"""
    playlist.add_paths(audio_files)

    reader.start()

    qtbot.waitUntil(lambda: read_function.call_count() == 3, timeout=WAIT_TIMEOUT_MS)
    qtbot.wait(50)
    assert read_function.call_count() == 3


# -- shutdown ---------------------------------------------------------------


def test_shutdown_stops_new_requests(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """shutdown 後は新しい要求を受け付けない。"""
    reader.start()
    reader.shutdown(timeout_ms=2_000)

    playlist.add_paths(audio_files)
    qtbot.wait(50)

    assert read_function.calls == []
    assert reader.is_running is False


def test_results_after_shutdown_are_not_applied(
    reader: MetadataReader,
    playlist: PlaylistModel,
    read_function: RecordingReader,
    audio_files: list[Path],
    qtbot: QtBot,
) -> None:
    """shutdown 後に届いた結果は適用しない。"""
    playlist.add_paths(audio_files[:1])
    read_function.hold()
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    read_function.release()
    reader.shutdown(timeout_ms=2_000)
    qtbot.wait(50)

    assert playlist.entry_at(0).metadata is None


def test_shutdown_clears_pending_tasks_and_returns(
    playlist: PlaylistModel, audio_files: list[Path], tmp_path: Path, qtbot: QtBot
) -> None:
    """未開始のタスクを捨て、無期限には待たない。"""
    paths = [tmp_path / f"多い曲 {index}.wav" for index in range(50)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    pending_cleared = threading.Event()

    class SynchronizedShutdownReader(MetadataReader):
        """pending taskの破棄後に実行中workerを解放するtest double。"""

        def _clear_pending_tasks(self) -> None:
            super()._clear_pending_tasks()
            pending_cleared.set()
            read_function.release()

    reader = SynchronizedShutdownReader(
        playlist,
        read_function=read_function,
        max_threads=1,
    )
    playlist.add_paths(paths)
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    reader.shutdown(timeout_ms=2_000)

    assert pending_cleared.is_set()
    assert reader.is_running is False
    # 全 50 件が実行される前に終わっている。
    assert read_function.call_count() < len(paths)


def test_shutdown_timeout_is_only_a_logical_cancellation(
    playlist: PlaylistModel,
    audio_files: list[Path],
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """実行中I/Oは強制停止せず、待機timeout後は警告して論理キャンセルする。"""
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(audio_files[:1])
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    with caplog.at_level("WARNING"):
        reader.shutdown(timeout_ms=10)

    assert not reader.is_running
    assert "終了待ちがタイムアウト" in caplog.text

    # QThreadPool破棄は実行中タスクを待つため、テストの終了前に協調的に解放する。
    read_function.release()
    assert reader._pool.waitForDone(2_000)  # pyright: ignore[reportPrivateUsage]


def test_shutdown_is_idempotent(reader: MetadataReader) -> None:
    """shutdown を複数回呼んでも問題ない。"""
    reader.start()
    reader.shutdown(timeout_ms=1_000)
    reader.shutdown(timeout_ms=1_000)

    assert reader.is_running is False


def test_reader_can_be_deleted_after_shutdown(
    playlist: PlaylistModel, audio_files: list[Path], qtbot: QtBot
) -> None:
    """破棄後に結果が届いてもクラッシュしない。"""
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(audio_files[:1])
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)

    # 実行中のワーカーを解放してから破棄する（QThreadPool の破棄は実行中の
    # タスクを待つため、待機中のまま破棄するとテストが長く止まる）。
    read_function.release()
    reader.shutdown(timeout_ms=2_000)
    del reader
    qtbot.wait(50)

    assert playlist.rowCount() == 1
    assert playlist.entry_at(0).metadata is None


# -- 並列数と GUI 応答性 ----------------------------------------------------


def test_thread_count_is_bounded(playlist: PlaylistModel) -> None:
    """並列数は過度に大きくしない。"""
    default_reader = MetadataReader(playlist)

    assert 1 <= default_reader.max_thread_count <= 4
    assert MetadataReader(playlist, max_threads=2).max_thread_count == 2
    assert MetadataReader(playlist, max_threads=0).max_thread_count == 1


def test_gui_stays_responsive_while_reading(
    playlist: PlaylistModel, tmp_path: Path, qtbot: QtBot
) -> None:
    """1000 件の読み取り中でも GUI のイベントが処理される。"""
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    max_threads = 2
    reader = MetadataReader(playlist, read_function=read_function, max_threads=max_threads)
    playlist.add_paths(paths)
    heartbeats: list[int] = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: heartbeats.append(1))
    timer.start()

    reader.start()  # 1000 件を同期読取しない
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)
    qtbot.waitUntil(lambda: len(heartbeats) >= 5, timeout=WAIT_TIMEOUT_MS)

    # ワーカーが待機中でも GUI は動き、同時実行数は設定値以下。
    assert read_function.call_count() <= max_threads
    assert [entry.entry_id for entry in playlist.entries()][:3] == [
        playlist.entry_at(row).entry_id for row in range(3)
    ]
    timer.stop()
    read_function.release()
    reader.shutdown(timeout_ms=2_000)


def test_scheduling_1000_entries_does_not_read_synchronously(
    playlist: PlaylistModel, tmp_path: Path
) -> None:
    """schedule 呼び出しが 1000 ファイルを同期読取しない。"""
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=2)
    playlist.add_paths(paths)

    reader.start()

    assert read_function.call_count() < len(paths)
    read_function.release()
    reader.shutdown(timeout_ms=2_000)


def test_scheduling_1000_entries_bounds_the_submitted_work(
    playlist: PlaylistModel, tmp_path: Path
) -> None:
    """1000曲でもQThreadPoolへ積む件数は上限で頭打ちになる。

    全件を即座に投入すると、worker・Signal object・requestが件数ぶん作られ、
    起動直後のメモリとI/O量が予測できなくなる。
    """
    paths = [tmp_path / f"曲 {index:04d}.wav" for index in range(1000)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=2)
    playlist.add_paths(paths)

    reader.start()

    assert reader.in_flight_count <= reader.max_in_flight
    assert reader.pending_count == len(paths) - reader.in_flight_count
    loading = [
        entry for entry in playlist.entries() if entry.metadata_status is MetadataStatus.LOADING
    ]
    assert len(loading) == reader.in_flight_count
    # 残りは未要求のまま待機する（LOADINGへ固着させない）。
    assert all(
        entry.metadata_status is MetadataStatus.NOT_REQUESTED
        for entry in playlist.entries()[reader.in_flight_count :]
    )
    read_function.release()
    reader.shutdown(timeout_ms=2_000)


def test_all_entries_are_eventually_read_through_the_bounded_queue(
    playlist: PlaylistModel, tmp_path: Path, qtbot: QtBot
) -> None:
    """上限つき投入でも、待ち行列を消化して全件が読まれる。"""
    paths = [tmp_path / f"曲 {index:03d}.wav" for index in range(50)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=2)
    playlist.add_paths(paths)

    reader.start()

    qtbot.waitUntil(lambda: read_function.call_count() == len(paths), timeout=WAIT_TIMEOUT_MS)
    qtbot.waitUntil(lambda: reader.pending_count == 0, timeout=WAIT_TIMEOUT_MS)
    assert reader.in_flight_count == 0
    reader.shutdown(timeout_ms=2_000)


ReadCallable = Callable[[Path], TrackMetadata]


def test_model_reset_discards_queued_requests(
    playlist: PlaylistModel, tmp_path: Path, qtbot: QtBot
) -> None:
    """resetで積んだだけの要求を捨て、古いI/Oが新しいI/Oへ上乗せされない。

    投入済みは上限件数で頭打ちなので、reset を繰り返しても同時に走る読み取りは
    ``max_in_flight`` を超えない。
    """
    paths = [tmp_path / f"曲 {index:03d}.wav" for index in range(200)]
    for path in paths:
        path.write_bytes(b"x")
    read_function = RecordingReader()
    read_function.hold()
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(paths)
    reader.start()
    qtbot.waitUntil(lambda: read_function.started.is_set(), timeout=WAIT_TIMEOUT_MS)
    assert reader.pending_count > 0

    playlist.replace_entries([])

    assert reader.pending_count == 0
    assert reader.in_flight_count <= reader.max_in_flight
    read_function.release()
    reader.shutdown(timeout_ms=2_000)


def test_the_same_entry_is_not_queued_twice(
    playlist: PlaylistModel, tmp_path: Path, audio_files: list[Path]
) -> None:
    """同じ entry を重複して積まない。"""
    del tmp_path
    read_function = RecordingReader()
    read_function.hold()
    # 投入枠を使い切らせて、待ち行列へ積まれる状況を作る。
    reader = MetadataReader(playlist, read_function=read_function, max_threads=1)
    playlist.add_paths(audio_files)
    reader.start()
    pending_before = reader.pending_count

    # 同じ行に対して重ねて要求しても待ち行列は増えない。
    reader._schedule_all()  # pyright: ignore[reportPrivateUsage]

    assert reader.pending_count == pending_before
    read_function.release()
    reader.shutdown(timeout_ms=2_000)

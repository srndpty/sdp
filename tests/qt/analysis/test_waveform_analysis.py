"""fake decode境界で波形解析のthread・token・cache契約を検証する。"""

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.analysis.waveform import WAVEFORM_BUCKET_MS, WaveformData
from sdp.core.analysis.waveform_cache import (
    WAVEFORM_ANALYSIS_VERSION,
    WAVEFORM_FORMAT_VERSION,
    WaveformCache,
    WaveformCacheKey,
)
from sdp.core.playback.controller import PlaybackController
from sdp.services.thread_shutdown import ShutdownOutcome, wait_for_abandoned_threads
from sdp.services.waveform_analysis import (
    FILE_CHANGED_MESSAGE,
    DecodedChunk,
    DecodeFunction,
    WaveformAnalysisService,
)


def make_audio_file(tmp_path: Path, name: str = "音源.wav") -> Path:
    path = tmp_path / name
    path.write_bytes(b"fake audio")
    return path


def sine_chunks(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
    del path
    phase = np.linspace(0, 2 * np.pi, 1_000, endpoint=False, dtype=np.float32)
    for _ in range(5):
        if cancelled():
            return
        yield DecodedChunk(np.sin(phase).astype(np.float32), 1_000)


@pytest.fixture
def controller(qtbot: QtBot) -> PlaybackController:
    del qtbot
    return PlaybackController(FakePlaybackBackend())


def make_service(
    controller: PlaybackController,
    cache_directory: Path,
    decode_function: DecodeFunction = sine_chunks,
) -> WaveformAnalysisService:
    return WaveformAnalysisService(
        controller,
        cache_directory,
        decode_function=decode_function,
        max_cache_bytes=1024 * 1024,
    )


def test_start_is_idempotent_and_build_does_not_start_thread(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """構築だけではthreadを開始せず、startは冪等。"""
    service = make_service(controller, tmp_path / "cache")
    assert not service.is_running
    service.start()
    service.start()
    qtbot.waitUntil(lambda: service.is_running)
    service.shutdown()
    assert not service.is_running


def test_start_analyzes_existing_source_and_publishes_on_gui_thread(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """start時点のsourceを解析し、公開SignalはserviceのGUI threadで受信する。"""
    source = make_audio_file(tmp_path)
    controller.load(source)
    service = make_service(controller, tmp_path / "cache")
    finished = QSignalSpy(service.analysis_finished)
    receiver_threads: list[QThread] = []

    def record_receiver_thread(path: object, token: int, data: object, from_cache: bool) -> None:
        del path, token, data, from_cache
        receiver_threads.append(QThread.currentThread())

    service.analysis_finished.connect(record_receiver_thread)

    service.start()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)

    assert finished.at(0)[0] == source.resolve()
    assert isinstance(finished.at(0)[2], WaveformData)
    assert finished.at(0)[3] is False
    assert receiver_threads == [service.thread()]
    service.shutdown()


def test_source_change_emits_partial_and_finished_then_none_clears(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """source_changedで解析し、部分・完了・解除を順に公開する。"""
    source = make_audio_file(tmp_path)
    service = make_service(controller, tmp_path / "cache")
    partial = QSignalSpy(service.partial_ready)
    finished = QSignalSpy(service.analysis_finished)
    cleared = QSignalSpy(service.analysis_cleared)
    service.start()

    controller.load(source)
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    assert partial.count() >= 1
    before_clear = cleared.count()
    service._on_source_changed(None)  # pyright: ignore[reportPrivateUsage]
    assert cleared.count() == before_clear + 1
    service.shutdown()


def test_cache_hit_skips_decode_and_corruption_redecodes(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """正常cacheはdecodeせず、破損cacheはmissとして再解析する。"""
    source = make_audio_file(tmp_path)
    cache_dir = tmp_path / "cache"
    calls: list[Path] = []

    def counting_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        calls.append(path)
        yield from sine_chunks(path, cancelled)

    first = make_service(controller, cache_dir, counting_decode)
    first_finished = QSignalSpy(first.analysis_finished)
    first.start()
    controller.load(source)
    qtbot.waitUntil(lambda: first_finished.count() == 1, timeout=5_000)
    key = WaveformCacheKey.from_path(source)
    cache_path = cache_dir / key.filename
    qtbot.waitUntil(cache_path.is_file, timeout=5_000)
    first.shutdown()

    second = make_service(controller, cache_dir, counting_decode)
    second_finished = QSignalSpy(second.analysis_finished)
    second.start()
    qtbot.waitUntil(lambda: second_finished.count() == 1, timeout=5_000)
    assert second_finished.at(0)[3] is True
    assert calls == [source.resolve()]
    second.shutdown()

    cache_path.write_bytes(b"broken")
    third = make_service(controller, cache_dir, counting_decode)
    third_finished = QSignalSpy(third.analysis_finished)
    third.start()
    qtbot.waitUntil(lambda: third_finished.count() == 1, timeout=5_000)
    assert third_finished.at(0)[3] is False
    assert calls == [source.resolve(), source.resolve()]
    third.shutdown()


def test_source_change_cancels_old_result_and_cache_write(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """旧tokenのpartial／完了／cache保存を捨て、新sourceだけを公開する。"""
    first = make_audio_file(tmp_path, "A.wav")
    second = make_audio_file(tmp_path, "B.wav")
    entered = threading.Event()
    release = threading.Event()

    def controlled_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        if path == first.resolve():
            entered.set()
            release.wait(timeout=5)
        if cancelled():
            return
        yield DecodedChunk(np.ones(2_000, dtype=np.float32), 1_000)

    cache_dir = tmp_path / "cache"
    service = make_service(controller, cache_dir, controlled_decode)
    partial = QSignalSpy(service.partial_ready)
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    controller.load(first)
    assert entered.wait(timeout=5)

    controller.load(second)
    release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)

    assert all(finished.at(index)[0] == second.resolve() for index in range(finished.count()))
    assert all(partial.at(index)[0] == second.resolve() for index in range(partial.count()))
    assert not (cache_dir / WaveformCacheKey.from_path(first).filename).exists()
    service.shutdown()


def test_source_change_clears_old_waveform_before_new_worker_starts(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """AからBへの変更時はBのworker開始を待たず旧波形を解除する。"""
    first = make_audio_file(tmp_path, "A.wav")
    second = make_audio_file(tmp_path, "B.wav")
    entered = threading.Event()
    release = threading.Event()

    def controlled_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        if path == second.resolve():
            entered.set()
            release.wait(timeout=5)
        if not cancelled():
            yield DecodedChunk(np.ones(2_000, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", controlled_decode)
    cleared = QSignalSpy(service.analysis_cleared)
    started = QSignalSpy(service.analysis_started)
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    controller.load(first)
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    clear_count = cleared.count()
    start_count = started.count()

    controller.load(second)

    assert cleared.count() == clear_count + 1
    assert started.count() == start_count
    assert entered.wait(timeout=5)
    release.set()
    qtbot.waitUntil(lambda: finished.count() == 2, timeout=5_000)
    service.shutdown()


def test_same_path_with_changed_mtime_is_reanalyzed(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """同じpathでもsize／mtimeが変わった再loadは別requestとして解析する。"""
    source = make_audio_file(tmp_path)
    calls: list[Path] = []

    def counting_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        calls.append(path)
        yield from sine_chunks(path, cancelled)

    service = make_service(controller, tmp_path / "cache", counting_decode)
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    controller.load(source)
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    source.write_bytes(b"changed audio size")
    controller.load(source)
    qtbot.waitUntil(lambda: finished.count() == 2, timeout=5_000)
    assert calls == [source.resolve(), source.resolve()]
    service.shutdown()


def test_failure_is_separate_from_playback_and_shutdown_is_idempotent(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """解析失敗は専用Signalだけで通知し、PlaybackControllerを操作しない。"""
    source = make_audio_file(tmp_path)

    def failing_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path, cancelled
        raise OSError("decode failure")
        yield  # pragma: no cover

    backend = controller._backend  # pyright: ignore[reportPrivateUsage]
    service = make_service(controller, tmp_path / "cache", failing_decode)
    failed = QSignalSpy(service.analysis_failed)
    service.start()
    controller.load(source)
    backend.calls.clear()  # type: ignore[attr-defined]
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=5_000)
    assert backend.call_names() == []  # type: ignore[attr-defined]
    service.shutdown()
    service.shutdown()
    controller.load(source)
    qtbot.wait(0)
    assert failed.count() == 1


def test_missing_source_request_fails_without_starting_decode(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """解析直前に欠損したsourceはworkerが失敗Signalへ終端し、decodeを投入しない。

    存在確認はGUIスレッドで行わない（重複I/Oを避ける）。現在のplayback source が
    解析時に欠損していれば、worker側の ``WaveformCacheKey.from_path`` が失敗し、
    decodeを投入せずに ``analysis_failed`` で終端する。
    """
    calls: list[Path] = []

    def counting_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del cancelled
        calls.append(path)
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    source = make_audio_file(tmp_path)
    controller.load(source)
    service = make_service(controller, tmp_path / "cache", counting_decode)
    started = QSignalSpy(service.analysis_started)
    failed = QSignalSpy(service.analysis_failed)
    # 解析対象は現在のplayback source。start前に消し、worker側の欠損検出を誘発する。
    source.unlink()
    service.start()
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=5_000)
    assert started.count() == 1
    assert started.at(0)[:2] == failed.at(0)[:2]
    assert calls == []
    service.shutdown()


def test_file_removed_during_read_emits_failure(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """読取開始後のファイル削除も現在requestの解析失敗として通知する。"""
    source = make_audio_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def removing_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del cancelled
        entered.set()
        release.wait(timeout=5)
        path.read_bytes()
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", removing_decode)
    failed = QSignalSpy(service.analysis_failed)
    service.start()
    controller.load(source)
    assert entered.wait(timeout=5)
    source.unlink()
    release.set()
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=5_000)
    assert failed.at(0)[0] == source.resolve()
    service.shutdown()


def test_file_changed_during_analysis_emits_terminal_failure(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """現在sourceの内容変更はsilent破棄せず、失敗で要求を終了する。"""
    source = make_audio_file(tmp_path)
    initial_key = WaveformCacheKey.from_path(source)
    entered = threading.Event()
    release = threading.Event()

    def controlled_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path
        entered.set()
        release.wait(timeout=5)
        if not cancelled():
            yield DecodedChunk(np.ones(2_000, dtype=np.float32), 1_000)

    cache_dir = tmp_path / "cache"
    service = make_service(controller, cache_dir, controlled_decode)
    finished = QSignalSpy(service.analysis_finished)
    failed = QSignalSpy(service.analysis_failed)
    service.start()
    controller.load(source)
    assert entered.wait(timeout=5)
    source.write_bytes(b"changed audio with a different size")
    release.set()
    qtbot.waitUntil(lambda: failed.count() == 1, timeout=5_000)

    assert finished.count() == 0
    assert failed.at(0)[2] == FILE_CHANGED_MESSAGE
    assert not (cache_dir / initial_key.filename).exists()
    assert service._request is None  # pyright: ignore[reportPrivateUsage]
    service.shutdown()


def test_file_identity_rechecks_run_only_on_worker_thread(
    controller: PlaybackController,
    tmp_path: Path,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """キャッシュキーの生成と再確認を、いずれもGUI threadで実行しない。

    内容fingerprintの算出は最大192KiBの読み取りを伴う。GUIスレッドで作ると
    NAS・休止ディスク・クラウドプレースホルダーでsource切替のたびにUIが止まる。
    """
    source = make_audio_file(tmp_path)
    threads: list[QThread] = []
    original_from_path = WaveformCacheKey.from_path

    def recording_from_path(
        cls: type[WaveformCacheKey],
        path: Path,
        *,
        analysis_version: int = WAVEFORM_ANALYSIS_VERSION,
        bucket_ms: int = WAVEFORM_BUCKET_MS,
        format_version: int = WAVEFORM_FORMAT_VERSION,
    ) -> WaveformCacheKey:
        del cls
        threads.append(QThread.currentThread())
        return original_from_path(
            path,
            analysis_version=analysis_version,
            bucket_ms=bucket_ms,
            format_version=format_version,
        )

    monkeypatch.setattr(WaveformCacheKey, "from_path", classmethod(recording_from_path))
    service = make_service(controller, tmp_path / "cache")
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    controller.load(source)
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)

    assert threads
    assert all(
        thread is service._thread  # pyright: ignore[reportPrivateUsage]
        for thread in threads
    )
    assert service.thread() not in threads
    service.shutdown()


def test_cache_pruning_runs_on_worker_thread(
    controller: PlaybackController,
    tmp_path: Path,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache保存後のLRU走査をGUI threadへ持ち込まない。"""
    source = make_audio_file(tmp_path)
    threads: list[QThread] = []
    original_prune = WaveformCache.prune

    def record_prune(self: WaveformCache, *, protected: Path | None = None) -> None:
        threads.append(QThread.currentThread())
        original_prune(self, protected=protected)

    monkeypatch.setattr(WaveformCache, "prune", record_prune)
    service = make_service(controller, tmp_path / "cache")
    service.start()
    controller.load(source)
    qtbot.waitUntil(lambda: len(threads) == 1, timeout=5_000)
    assert threads == [service._thread]  # pyright: ignore[reportPrivateUsage]
    assert threads[0] is not service.thread()
    service.shutdown()


def test_service_can_be_deleted_after_shutdown(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """shutdown済みならQObject削除後のqueued結果でもクラッシュしない。"""
    service = make_service(controller, tmp_path / "cache")
    service.start()
    service.shutdown()
    service.deleteLater()
    qtbot.waitUntil(lambda: not isValid(service))
    assert not isValid(service)


def test_shutdown_waits_for_blocked_worker_after_timeout(
    controller: PlaybackController,
    tmp_path: Path,
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """短いtimeoutを超えても、hard上限までにworkerが戻れば通常どおり終了する。"""
    source = make_audio_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path, cancelled
        entered.set()
        release.wait(timeout=5)
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", blocked_decode)
    service.start()
    controller.load(source)
    assert entered.wait(timeout=5)
    releaser = threading.Timer(0.05, release.set)
    releaser.start()

    with caplog.at_level("WARNING"):
        outcome = service.shutdown(timeout_ms=1)
    releaser.join(timeout=1)

    assert outcome is ShutdownOutcome.STOPPED
    assert not service._thread.isRunning()  # pyright: ignore[reportPrivateUsage]
    assert "もう少し待機します" in caplog.text
    service.deleteLater()
    qtbot.waitUntil(lambda: not isValid(service))
    assert not isValid(service)


def test_cancelled_tokens_are_reclaimed_after_worker_handles_cancel(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """曲切替で論理cancelしたtokenをworker終端後に回収する。"""
    first = make_audio_file(tmp_path, "A.wav")
    second = make_audio_file(tmp_path, "B.wav")
    entered = threading.Event()
    release = threading.Event()

    def controlled_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        if path == first.resolve():
            entered.set()
            release.wait(timeout=5)
        if not cancelled():
            yield DecodedChunk(np.ones(2_000, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", controlled_decode)
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    controller.load(first)
    assert entered.wait(timeout=5)
    controller.load(second)
    assert service._cancellations.count() == 1  # pyright: ignore[reportPrivateUsage]
    release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    qtbot.waitUntil(
        lambda: service._cancellations.count() == 0,  # pyright: ignore[reportPrivateUsage]
        timeout=5_000,
    )
    service.shutdown()


def test_gui_heartbeat_runs_while_worker_is_blocked(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """decodeと縮約がworkerで待機中もGUI event loopは処理される。"""
    source = make_audio_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path
        entered.set()
        release.wait(timeout=5)
        minute = np.zeros(60_000, dtype=np.float32)
        for _ in range(60):
            if cancelled():
                return
            yield DecodedChunk(minute, 1_000)

    service = make_service(controller, tmp_path / "cache", blocked_decode)
    finished = QSignalSpy(service.analysis_finished)
    partial = QSignalSpy(service.partial_ready)
    ticks: list[int] = []
    timer = QTimer()
    timer.setInterval(0)
    timer.timeout.connect(lambda: ticks.append(len(ticks)))
    timer.start()
    service.start()
    controller.load(source)
    assert entered.wait(timeout=5)
    qtbot.waitUntil(lambda: len(ticks) >= 3, timeout=2_000)
    ticks_before_reduction = len(ticks)
    release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    data = finished.at(0)[2]
    assert isinstance(data, WaveformData)
    assert data.minimum.size == 180_000
    assert data.minimum.nbytes + data.maximum.nbytes == 1_440_000
    assert partial.count() >= 2
    assert len(ticks) > ticks_before_reduction
    timer.stop()
    service.shutdown()


def test_shutdown_does_not_rewrite_an_abandoned_result_as_stopped(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """冪等なshutdownでも、初回のABANDONEDを黙ってSTOPPEDへ書き換えない。"""
    del qtbot
    source = make_audio_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocked_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path, cancelled
        entered.set()
        release.wait(timeout=5)
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", blocked_decode)
    service.start()
    controller.load(source)
    assert entered.wait(timeout=5)

    # workerが戻らない状態で放棄させる。
    assert service.shutdown(timeout_ms=0, hard_timeout_ms=0) is ShutdownOutcome.ABANDONED
    assert service.shutdown() is ShutdownOutcome.ABANDONED

    release.set()
    assert wait_for_abandoned_threads()

    # 実際に終わったあとは STOPPED へ更新してよい。
    assert service.shutdown() is ShutdownOutcome.STOPPED


def test_cache_is_saved_even_when_the_next_source_starts_immediately(
    controller: PlaybackController, tmp_path: Path, qtbot: QtBot
) -> None:
    """Aの完了直後にBをloadしても、Aのキャッシュが保存される。

    保存要求がworkerの現在状態（現在request・現在key）を参照していると、
    GUIを1往復するあいだにBの解析が始まった時点でAの結果を捨ててしまう。
    """
    first = make_audio_file(tmp_path, "A.wav")
    second = make_audio_file(tmp_path, "B.wav")
    cache_dir = tmp_path / "cache"
    first_key = WaveformCacheKey.from_path(first)

    def decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del path, cancelled
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    service = make_service(controller, cache_dir, decode)
    finished = QSignalSpy(service.analysis_finished)
    service.start()
    try:
        controller.load(first)
        qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
        # analysis_finished を受けた直後（保存要求がworkerへ届く前）に次の曲を読む。
        controller.load(second)

        qtbot.waitUntil(
            lambda: (cache_dir / first_key.filename).is_file(),
            timeout=5_000,
        )
    finally:
        service.shutdown()

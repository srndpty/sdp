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
from sdp.core.analysis.waveform import WaveformData
from sdp.core.analysis.waveform_cache import WaveformCache, WaveformCacheKey
from sdp.core.playback.controller import PlaybackController
from sdp.services.waveform_analysis import (
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
    controller: PlaybackController, tmp_path: Path
) -> None:
    """解析直前に欠損したpathは専用失敗Signalとなり、decodeを投入しない。"""
    calls: list[Path] = []

    def counting_decode(path: Path, cancelled: Callable[[], bool]) -> Iterator[DecodedChunk]:
        del cancelled
        calls.append(path)
        yield DecodedChunk(np.zeros(20, dtype=np.float32), 1_000)

    service = make_service(controller, tmp_path / "cache", counting_decode)
    failed = QSignalSpy(service.analysis_failed)
    service.start()
    missing = tmp_path / "missing.wav"
    service._on_source_changed(missing)  # pyright: ignore[reportPrivateUsage]
    assert failed.count() == 1
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
    release.set()
    qtbot.waitUntil(lambda: finished.count() == 1, timeout=5_000)
    data = finished.at(0)[2]
    assert isinstance(data, WaveformData)
    assert data.minimum.size == 180_000
    assert data.minimum.nbytes + data.maximum.nbytes == 1_440_000
    assert partial.count() >= 2
    timer.stop()
    service.shutdown()

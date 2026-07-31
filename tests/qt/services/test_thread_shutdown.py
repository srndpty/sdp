"""終了時のthread待機が、上限を超えても必ず制御を返すことを検証する。

``timeout`` を名乗るAPIが無期限待機へ移ると、「閉じるを押したのにプロセスが
残る」状態になる。一方で実行中QThreadを親ごと破棄すると異常終了するため、
上限超過時は放棄して制御だけ返す設計になっている。
"""

import threading
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QThread
from pytestqt.qtbot import QtBot

from sdp.services.thread_shutdown import ShutdownOutcome, abandoned_thread_count, stop_thread

WAIT_TIMEOUT_MS = 5_000


class _BlockingThread(QThread):
    """解放されるまで戻らないthread（協調的キャンセルを持たない処理の模擬）。"""

    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self._release = release
        self.entered = threading.Event()

    def run(self) -> None:
        self.entered.set()
        self._release.wait(timeout=10)


@pytest.fixture
def release() -> Iterator[threading.Event]:
    event = threading.Event()
    yield event
    event.set()


def test_finished_thread_reports_stopped(qtbot: QtBot) -> None:
    """既に終わっているthreadは待たずにSTOPPED。"""
    del qtbot
    thread = QThread()

    assert stop_thread(thread, label="テスト") is ShutdownOutcome.STOPPED


def test_thread_that_returns_in_time_reports_stopped(
    release: threading.Event, qtbot: QtBot
) -> None:
    """上限内に戻ればSTOPPEDで、放棄しない。"""
    del qtbot
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    before = abandoned_thread_count()
    threading.Timer(0.05, release.set).start()

    outcome = stop_thread(
        thread, label="テスト", soft_timeout_ms=10, hard_timeout_ms=WAIT_TIMEOUT_MS
    )

    assert outcome is ShutdownOutcome.STOPPED
    assert abandoned_thread_count() == before
    assert not thread.isRunning()


def test_blocked_thread_is_abandoned_instead_of_waiting_forever(
    release: threading.Event, qtbot: QtBot
) -> None:
    """戻らないthreadは放棄し、呼び出し側へ制御を返す。"""
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    before = abandoned_thread_count()

    outcome = stop_thread(thread, label="テスト", soft_timeout_ms=1, hard_timeout_ms=20)

    assert outcome is ShutdownOutcome.ABANDONED
    assert thread.isRunning()
    # 親から切り離して保持しているため、QObject破棄で異常終了しない。
    assert thread.parent() is None
    assert abandoned_thread_count() == before + 1

    release.set()
    qtbot.waitUntil(lambda: abandoned_thread_count() == before, timeout=WAIT_TIMEOUT_MS)
    assert thread.wait(WAIT_TIMEOUT_MS)


def test_abandoned_thread_releases_itself_after_returning(
    release: threading.Event, qtbot: QtBot
) -> None:
    """放棄したthreadは、協調的な処理が戻った時点で参照を手放す。"""
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    stop_thread(thread, label="テスト", soft_timeout_ms=1, hard_timeout_ms=5)
    assert abandoned_thread_count() == 1

    release.set()

    qtbot.waitUntil(lambda: abandoned_thread_count() == 0, timeout=WAIT_TIMEOUT_MS)
    assert thread.wait(WAIT_TIMEOUT_MS)

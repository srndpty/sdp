"""終了時のthread待機が、上限を超えても必ず制御を返すことを検証する。

``timeout`` を名乗るAPIが無期限待機へ移ると、「閉じるを押したのにプロセスが
残る」状態になる。一方で実行中QThreadを親ごと破棄すると異常終了するため、
上限超過時は放棄して制御だけ返す設計になっている。
"""

import gc
import threading
import weakref
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject, QThread
from pytestqt.qtbot import QtBot

from sdp.services.thread_shutdown import (
    ShutdownOutcome,
    abandoned_thread_count,
    stop_thread,
    wait_for_abandoned_threads,
)

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
    # 回収はQtのevent loopに依存しない（実際の終了処理はexec()後に走るため）。
    assert wait_for_abandoned_threads()
    assert abandoned_thread_count() == before
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

    assert wait_for_abandoned_threads()
    assert thread.wait(WAIT_TIMEOUT_MS)


def test_abandoned_thread_is_reaped_without_running_a_qt_event_loop(
    release: threading.Event,
) -> None:
    """Qtのevent loopを回さなくても回収される。

    実際の終了処理は ``app.exec()`` が戻ったあとに走るため、queued connectionへ
    回収を任せると配送されない。ここではevent loopを一切回さずに確かめる。
    """
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    before = abandoned_thread_count()

    assert stop_thread(thread, label="テスト", soft_timeout_ms=1, hard_timeout_ms=5) is (
        ShutdownOutcome.ABANDONED
    )
    assert abandoned_thread_count() == before + 1

    release.set()

    assert wait_for_abandoned_threads()
    assert abandoned_thread_count() == before


def test_thread_finishing_right_after_the_hard_timeout_is_still_reaped(
    release: threading.Event,
) -> None:
    """hard timeout直後に終了しても取りこぼさない。

    「wait()がFalseを返した直後にthreadが終わる」順序では、あとから接続する
    signalは届かない。回収はsignalではなくwait()で行う。
    """
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    before = abandoned_thread_count()

    # 放棄の直前に解放し、finishedのemitと登録が競合する状況を作る。
    release.set()
    stop_thread(thread, label="テスト", soft_timeout_ms=0, hard_timeout_ms=0)

    assert wait_for_abandoned_threads()
    assert abandoned_thread_count() == before


def test_keepalive_objects_survive_until_the_thread_returns(
    release: threading.Event,
) -> None:
    """放棄したthreadが戻るまで、渡したオブジェクトを解放しない。

    worker QObjectのPython参照が先に切れると、thread内でまだ動いている
    コードの足元が崩れる。
    """
    thread = _BlockingThread(release)
    thread.start()
    assert thread.entered.wait(timeout=5)
    keepalive = QObject()
    reference = weakref.ref(keepalive)

    stop_thread(
        thread, label="テスト", soft_timeout_ms=1, hard_timeout_ms=5, keepalive=(keepalive,)
    )
    del keepalive
    gc.collect()

    # threadが動いているあいだは生きている。
    assert reference() is not None

    release.set()
    assert wait_for_abandoned_threads()
    gc.collect()
    assert reference() is None

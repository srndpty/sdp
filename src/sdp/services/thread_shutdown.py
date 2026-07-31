"""終了時のthread待機を、上限つきで打ち切るための共通処理。

``timeout`` を名乗るAPIが、超過後に無期限待機へ移ると、呼び出し側は制御を
取り戻せない。実際には「閉じるを押したのにプロセスが残る」形で現れる。

一方で、実行中の :class:`QThread` を親ごと破棄すると
``QThread: Destroyed while thread is still running`` で異常終了する。
そのため、上限を超えた場合は **待ち続けるのでも即座に捨てるのでもなく**、
threadと関連オブジェクトをここへ引き取り、制御だけを返す。

回収は **Qtのevent loopに依存しない**。アプリの終了処理は ``app.exec()`` が
戻ったあと、つまりmainのevent loopが止まってから走るため、queued connectionへ
回収を任せると配送されない。専用のreaper threadが :meth:`QThread.wait` で
待ち、戻ったところで参照を解放する。

引き取るのはthreadだけではない。worker QObjectのPython参照が先に切れると、
thread内でまだ動いているコードの足元が崩れる。呼び出し側は ``keepalive`` で
生かしておくべきオブジェクトを渡す。

強制終了（``QThread.terminate``）は使わない。保存中のユーザーデータが壊れうるため。
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from PySide6.QtCore import QThread

_logger = logging.getLogger(__name__)

DEFAULT_SOFT_TIMEOUT_MS = 3_000
"""通常はこの時間内に終わる、という目安。超えたら警告する。"""

DEFAULT_HARD_TIMEOUT_MS = 10_000
"""ここまで待っても戻らなければ放棄して制御を返す上限。"""

_REAPER_POLL_MS = 200
"""reaper threadが1回のwaitで待つ時間。停止要求へ反応できる粒度にする。"""


class ShutdownOutcome(Enum):
    """終了待機の結果。"""

    STOPPED = auto()
    """時間内にthreadが終了した。"""

    ABANDONED = auto()
    """上限を超えたため放棄して制御を返した（threadはまだ動いている）。"""


@dataclass(slots=True)
class _AbandonedThread:
    """放棄したthreadと、一緒に生かしておくオブジェクト。"""

    thread: QThread
    label: str
    keepalive: tuple[object, ...] = field(default_factory=tuple)


class _Reaper:
    """放棄したthreadをevent loop非依存で見張り、戻ったら参照を解放する。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[_AbandonedThread] = []
        self._worker: threading.Thread | None = None

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def add(self, entry: _AbandonedThread) -> None:
        """引き取って見張りを開始する。"""
        # 親のQObjectと一緒に破棄されると異常終了するため、所有権をここへ移す。
        entry.thread.setParent(None)
        with self._lock:
            self._entries.append(entry)
            self._start_worker_locked()

    def wait_for_empty(self, timeout_s: float) -> bool:
        """すべて回収されるまで待つ（テストと診断用）。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while self.count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.count == 0

    def _start_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        # daemon threadにして、プロセス終了を妨げない。ここで待ち続けても
        # アプリの終了自体は進む（放棄の目的はまさにそれ）。
        self._worker = threading.Thread(target=self._run, name="sdp-thread-reaper", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                pending = list(self._entries)
                if not pending:
                    # 空判定と worker の解除を同じ critical section で行う。
                    # add() 側も同じlockを取るため、取りこぼしにならない。
                    self._worker = None
                    return
            for entry in pending:
                # wait() 自体が待機になるので、追加のsleepは要らない。
                # hard timeout 直後に終了していた場合も、ここで即座に回収できる。
                if entry.thread.wait(_REAPER_POLL_MS):
                    self._release(entry)

    def _release(self, entry: _AbandonedThread) -> None:
        with self._lock:
            if entry not in self._entries:
                return
            self._entries.remove(entry)
        _logger.info("放棄した%sが終了したため参照を解放しました", entry.label)


_reaper = _Reaper()


def abandoned_thread_count() -> int:
    """まだ戻っていない放棄済みthreadの数（診断とテスト用）。"""
    return _reaper.count


def wait_for_abandoned_threads(timeout_s: float = 5.0) -> bool:
    """放棄済みthreadがすべて回収されるまで待つ（テストと診断用）。"""
    return _reaper.wait_for_empty(timeout_s)


def stop_thread(
    thread: QThread,
    *,
    label: str,
    soft_timeout_ms: int = DEFAULT_SOFT_TIMEOUT_MS,
    hard_timeout_ms: int = DEFAULT_HARD_TIMEOUT_MS,
    keepalive: tuple[object, ...] = (),
) -> ShutdownOutcome:
    """threadの終了を上限つきで待つ。**必ず呼び出し側へ制御を返す。**

    :param label: ログへ出す対象名。
    :param soft_timeout_ms: これを超えたら警告する（まだ待つ）。
    :param hard_timeout_ms: これを超えたら放棄して戻る。
    :param keepalive: 放棄する場合に一緒に生かしておくオブジェクト
        （worker QObjectなど、thread内でまだ使われているもの）。
    """
    # isRunning() が False でも必ず wait() する。run() から戻った直後の thread は
    # isRunning() が False になり得るが、OSスレッドの後始末はまだ終わっていない。
    # join せずに戻ると、直後のQObject破棄でheap corruptionになる（実測）。
    if thread.wait(max(0, soft_timeout_ms)):
        return ShutdownOutcome.STOPPED

    _logger.warning("%sが%dms以内に終了しません。もう少し待機します。", label, soft_timeout_ms)
    remaining = max(0, hard_timeout_ms - soft_timeout_ms)
    if thread.wait(remaining):
        return ShutdownOutcome.STOPPED

    _logger.error(
        "%sが%dms以内に終了しないため、待機を打ち切って終了処理を続けます。", label, hard_timeout_ms
    )
    _reaper.add(_AbandonedThread(thread=thread, label=label, keepalive=keepalive))
    return ShutdownOutcome.ABANDONED

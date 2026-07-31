"""終了時のthread待機を、上限つきで打ち切るための共通処理。

``timeout`` を名乗るAPIが、超過後に無期限待機へ移ると、呼び出し側は制御を
取り戻せない。実際には「閉じるを押したのにプロセスが残る」形で現れる。

一方で、実行中の :class:`QThread` を親ごと破棄すると
``QThread: Destroyed while thread is still running`` で異常終了する。
そのため、上限を超えた場合は **待ち続けるのでも即座に捨てるのでもなく**、
threadを親から切り離してモジュールスコープで保持し、制御だけを返す。
放棄したthreadは、協調的な処理が戻った時点で自分を解放する。

強制終了（``QThread.terminate``）は使わない。保存中のユーザーデータが壊れうるため。
"""

import logging
from enum import Enum, auto

from PySide6.QtCore import QObject, Qt, QThread, Slot

_logger = logging.getLogger(__name__)

DEFAULT_SOFT_TIMEOUT_MS = 3_000
"""通常はこの時間内に終わる、という目安。超えたら警告する。"""

DEFAULT_HARD_TIMEOUT_MS = 10_000
"""ここまで待っても戻らなければ放棄して制御を返す上限。"""


class _AbandonRegistry(QObject):
    """放棄したthreadの参照を持ち続け、戻ったところで解放する。

    Pythonの参照が切れるとPySide6が実行中のQThreadを破棄してしまうため、
    ここで保持する。``finished`` はworker thread側から出るので、
    **queued接続でこのQObjectのthread（GUI thread）へ渡してから**リストを触る。
    """

    def __init__(self) -> None:
        super().__init__()
        self._threads: list[QThread] = []

    @property
    def count(self) -> int:
        return len(self._threads)

    def add(self, thread: QThread) -> None:
        thread.setParent(None)
        self._threads.append(thread)
        thread.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def _on_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread not in self._threads:
            return
        # OSスレッドの後始末まで見届けてから参照を手放す。join せずに解放すると、
        # 直後のGCによる破棄で access violation になる（実測）。
        thread.wait(DEFAULT_SOFT_TIMEOUT_MS)
        # 参照を手放すだけにする。setParent(None) でC++側の所有権はPythonへ
        # 戻っているため、ここで deleteLater() を呼ぶと後のGCと二重解放になる
        # （実測でheap corruptionになった）。終了済みなのでGCでの破棄は安全。
        self._threads.remove(thread)


_registry: _AbandonRegistry | None = None
"""放棄したthreadの保管庫。**import時には作らない。**

QApplicationより前に生成したQObjectをプロセス全体で持ち続けると、
アプリケーションの寿命と噛み合わずに不安定になる。初めて必要になった時点で作る。
"""


def _get_registry() -> _AbandonRegistry:
    global _registry
    if _registry is None:
        _registry = _AbandonRegistry()
    return _registry


class ShutdownOutcome(Enum):
    """終了待機の結果。"""

    STOPPED = auto()
    """時間内にthreadが終了した。"""

    ABANDONED = auto()
    """上限を超えたため放棄して制御を返した（threadはまだ動いている）。"""


def abandoned_thread_count() -> int:
    """まだ戻っていない放棄済みthreadの数（診断とテスト用）。"""
    return 0 if _registry is None else _registry.count


def stop_thread(
    thread: QThread,
    *,
    label: str,
    soft_timeout_ms: int = DEFAULT_SOFT_TIMEOUT_MS,
    hard_timeout_ms: int = DEFAULT_HARD_TIMEOUT_MS,
) -> ShutdownOutcome:
    """threadの終了を上限つきで待つ。**必ず呼び出し側へ制御を返す。**

    :param label: ログへ出す対象名。
    :param soft_timeout_ms: これを超えたら警告する（まだ待つ）。
    :param hard_timeout_ms: これを超えたら放棄して戻る。
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
    _abandon(thread)
    return ShutdownOutcome.ABANDONED


def _abandon(thread: QThread) -> None:
    """実行中threadを親から切り離し、破棄されないよう保持する。

    親のQObjectと一緒に破棄されると
    ``QThread: Destroyed while thread is still running`` で異常終了するため、
    所有権をここへ移す。threadが戻ったら解放する。
    """
    _get_registry().add(thread)

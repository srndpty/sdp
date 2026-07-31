"""プレイリストのファイル状態を、GUIスレッド外で少しずつ確認するサービス。

エントリ生成時にファイルシステムへ触れない代わり（:class:`FileStatus.UNKNOWN`）、
ここが背景で ``is_file()`` を実行して状態を確定させる。1000曲の復元や
大量のD&Dでも、GUIスレッドに件数ぶんの stat が積み上がらないようにするのが目的。

- 1バッチずつ直列に実行する。同時に何本も走らせない。
- Modelの世代（reset・行の増減）が変わったら古い結果を捨てる。
- 結果の反映はGUIスレッドで行う（Modelを他スレッドから触らない）。
- 未確認を欠損として扱わない。再生直前の個別確認は従来どおり同期で行う。
"""

import logging
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from sdp.core.playlist.entry import FileStatus, probe_file_status
from sdp.core.playlist.model import PlaylistModel

_logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 64
"""1回のバッチで確認する件数。応答性と往復回数の折り合い。"""

DEFAULT_SHUTDOWN_WAIT_MS = 3_000
"""終了時に実行中バッチを待つ上限。**超えても無期限待機へ移らない。**

1バッチは数十件の ``is_file()`` だけなので通常は即座に終わる。切断されたNASなどで
戻らない場合は、待たずに諦めて終了処理を進める（結果は世代不一致で捨てられる）。
"""


class _WorkerSignals(QObject):
    """worker threadからGUIスレッドへ結果を渡すためのSignal所有者。"""

    finished = Signal(int, object)


class _ProbeTask(QRunnable):
    """1バッチぶんのファイル状態を調べる（Modelには触れない）。"""

    def __init__(
        self,
        generation: int,
        targets: tuple[tuple[str, Path], ...],
        signals: _WorkerSignals,
        done: Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._targets = targets
        self._signals = signals
        self._done = done

    def run(self) -> None:
        try:
            results: dict[str, FileStatus] = {}
            for entry_id, path in self._targets:
                try:
                    results[entry_id] = probe_file_status(path)
                except OSError:
                    # 切断されたドライブや権限エラー。欠損として扱い、再確認は
                    # 再生直前の同期確認に任せる（ここで例外を伝播させない）。
                    _logger.debug("ファイル状態を確認できません: %s", path, exc_info=True)
                    results[entry_id] = FileStatus.MISSING
            try:
                self._signals.finished.emit(self._generation, results)
            except RuntimeError:
                # 所有者が先に破棄された（shutdownを経ないアプリ終了・テスト等）。
                # 結果を渡す相手がいないだけなので、警告にせず捨てる。
                _logger.debug("ファイル状態の通知先が既に破棄されています")
        finally:
            # 例外で抜けても shutdown 側の待機を必ず解く。
            self._done.set()


class PlaylistFileStatusChecker(QObject):
    """未確認エントリのファイル状態を、バッチ単位で背景確認する。"""

    def __init__(
        self,
        playlist: PlaylistModel,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        pool: QThreadPool | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if batch_size <= 0:
            raise ValueError("batch_sizeは1以上にしてください")
        self._playlist = playlist
        self._batch_size = batch_size
        self._pool = QThreadPool.globalInstance() if pool is None else pool
        self._signals = _WorkerSignals(self)
        self._signals.finished.connect(self._on_batch_finished)
        self._generation = 0
        self._running = False
        self._shutdown = False
        self._shutdown_stopped: bool | None = None
        self._batch_done: Event | None = None

        playlist.rowsInserted.connect(self._on_rows_inserted)
        playlist.modelReset.connect(self._on_model_reset)
        self.schedule()

    @property
    def is_running(self) -> bool:
        """バッチを実行中かどうか（テストと診断用）。"""
        return self._running

    def schedule(self) -> None:
        """未確認エントリがあれば次のバッチを開始する（実行中なら何もしない）。"""
        if self._shutdown or self._running:
            return
        targets = self._playlist.unchecked_entries(self._batch_size)
        if not targets:
            return
        self._running = True
        done = Event()
        self._batch_done = done
        self._pool.start(_ProbeTask(self._generation, targets, self._signals, done))

    def run_pending_now(self) -> int:
        """未確認エントリを同期で確定させ、更新した件数を返す。

        終了時と、背景実行を待てないテストのための同期経路。
        """
        statuses = {
            entry_id: probe_file_status(path)
            for entry_id, path in self._playlist.unchecked_entries(len(self._playlist.entries()))
        }
        return self._playlist.apply_file_statuses(statuses)

    def shutdown(self, *, wait_ms: int = DEFAULT_SHUTDOWN_WAIT_MS) -> bool:
        """以後の結果を捨て、新しいバッチを開始しない（冪等）。

        実行中バッチの完了は ``wait_ms`` を上限に待つ。**上限を超えても
        無期限待機へは移らない**（終了操作が返らなくなるのを避ける）。
        時間内に止まれば ``True``。

        冪等だが、**初回の失敗を成功へ書き換えない**。2回目以降は初回の結果を
        返し、その後にバッチが実際に終わっていた場合だけ ``True`` へ更新する。

        既知の制限: 本checkerは ``QThreadPool``（既定で global instance）で
        runnableを走らせるため、個々のrunnableを切り離せない。``wait_ms`` は
        **このメソッドの待機上限であって、プロセス終了の上限ではない**。切断された
        NASなどで ``is_file()`` が戻らない場合、QApplication・global poolの破棄時に
        ブロックしうる（``MetadataReader`` と同じ制約。docs/architecture.md 参照）。
        """
        if self._shutdown:
            return self._recheck_shutdown_outcome()
        self._shutdown = True
        self._generation += 1
        done = self._batch_done
        stopped = done is None or done.wait(max(0, wait_ms) / 1_000.0)
        if not stopped:
            _logger.warning("ファイル状態確認のバッチが%dms以内に終わりませんでした", wait_ms)
        self._shutdown_stopped = stopped
        return stopped

    def _recheck_shutdown_outcome(self) -> bool:
        """初回の結果を返す。遅れて終わっていれば成功へ更新する。"""
        if self._shutdown_stopped:
            return True
        done = self._batch_done
        if done is not None and done.is_set():
            self._shutdown_stopped = True
        return bool(self._shutdown_stopped)

    def _on_rows_inserted(self, *_arguments: object) -> None:
        self.schedule()

    def _on_model_reset(self) -> None:
        # 復元などで行が総入れ替えされた。進行中バッチの結果は entry_id が
        # 一致しなければ無視されるが、世代を進めて明示的に捨てる。
        self._generation += 1
        self.schedule()

    def _on_batch_finished(self, generation: int, results: object) -> None:
        self._running = False
        if self._shutdown:
            return
        # 結果を捨てる場合でも必ず次を予約する。捨てて return すると、
        # 「バッチ実行中に modelReset が起きた」ときに誰も再開せず、
        # 以後のUNKNOWNが永久に未確認のまま残る。
        if generation == self._generation and isinstance(results, dict):
            self._playlist.apply_file_statuses(cast("dict[str, FileStatus]", results))
        self.schedule()

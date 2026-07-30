"""Mutagen によるメタデータ読み取りと、その非同期実行。

読み取りはファイル I/O なので **GUI スレッドでは行わない**。
純粋関数 :func:`read_track_metadata` をワーカースレッドで実行し、
結果は GUI スレッドで PlaylistModel へ反映する。

古い結果を誤って反映しないよう、要求ごとに単調増加のトークンを付け、
反映前に entry_id・path・状態を照合する。パスから別 entry へ流用しない。
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import mutagen
from mutagen import MutagenError
from PySide6.QtCore import QModelIndex, QObject, QRunnable, QThread, QThreadPool, Signal

from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playlist.model import FILE_STATUS_ROLE, PlaylistModel

_logger = logging.getLogger(__name__)

ARTIST_SEPARATOR = "/"
"""複数アーティストを 1 つの文字列へ結合する区切り。1 件なら付けない。"""

MAX_WORKER_THREADS = 4
"""メタデータ読み取りの最大並列数の上限。ファイル I/O なので過度に増やさない。"""

SHUTDOWN_TIMEOUT_MS = 3_000


class MetadataReadError(Exception):
    """メタデータを読み取れなかった。ユーザー向け文言はここへ入れない。"""


def read_track_metadata(path: Path) -> TrackMetadata:
    """1 ファイルからメタデータを読み取る純粋関数（Qt に依存しない）。

    未対応形式・破損・権限不足などは :class:`MetadataReadError` にまとめる。
    Mutagen のオブジェクトはこの関数の外へ出さない。
    """
    try:
        audio = _open_audio(path)
    except MutagenError as error:
        raise MetadataReadError(f"メタデータを解析できません: {path}") from error
    except OSError as error:
        raise MetadataReadError(f"ファイルを読み取れません: {path}") from error

    if audio is None:
        raise MetadataReadError(f"未対応の形式です: {path}")

    # 属性取得や抽出ロジックの予期しない例外は、プログラミングエラーとして
    # ワーカー側の logger.exception へ到達させる。ここで通常の読取失敗へ
    # 変換すると traceback が失われ、不具合を診断できなくなる。
    tags: object = getattr(audio, "tags", None)
    info: object = getattr(audio, "info", None)
    return TrackMetadata(
        title=_first_text(tags, "title"),
        artist=_joined_text(tags, "artist"),
        album=_first_text(tags, "album"),
        duration_ms=_duration_ms(info),
    )


def _open_audio(path: Path) -> object:
    """Mutagen で開く。Mutagen の戻り型は形式ごとの共用体で一部が Unknown のため、
    型の曖昧さをこの 1 か所へ閉じ込め、以降は ``object`` から段階的に検証する。

    ``easy=True`` で形式ごとのタグキーを共通名（title / artist / album）へ寄せる。
    """
    return mutagen.File(  # pyright: ignore[reportUnknownMemberType, reportPrivateImportUsage]
        path, easy=True
    )


def _tag_values(tags: object, key: str) -> list[str]:
    """easy tags から指定キーの文字列値だけを取り出す。

    easy tags の通常契約は「キー → 文字列のリスト」。それ以外の型が来た場合は、
    無理に文字列化して意味不明な表示を作らず無視する。
    """
    if tags is None:
        return []
    try:
        raw = cast("Any", tags).get(key)
    except (KeyError, TypeError, ValueError):
        return []
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [value.strip() for value in cast("list[Any]", raw) if isinstance(value, str)]


def _first_text(tags: object, key: str) -> str | None:
    """最初の非空値。すべて空なら ``None``。"""
    for value in _tag_values(tags, key):
        if value:
            return value
    return None


def _joined_text(tags: object, key: str) -> str | None:
    """非空値を順序どおり結合する。1 件なら区切りを付けない。"""
    values = [value for value in _tag_values(tags, key) if value]
    if not values:
        return None
    return ARTIST_SEPARATOR.join(values)


def _duration_ms(info: object) -> int | None:
    """``info.length``（秒）をミリ秒へ変換する。

    取得できない・有限でない・負の場合は ``None``。長さが分からないだけで、
    取得できたタグまで捨てない。
    """
    length = getattr(info, "length", None)
    if not isinstance(length, (int, float)) or isinstance(length, bool):
        return None
    seconds = float(length)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return round(seconds * 1000)


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    """1 件の読み取り要求。ワーカーへ渡す不変値。"""

    entry_id: str
    path: Path
    token: int


@dataclass(frozen=True, slots=True)
class MetadataResult:
    """読み取り結果。``metadata`` が ``None`` なら失敗。"""

    entry_id: str
    path: Path
    token: int
    metadata: TrackMetadata | None


ReadFunction = Callable[[Path], TrackMetadata]


class _WorkerSignals(QObject):
    """ワーカーが結果を返すためのシグナル。

    ワーカー自身が所有する（MetadataReader の子にしない）。受信側の
    MetadataReader が先に破棄されても、Qt が接続を切るだけで済むようにするため。
    """

    finished = Signal(object)


class _MetadataWorker(QRunnable):
    """1 件のメタデータを読むだけの QRunnable。

    PlaylistModel にも QWidget にも触らない。例外をスレッド外へ漏らさない。
    """

    def __init__(self, request: MetadataRequest, read_function: ReadFunction) -> None:
        super().__init__()
        self._request = request
        self._read_function = read_function
        self.signals = _WorkerSignals()

    def run(self) -> None:
        metadata: TrackMetadata | None = None
        try:
            metadata = self._read_function(self._request.path)
        except (MetadataReadError, OSError, MutagenError, ValueError) as error:
            _logger.info(
                "メタデータを読み取れませんでした: entry_id=%s path=%s (%s: %s)",
                self._request.entry_id,
                self._request.path,
                type(error).__name__,
                error,
            )
        except Exception:
            # 想定外でもワーカースレッドから例外を漏らさない
            # （BaseException は捕まえない）。
            _logger.exception(
                "メタデータ読み取りで予期しない例外: entry_id=%s path=%s",
                self._request.entry_id,
                self._request.path,
            )
        self.signals.finished.emit(
            MetadataResult(
                entry_id=self._request.entry_id,
                path=self._request.path,
                token=self._request.token,
                metadata=metadata,
            )
        )


class MetadataReader(QObject):
    """PlaylistModel のエントリについて、メタデータを非同期に取得する。

    UI・再生制御・永続化は知らない。Model の行データだけを更新する。
    """

    def __init__(
        self,
        playlist: PlaylistModel,
        *,
        read_function: ReadFunction = read_track_metadata,
        max_threads: int | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playlist = playlist
        self._read_function = read_function
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(_resolve_thread_count(max_threads))
        # entry_id ごとの最新トークン。古い結果を弾くために使う。
        self._tokens: dict[str, int] = {}
        self._next_token = 0
        self._started = False
        self._shutdown = False

    @property
    def max_thread_count(self) -> int:
        return self._pool.maxThreadCount()

    @property
    def is_running(self) -> bool:
        return self._started and not self._shutdown

    # -- ライフサイクル -----------------------------------------------------

    def start(self) -> None:
        """Model の変化を監視し、既存エントリの読み取りを開始する（冪等）。"""
        if self._started or self._shutdown:
            return
        self._started = True
        self._playlist.rowsInserted.connect(self._on_rows_inserted)
        self._playlist.rowsRemoved.connect(self._on_rows_removed)
        self._playlist.dataChanged.connect(self._on_data_changed)
        self._playlist.modelReset.connect(self._on_model_reset)
        self._schedule_all()

    def shutdown(self, timeout_ms: int = SHUTDOWN_TIMEOUT_MS) -> None:
        """新しい要求を止め、未開始のタスクを捨てて協調的に停止する。

        実行中の Mutagen の同期 I/O は強制終了しない。トークンを無効化して
        結果を無視する論理キャンセルを行う。``timeout_ms`` はこのメソッド内での
        待機上限であり、実行中I/Oが戻らなければ、後のQThreadPool破棄でプロセス終了が
        遅れる可能性がある。厳密な終了時刻は保証しない。
        """
        if self._shutdown:
            return
        self._shutdown = True
        self._tokens.clear()
        if self._started:
            self._playlist.rowsInserted.disconnect(self._on_rows_inserted)
            self._playlist.rowsRemoved.disconnect(self._on_rows_removed)
            self._playlist.dataChanged.disconnect(self._on_data_changed)
            self._playlist.modelReset.disconnect(self._on_model_reset)
        self._clear_pending_tasks()
        if not self._pool.waitForDone(timeout_ms):
            _logger.warning(
                "メタデータ読み取りの終了待ちがタイムアウトしました（%dms）。", timeout_ms
            )

    def _clear_pending_tasks(self) -> None:
        """まだ開始していない読み取りtaskを破棄する。"""
        self._pool.clear()

    # -- Model の変化 -------------------------------------------------------

    def _on_rows_inserted(self, parent: QModelIndex, first: int, last: int) -> None:
        del parent
        for row in range(first, last + 1):
            if 0 <= row < self._playlist.rowCount():
                self._schedule_entry(row)

    def _on_rows_removed(self, parent: QModelIndex, first: int, last: int) -> None:
        """Modelから消えたentryの最新tokenを破棄する。"""
        del parent, first, last
        existing_ids = {entry.entry_id for entry in self._playlist.entries()}
        self._tokens = {
            entry_id: token for entry_id, token in self._tokens.items() if entry_id in existing_ids
        }

    def _on_data_changed(
        self, top_left: QModelIndex, bottom_right: QModelIndex, roles: list[int]
    ) -> None:
        """ファイル状態の変化だけに反応する。

        自分が起こすメタデータ更新の ``dataChanged`` へ反応すると再要求が
        無限に続くため、role で絞る。
        """
        if roles and FILE_STATUS_ROLE not in roles:
            return
        for row in range(top_left.row(), bottom_right.row() + 1):
            if 0 <= row < self._playlist.rowCount():
                self._schedule_entry(row)

    def _on_model_reset(self) -> None:
        self._tokens.clear()
        self._schedule_all()

    def _schedule_all(self) -> None:
        for row in range(self._playlist.rowCount()):
            self._schedule_entry(row)

    def _schedule_entry(self, row: int) -> None:
        """1 行の読み取りを投入する。欠損・取得済み・要求中は何もしない。"""
        if self._shutdown:
            return
        entry = self._playlist.entry_at(row)
        if entry.is_missing:
            # 欠損中は要求しない。復活したときの dataChanged で改めて要求する。
            self._tokens.pop(entry.entry_id, None)
            return
        if entry.metadata_status is not MetadataStatus.NOT_REQUESTED:
            return

        self._next_token += 1
        token = self._next_token
        self._tokens[entry.entry_id] = token
        self._playlist.mark_metadata_loading(entry.entry_id)

        worker = _MetadataWorker(
            MetadataRequest(entry_id=entry.entry_id, path=entry.path, token=token),
            self._read_function,
        )
        worker.signals.finished.connect(self._on_result)
        self._pool.start(worker)

    # -- 結果の反映（GUI スレッド） -----------------------------------------

    def _on_result(self, result: object) -> None:
        if not isinstance(result, MetadataResult):
            return
        if not self._is_applicable(result):
            # 同じentryの新しい要求がある場合は、そのtokenを古い結果で消さない。
            if self._tokens.get(result.entry_id) == result.token:
                self._tokens.pop(result.entry_id, None)
                row = self._playlist.row_of_entry_id(result.entry_id)
                if row is not None:
                    entry = self._playlist.entry_at(row)
                    if not entry.is_missing and entry.metadata_status is MetadataStatus.LOADING:
                        # 現在要求の結果なのにpathなどが一致しない異常。LOADINGへ
                        # 固着させず失敗として閉じるが、値は適用しない。
                        self._playlist.mark_metadata_failed(result.entry_id)
            # 削除・reset・再要求・欠損・shutdown と競合しただけなので、
            # 通常運転で大量に起こりうる。警告は出さない。
            _logger.debug(
                "適用しないメタデータ結果: entry_id=%s token=%s",
                result.entry_id,
                result.token,
            )
            return

        self._tokens.pop(result.entry_id, None)
        if result.metadata is None:
            self._playlist.mark_metadata_failed(result.entry_id)
        else:
            self._playlist.apply_metadata(result.entry_id, result.metadata)

    def _is_applicable(self, result: MetadataResult) -> bool:
        """古い結果・別 entry の結果を弾く。パスからの流用は認めない。"""
        if self._shutdown:
            return False
        if self._tokens.get(result.entry_id) != result.token:
            return False
        row = self._playlist.row_of_entry_id(result.entry_id)
        if row is None:
            return False
        entry = self._playlist.entry_at(row)
        return (
            entry.path == result.path
            and not entry.is_missing
            and entry.metadata_status is MetadataStatus.LOADING
        )


def _resolve_thread_count(max_threads: int | None) -> int:
    if max_threads is not None:
        return max(1, max_threads)
    return max(1, min(MAX_WORKER_THREADS, QThread.idealThreadCount()))

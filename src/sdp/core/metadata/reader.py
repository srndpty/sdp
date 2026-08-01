"""Mutagen によるメタデータ読み取りと、その非同期実行。

読み取りはファイル I/O なので **GUI スレッドでは行わない**。
純粋関数 :func:`read_track_metadata` をワーカースレッドで実行し、
結果は GUI スレッドで PlaylistModel へ反映する。

古い結果を誤って反映しないよう、要求ごとに単調増加のトークンを付け、
反映前に entry_id・path・状態を照合する。パスから別 entry へ流用しない。
"""

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import mutagen
from mutagen import MutagenError
from mutagen.id3 import ID3, Encoding, ID3NoHeaderError
from mutagen.mp3 import EasyMP3
from PySide6.QtCore import QModelIndex, QObject, QRunnable, QThread, QThreadPool, Signal

from sdp.core.metadata.types import MetadataStatus, TrackMetadata
from sdp.core.playlist.entry import FileStatus
from sdp.core.playlist.model import FILE_STATUS_ROLE, PlaylistModel
from sdp.services.thread_shutdown import ShutdownOutcome

_logger = logging.getLogger(__name__)

ARTIST_SEPARATOR = "/"
"""複数アーティストを 1 つの文字列へ結合する区切り。1 件なら付けない。"""

MAX_WORKER_THREADS = 4
"""メタデータ読み取りの最大並列数の上限。ファイル I/O なので過度に増やさない。"""

SHUTDOWN_TIMEOUT_MS = 3_000

BACKLOG_SLOTS = 4
"""実行中スレッド数へ上乗せする投入枠。

これ以上は待機キューへ積まず、``MetadataReader`` 内の待ち行列に留める。
1000曲を追加しても、QThreadPool へ積まれる worker と Signal object は
``max_thread_count + BACKLOG_SLOTS`` 件で頭打ちになり、起動直後のメモリと
I/O量が件数に比例しなくなる。
"""


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
    try:
        id3_tags = _read_id3_tags(path) if isinstance(audio, EasyMP3) else None
    except (MutagenError, OSError) as error:
        raise MetadataReadError(f"ID3タグを読み取れません: {path}") from error
    return TrackMetadata(
        title=_first_text(tags, "title", id3_tags=id3_tags, frame_id="TIT2"),
        artist=_joined_text(tags, "artist", id3_tags=id3_tags, frame_id="TPE1"),
        album=_first_text(tags, "album", id3_tags=id3_tags, frame_id="TALB"),
        duration_ms=_duration_ms(info),
        bitrate_bps=_bitrate_bps(info),
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


def _read_id3_tags(path: Path) -> ID3 | None:
    """MP3の生ID3タグを読み、各frameの文字encoding情報を保持する。"""
    try:
        return ID3(path)
    except ID3NoHeaderError:
        return None


def _id3_tag_values(tags: ID3 | None, frame_id: str) -> list[str] | None:
    """ID3 text frameを宣言encoding付きで取り出す。frameが無ければfallbackさせる。"""
    if tags is None:
        return None
    frames = cast("list[Any]", cast("Any", tags).getall(frame_id))
    if not frames:
        return None
    values: list[str] = []
    for frame in frames:
        raw = getattr(frame, "text", None)
        if not isinstance(raw, list):
            continue
        declared_latin1 = getattr(frame, "encoding", None) == Encoding.LATIN1
        values.extend(
            _repair_mojibake(value, declared_latin1=declared_latin1).strip()
            for value in cast("list[Any]", raw)
            if isinstance(value, str)
        )
    return values


def _repair_mojibake(value: str, *, declared_latin1: bool) -> str:
    """Latin-1指定が確認できたID3文字列だけをCP932として補正する。

    古いタグ編集ソフトには、Shift-JISのバイト列をID3上でLatin-1と宣言するものがある。
    EasyTagやFLAC等の来歴が不明な文字列へは適用しない。宣言を確認した値でも、
    round-tripが成立し、日本語が文字列の半分以上を占める場合だけ採用する。
    """
    if not declared_latin1 or _contains_japanese(value):
        return value
    try:
        original_bytes = value.encode("latin-1")
        repaired = original_bytes.decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    if repaired.encode("cp932") != original_bytes:
        return value
    visible = [character for character in repaired if not character.isspace()]
    japanese = sum(1 for character in visible if _is_japanese(character))
    return repaired if visible and japanese / len(visible) >= 0.5 else value


def _contains_japanese(value: str) -> bool:
    return any(_is_japanese(character) for character in value)


def _is_japanese(character: str) -> bool:
    return "\u3040" <= character <= "\u30ff" or "\u3400" <= character <= "\u9fff"


def _first_text(
    tags: object,
    key: str,
    *,
    id3_tags: ID3 | None = None,
    frame_id: str = "",
) -> str | None:
    """最初の非空値。すべて空なら ``None``。"""
    id3_values = _id3_tag_values(id3_tags, frame_id) if frame_id else None
    for value in _tag_values(tags, key) if id3_values is None else id3_values:
        if value:
            return value
    return None


def _joined_text(
    tags: object,
    key: str,
    *,
    id3_tags: ID3 | None = None,
    frame_id: str = "",
) -> str | None:
    """非空値を順序どおり結合する。1 件なら区切りを付けない。"""
    id3_values = _id3_tag_values(id3_tags, frame_id) if frame_id else None
    source_values = _tag_values(tags, key) if id3_values is None else id3_values
    values = [value for value in source_values if value]
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


def _bitrate_bps(info: object) -> int | None:
    """``info.bitrate``を正のbit/sとして取得する。"""
    bitrate = getattr(info, "bitrate", None)
    if not isinstance(bitrate, (int, float)) or isinstance(bitrate, bool):
        return None
    value = float(bitrate)
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value)


def _file_size_bytes(path: Path) -> int:
    """ファイルサイズを取得し、I/O失敗をメタデータ読取失敗へ揃える。"""
    try:
        return path.stat().st_size
    except OSError as error:
        raise MetadataReadError(f"ファイル情報を読み取れません: {path}") from error


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
    file_size_bytes: int | None = None


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
        file_size_bytes: int | None = None
        try:
            file_size_bytes = _file_size_bytes(self._request.path)
        except MetadataReadError as error:
            _logger.info("ファイルサイズを取得できませんでした: %s", error)
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
                file_size_bytes=file_size_bytes,
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
        self._shutdown_outcome: ShutdownOutcome | None = None
        # 投入待ちの entry_id（FIFO）。同じ entry を二重に積まない。
        self._pending: deque[str] = deque()
        self._pending_ids: set[str] = set()
        self._in_flight = 0

    @property
    def max_thread_count(self) -> int:
        return self._pool.maxThreadCount()

    @property
    def max_in_flight(self) -> int:
        """同時に QThreadPool へ積む上限。"""
        return self._pool.maxThreadCount() + BACKLOG_SLOTS

    @property
    def pending_count(self) -> int:
        """まだ投入していない待ち件数（診断とテスト用）。

        取り消し済みの幽霊はdequeへ残るため、有効な件数は集合の側で数える。
        """
        return len(self._pending_ids)

    @property
    def in_flight_count(self) -> int:
        """投入済みで結果待ちの件数（診断とテスト用）。"""
        return self._in_flight

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

    def shutdown(self, timeout_ms: int = SHUTDOWN_TIMEOUT_MS) -> ShutdownOutcome:
        """新しい要求を止め、未開始のタスクを捨てて協調的に停止する。

        実行中の Mutagen の同期 I/O は強制終了しない。トークンを無効化して
        結果を無視する論理キャンセルを行う。

        **``timeout_ms`` はこのメソッドの待機上限であり、プロセス終了の上限ではない。**
        ``QThreadPool`` のデストラクタは全runnableの完了までブロックする仕様のため、
        実行中のI/Oが戻らなければ、このメソッドが戻ったあとのpool破棄で終了が
        止まりうる。QThreadに対する :func:`sdp.services.thread_shutdown.stop_thread`
        のような「放棄」はQThreadPoolでは行えない（個々のrunnableを切り離せない）。

        厳密なプロセス終了上限が要るなら、キャンセル不能なMutagen I/Oを
        別プロセスへ分離する必要がある。現状は「戻らないファイルは想定しない」
        という前提で、待機上限だけを設けている
        （[architecture.md](../../../docs/architecture.md) の終了処理の節）。

        時間内に全runnableが終われば ``STOPPED``、そうでなければ ``ABANDONED``。
        冪等だが、初回の結果は書き換えない。

        ``timeout_ms`` の負値は 0 として扱う。Qt の待機APIでは負値が無期限待機を
        意味しうるため、そのまま渡すと「待機上限」という契約を破ってしまう。
        """
        if self._shutdown:
            return self._recheck_shutdown_outcome(timeout_ms)
        self._shutdown = True
        self._tokens.clear()
        self._pending.clear()
        self._pending_ids.clear()
        if self._started:
            self._playlist.rowsInserted.disconnect(self._on_rows_inserted)
            self._playlist.rowsRemoved.disconnect(self._on_rows_removed)
            self._playlist.dataChanged.disconnect(self._on_data_changed)
            self._playlist.modelReset.disconnect(self._on_model_reset)
        self._clear_pending_tasks()
        timeout = max(0, timeout_ms)
        if self._pool.waitForDone(timeout):
            self._shutdown_outcome = ShutdownOutcome.STOPPED
        else:
            _logger.warning(
                "メタデータ読み取りの終了待ちがタイムアウトしました（%dms）。"
                "実行中のI/Oが戻るまで、後のQThreadPool破棄で終了が遅れます。",
                timeout,
            )
            self._shutdown_outcome = ShutdownOutcome.ABANDONED
        return self._shutdown_outcome

    def _recheck_shutdown_outcome(self, timeout_ms: int) -> ShutdownOutcome:
        """初回の結果を返す。その後に全runnableが終わっていれば STOPPED へ更新する。"""
        del timeout_ms
        if self._shutdown_outcome is ShutdownOutcome.ABANDONED and self._pool.waitForDone(0):
            self._shutdown_outcome = ShutdownOutcome.STOPPED
        return ShutdownOutcome.STOPPED if self._shutdown_outcome is None else self._shutdown_outcome

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
        # 取り消しはlazy deletion。dequeの再構築はここでも行わない。
        self._pending_ids &= existing_ids

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
        # 行が総入れ替えされた。積んだだけの要求は捨て、投入済みの結果は
        # token 不一致で無視される。投入済みは上限件数で頭打ちなので、
        # ここで pool.clear() はしない（投入枠のカウントが戻らなくなる）。
        self._tokens.clear()
        self._pending.clear()
        self._pending_ids.clear()
        self._schedule_all()

    def _schedule_all(self) -> None:
        for row in range(self._playlist.rowCount()):
            self._schedule_entry(row)
        self._pump()

    def _schedule_entry(self, row: int) -> None:
        """1 行を待ち行列へ積む。欠損・取得済み・要求中・積み済みは何もしない。

        ここでは QThreadPool へ投入しない。1000曲を追加しても worker と Signal
        object が件数ぶん作られないよう、投入は :meth:`_pump` が上限つきで行う。
        """
        if self._shutdown:
            return
        entry = self._playlist.entry_at(row)
        if entry.file_status is not FileStatus.AVAILABLE:
            # 欠損中と**未確認中**は要求しない。UNKNOWNのまま読み始めると、
            # ファイル状態確認とMutagen読み取りが同じファイルへ同時にI/Oを出し、
            # 欠損と判明するエントリや切断NASへも無駄な読み取りが走る。
            # AVAILABLEが確定した時点の dataChanged（FILE_STATUS_ROLE）で改めて要求する。
            self._tokens.pop(entry.entry_id, None)
            self._discard_pending(entry.entry_id)
            return
        if entry.metadata_status is not MetadataStatus.NOT_REQUESTED:
            return
        if entry.entry_id in self._pending_ids:
            return
        self._pending.append(entry.entry_id)
        self._pending_ids.add(entry.entry_id)
        self._pump()

    def _pump(self) -> None:
        """投入枠が空いているあいだ、待ち行列から順に投入する。"""
        while not self._shutdown and self._in_flight < self.max_in_flight and self._pending:
            entry_id = self._pending.popleft()
            # 取り消しはlazy deletion（_pending_idsから外すだけ）なので、
            # dequeへ残った幽霊はここで読み飛ばす。
            if entry_id not in self._pending_ids:
                continue
            self._pending_ids.discard(entry_id)
            if self._start_read(entry_id):
                self._in_flight += 1

    def _start_read(self, entry_id: str) -> bool:
        """1 件を実際に投入する。投入しなかった場合は ``False``。

        待ち行列に積んでから投入するまでの間に、削除・欠損・別経路での取得が
        起こりうるため、ここで状態を確認し直す。
        """
        row = self._playlist.row_of_entry_id(entry_id)
        if row is None:
            return False
        entry = self._playlist.entry_at(row)
        if (
            entry.file_status is not FileStatus.AVAILABLE
            or entry.metadata_status is not MetadataStatus.NOT_REQUESTED
        ):
            return False

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
        return True

    def _discard_pending(self, entry_id: str) -> None:
        """待ち行列から取り消す（deque本体からは消さないlazy deletion）。

        ファイル状態の確認は1行ずつ ``dataChanged`` を出すため、大量の欠損では
        取り消しが件数ぶん起きる。そのたびにdequeを作り直すとGUIスレッドで
        O(N^2) になるので、集合から外すだけにして投入時に読み飛ばす。
        """
        self._pending_ids.discard(entry_id)

    # -- 結果の反映（GUI スレッド） -----------------------------------------

    def _on_result(self, result: object) -> None:
        if not isinstance(result, MetadataResult):
            return
        # 適用可否に関わらず投入枠を返す（返し忘れると読み取りが止まる）。
        self._in_flight = max(0, self._in_flight - 1)
        try:
            self._apply_result(result)
        finally:
            self._pump()

    def _apply_result(self, result: MetadataResult) -> None:
        if not self._is_applicable(result):
            # 同じentryの新しい要求がある場合は、そのtokenを古い結果で消さない。
            if self._tokens.get(result.entry_id) == result.token:
                self._tokens.pop(result.entry_id, None)
                row = self._playlist.row_of_entry_id(result.entry_id)
                if row is not None:
                    entry = self._playlist.entry_at(row)
                    if (
                        entry.file_status is FileStatus.AVAILABLE
                        and entry.metadata_status is MetadataStatus.LOADING
                    ):
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
        self._playlist.apply_file_size(result.entry_id, result.file_size_bytes)
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
            and entry.file_status is FileStatus.AVAILABLE
            and entry.metadata_status is MetadataStatus.LOADING
        )


def _resolve_thread_count(max_threads: int | None) -> int:
    if max_threads is not None:
        return max(1, max_threads)
    return max(1, min(MAX_WORKER_THREADS, QThread.idealThreadCount()))

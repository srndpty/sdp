"""プレイリストの 1 行を表すデータ構造。

再生状態（現在再生中・選択中）や UI 状態は持たない。
メタデータは値（TrackMetadata）と読み込み状態を不変値として保持するだけで、
読み取り処理そのものは :mod:`sdp.core.metadata.reader` の責務。
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from sdp.core.metadata.types import MetadataStatus, TrackMetadata


class FileStatus(Enum):
    """エントリのファイルが利用できるかどうか。

    欠損しても行は消さずに保持する（PL-05）。再生エラーやメタデータの状態は
    ここへ混ぜない。それらは別の関心事であり、必要になった段階で追加する。

    ``UNKNOWN`` は「まだ調べていない」。エントリ生成時にファイルシステムへ
    触れないためにある（1000曲の復元やD&DでGUIスレッドが止まらないようにする）。
    実際の確認は :class:`~sdp.services.playlist_file_status.PlaylistFileStatusChecker`
    が背景で少しずつ行う。**未確認を欠損として扱わない**（灰色表示も曲送りの
    スキップもしない）。再生直前には個別に再確認するため、取りこぼしはしない。
    """

    UNKNOWN = auto()
    AVAILABLE = auto()
    MISSING = auto()


def normalize_path(path: Path) -> Path:
    """エントリが保持するパスの正規化。ここが唯一の正規化地点。

    絶対パスへ統一し、相対パスを作業ディレクトリ依存のまま保持しない。
    ``strict=False`` なのは、存在しないファイルも復元・保持できる必要があるため
    （欠損エントリはプレイリストから消さない）。
    """
    return Path(path).expanduser().resolve(strict=False)


def new_entry_id() -> str:
    """新しい entry_id を発行する。

    行番号でも ``hash(path)`` でもない、プロセスをまたいで安定した文字列。
    同じパスを複数回追加しても別の ID になり、保存・復元でも維持される。
    """
    return uuid.uuid4().hex


def probe_file_status(path: Path) -> FileStatus:
    """現在のファイル状態を調べる。拡張子や音声形式は判定しない。"""
    return FileStatus.AVAILABLE if path.is_file() else FileStatus.MISSING


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    """プレイリストの 1 行。

    同じパスの重複追加を許可するため、行の同一性は :attr:`path` ではなく
    :attr:`entry_id` で判断する（PL-07）。
    """

    entry_id: str
    path: Path
    file_status: FileStatus = FileStatus.UNKNOWN
    metadata: TrackMetadata | None = field(init=False, default=None)
    metadata_status: MetadataStatus = field(init=False, default=MetadataStatus.NOT_REQUESTED)

    def __post_init__(self) -> None:
        # ここではファイルシステムへ触れない。1000曲の復元やD&Dで、GUIスレッド上に
        # 件数ぶんの stat が積み上がるのを避けるため（NAS・切断ドライブ・
        # オンライン専用ファイルでは1件が長時間ブロックしうる）。
        if not self.entry_id:
            raise ValueError("entry_id が空です。")
        if not self.path.is_absolute():
            raise ValueError(f"entry のパスは絶対パスにしてください: {self.path}")

    @property
    def display_name(self) -> str:
        """表示用のファイル名。メタデータによる表示は P2-D の責務。"""
        return self.path.name

    @property
    def is_missing(self) -> bool:
        return self.file_status is FileStatus.MISSING

    @property
    def display_title(self) -> str:
        """表示に使うタイトル。

        メタデータが無い・読み取り中・失敗のいずれでも、常に何かが表示されるよう
        ファイル名（それも取れなければパス全体）へフォールバックする。
        """
        if self.metadata is not None and self.metadata.title:
            return self.metadata.title
        return self.path.name or str(self.path)

    def with_refreshed_status(self) -> "PlaylistEntry":
        """**その場でファイルを調べ直した**新しいエントリを返す。

        再生直前など、1件だけ確実に確認したい場所で使う。全行への一括適用には
        使わない（GUIスレッドで件数ぶんの stat が走るため）。
        状態が変わらない場合は自分自身を返すため、呼び出し側は同一性で判定できる。
        """
        return self.with_file_status(probe_file_status(self.path))

    def with_file_status(self, status: FileStatus) -> "PlaylistEntry":
        """調べ済みの状態を当てはめた新しいエントリを返す（I/Oはしない）。

        欠損との出入りではメタデータを捨てて ``NOT_REQUESTED`` へ戻す。
        欠損中は表示をファイル名へ戻し、復活後は読み直せるようにするため。
        一方、``UNKNOWN`` から確定しただけのときは読み取り済み・読み取り中の
        メタデータを保持する（背景の状態確認が読み取りを打ち消さないように）。
        """
        if status is self.file_status:
            return self
        keeps_metadata = FileStatus.MISSING not in (status, self.file_status)
        clone = object.__new__(PlaylistEntry)
        object.__setattr__(clone, "entry_id", self.entry_id)
        object.__setattr__(clone, "path", self.path)
        object.__setattr__(clone, "file_status", status)
        object.__setattr__(clone, "metadata", self.metadata if keeps_metadata else None)
        object.__setattr__(
            clone,
            "metadata_status",
            self.metadata_status if keeps_metadata else MetadataStatus.NOT_REQUESTED,
        )
        return clone

    # -- メタデータ（不変更新） ---------------------------------------------

    def with_metadata_loading(self) -> "PlaylistEntry":
        """読み取り中にする。値はまだ持たない。"""
        return self._with_metadata(None, MetadataStatus.LOADING)

    def with_metadata(self, metadata: TrackMetadata) -> "PlaylistEntry":
        """読み取れた値を保持する。"""
        return self._with_metadata(metadata, MetadataStatus.LOADED)

    def with_metadata_failed(self) -> "PlaylistEntry":
        """読み取りに失敗した。値は持たず、表示はファイル名へフォールバックする。"""
        return self._with_metadata(None, MetadataStatus.FAILED)

    def without_metadata(self) -> "PlaylistEntry":
        """メタデータを捨てて未要求へ戻す（再読み取り可能にする）。"""
        return self._with_metadata(None, MetadataStatus.NOT_REQUESTED)

    def _with_metadata(
        self, metadata: TrackMetadata | None, status: MetadataStatus
    ) -> "PlaylistEntry":
        """entry_id・path・file_status を変えずに複製する（I/Oはしない）。"""
        clone = object.__new__(PlaylistEntry)
        object.__setattr__(clone, "entry_id", self.entry_id)
        object.__setattr__(clone, "path", self.path)
        object.__setattr__(clone, "file_status", self.file_status)
        object.__setattr__(clone, "metadata", metadata)
        object.__setattr__(clone, "metadata_status", status)
        return clone


def create_entry(
    path: Path, *, entry_id: str | None = None, file_status: FileStatus = FileStatus.UNKNOWN
) -> PlaylistEntry:
    """パスから新しいエントリを作る。パスの正規化だけを行い、I/Oはしない。

    ``entry_id`` を渡すのは永続化からの復元時のみを想定する。
    ファイル状態は既定で ``UNKNOWN`` とし、背景の確認に委ねる。
    """
    normalized = normalize_path(path)
    return PlaylistEntry(
        entry_id=new_entry_id() if entry_id is None else entry_id,
        path=normalized,
        file_status=file_status,
    )

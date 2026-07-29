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
    """

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
    file_status: FileStatus = field(init=False)
    metadata: TrackMetadata | None = field(init=False, default=None)
    metadata_status: MetadataStatus = field(init=False, default=MetadataStatus.NOT_REQUESTED)

    def __post_init__(self) -> None:
        if not self.entry_id:
            raise ValueError("entry_id が空です。")
        if not self.path.is_absolute():
            raise ValueError(f"entry のパスは絶対パスにしてください: {self.path}")
        object.__setattr__(self, "file_status", probe_file_status(self.path))

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
        """ファイル状態を再確認した新しいエントリを返す。

        ファイルが削除・復元された場合に Model 側が行を差し替えるために使う。
        状態が変わらない場合は自分自身を返すため、呼び出し側は同一性で判定できる。

        状態が変わった場合はメタデータを捨てて ``NOT_REQUESTED`` へ戻す。
        欠損中は表示をファイル名へ戻し、復活後は読み直せるようにするため。
        """
        refreshed = PlaylistEntry(entry_id=self.entry_id, path=self.path)
        if refreshed.file_status is self.file_status:
            return self
        return refreshed

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
        """entry_id・path・file_status を変えず、ファイルを再調査せずに複製する。"""
        # 通常構築と状態再確認は __post_init__ を通すが、メタデータ更新はGUIスレッドで
        # 頻発するため、内部限定の複製ではファイルシステムへ触れない。
        clone = object.__new__(PlaylistEntry)
        object.__setattr__(clone, "entry_id", self.entry_id)
        object.__setattr__(clone, "path", self.path)
        object.__setattr__(clone, "file_status", self.file_status)
        object.__setattr__(clone, "metadata", metadata)
        object.__setattr__(clone, "metadata_status", status)
        return clone


def create_entry(path: Path, *, entry_id: str | None = None) -> PlaylistEntry:
    """パスから新しいエントリを作る。正規化とファイル状態の判定を行う。

    ``entry_id`` を渡すのは永続化からの復元時のみを想定する。
    """
    normalized = normalize_path(path)
    return PlaylistEntry(
        entry_id=new_entry_id() if entry_id is None else entry_id,
        path=normalized,
    )

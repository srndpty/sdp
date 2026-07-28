"""プレイリストの 1 行を表すデータ構造。

再生状態（現在再生中・選択中）や UI 状態は持たない。
メタデータ関連の状態も持たない（必要性は P2-D で判断する）。
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


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

    def with_refreshed_status(self) -> "PlaylistEntry":
        """ファイル状態を再確認した新しいエントリを返す。

        ファイルが削除・復元された場合に Model 側が行を差し替えるために使う。
        状態が変わらない場合は自分自身を返すため、呼び出し側は同一性で判定できる。
        """
        refreshed = PlaylistEntry(entry_id=self.entry_id, path=self.path)
        if refreshed.file_status is self.file_status:
            return self
        return refreshed


def create_entry(path: Path, *, entry_id: str | None = None) -> PlaylistEntry:
    """パスから新しいエントリを作る。正規化とファイル状態の判定を行う。

    ``entry_id`` を渡すのは永続化からの復元時のみを想定する。
    """
    normalized = normalize_path(path)
    return PlaylistEntry(
        entry_id=new_entry_id() if entry_id is None else entry_id,
        path=normalized,
    )

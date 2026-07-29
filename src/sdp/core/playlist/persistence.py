"""プレイリストの JSON 保存と復元。

保存先の決定・保存タイミング・自動保存は呼び出し側（P2-C 以降）の責務で、
ここは「与えられたパスへ書く / 読む」だけを担当する。

M3U8 入出力は将来ここへ追加する（開発計画 PL-08。P8 以降）。
"""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from sdp.core.playlist.entry import PlaylistEntry, create_entry

SCHEMA_VERSION = 1
"""プレイリストファイルのスキーマバージョン。

実際の旧バージョンが生まれるまで migration は作り込まない
（[AGENTS.md](../../../../AGENTS.md) の方針）。読み込み時にバージョンが違えば
黙って解釈せず、明示的なエラーにする。
"""


class PlaylistFileError(Exception):
    """プレイリストファイルが壊れている、または解釈できない。

    メッセージは日本語。呼び出し側がユーザー向け表示とログへ振り分ける。
    """


def save_playlist(file_path: Path, entries: Sequence[PlaylistEntry]) -> None:
    """プレイリストをアトミックに書き出す。

    同じディレクトリの一時ファイルへ書いてから ``os.replace`` で置き換えるため、
    書き込み中の異常終了で既存ファイルが壊れない。

    ファイル状態（欠損かどうか）は保存しない。復元時にファイルシステムから
    判定し直すのが常に正しいため。
    """
    _validate_entries_for_save(entries)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "entries": [{"entry_id": entry.entry_id, "path": str(entry.path)} for entry in entries],
    }
    file_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=file_path.parent, prefix=f"{file_path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, file_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def load_playlist(file_path: Path) -> list[PlaylistEntry]:
    """プレイリストを復元する。ファイル状態は読み込み時に判定し直す。

    ファイルが無い場合は初回起動の正常状態として空リストを返す。
    内容が壊れている場合は :class:`PlaylistFileError`。未知のキーは無視する。
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as error:
        raise PlaylistFileError(
            f"プレイリストファイルが UTF-8 として不正です: {file_path}"
        ) from error
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlaylistFileError(
            f"プレイリストファイルが JSON として不正です: {file_path}"
        ) from error

    if not isinstance(parsed, dict):
        raise PlaylistFileError(f"プレイリストファイルの形式が不正です: {file_path}")
    document = cast("dict[str, object]", parsed)

    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise PlaylistFileError(
            f"未対応のプレイリスト schema_version です（期待 {SCHEMA_VERSION}、実際 {version!r}）"
        )

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise PlaylistFileError("プレイリストの entries が配列ではありません。")

    entries: list[PlaylistEntry] = []
    seen_entry_ids: set[str] = set()
    for index, raw_entry in enumerate(cast("list[object]", raw_entries)):
        entry = _entry_from_json(raw_entry, index)
        if entry.entry_id in seen_entry_ids:
            raise PlaylistFileError(f"entry_id が重複しています（{index} 番目）: {entry.entry_id}")
        seen_entry_ids.add(entry.entry_id)
        entries.append(entry)
    return entries


def _validate_entries_for_save(entries: Sequence[PlaylistEntry]) -> None:
    """自身で読み戻せないプレイリストを保存前に拒否する。"""
    seen_entry_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not entry.entry_id:
            raise ValueError(f"entries[{index}] の entry_id が空です。")
        if entry.entry_id in seen_entry_ids:
            raise ValueError(f"entry_id が重複しています: {entry.entry_id}")
        if not entry.path.is_absolute():
            raise ValueError(f"entries[{index}] のパスが絶対パスではありません。")
        seen_entry_ids.add(entry.entry_id)


def _entry_from_json(raw_entry: object, index: int) -> PlaylistEntry:
    if not isinstance(raw_entry, dict):
        raise PlaylistFileError(f"entries[{index}] がオブジェクトではありません。")
    fields = cast("dict[str, object]", raw_entry)
    entry_id = fields.get("entry_id")
    path = fields.get("path")
    if not isinstance(entry_id, str) or not entry_id:
        raise PlaylistFileError(f"entries[{index}] の entry_id が不正です: {entry_id!r}")
    if not isinstance(path, str) or not path:
        raise PlaylistFileError(f"entries[{index}] の path が不正です: {path!r}")
    # 保存後にファイルが消えていても復元する。状態はここで判定し直す。
    return create_entry(Path(path), entry_id=entry_id)

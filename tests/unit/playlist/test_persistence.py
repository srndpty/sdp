"""プレイリストの JSON 保存・復元を検証する（Qt 不要）。"""

import json
from pathlib import Path

import pytest

from sdp.core.playlist.entry import FileStatus, create_entry
from sdp.core.playlist.persistence import (
    SCHEMA_VERSION,
    PlaylistFileError,
    load_playlist,
    save_playlist,
)


@pytest.fixture
def audio_files(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for name in ("sine440.wav", "テスト 音源.mp3"):
        path = tmp_path / name
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def test_round_trip_preserves_order_ids_and_paths(tmp_path: Path, audio_files: list[Path]) -> None:
    """保存・復元で順序・entry_id・パスが保たれる。"""
    entries = [create_entry(path) for path in audio_files]
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, entries)
    restored = load_playlist(file_path)

    assert [entry.entry_id for entry in restored] == [entry.entry_id for entry in entries]
    assert [entry.path for entry in restored] == [entry.path for entry in entries]


def test_duplicate_paths_round_trip_with_distinct_ids(
    tmp_path: Path, audio_files: list[Path]
) -> None:
    """同じパスの重複行が別々の entry として復元される。"""
    entries = [create_entry(audio_files[0]), create_entry(audio_files[0])]
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, entries)
    restored = load_playlist(file_path)

    assert len(restored) == 2
    assert restored[0].entry_id != restored[1].entry_id
    assert restored[0].path == restored[1].path


def test_saved_file_is_utf8_json_with_schema_version(
    tmp_path: Path, audio_files: list[Path]
) -> None:
    """UTF-8 の JSON で、schema_version を含む。"""
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, [create_entry(audio_files[1])])

    document = json.loads(file_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["entries"][0]["path"].endswith("テスト 音源.mp3")
    assert "テスト 音源.mp3" in file_path.read_text(encoding="utf-8")


def test_file_status_is_not_persisted(tmp_path: Path, audio_files: list[Path]) -> None:
    """ファイル状態は保存せず、復元時に判定し直す。"""
    entry = create_entry(audio_files[0])
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, [entry])
    document = json.loads(file_path.read_text(encoding="utf-8"))
    assert set(document["entries"][0]) == {"entry_id", "path"}

    audio_files[0].unlink()
    restored = load_playlist(file_path)

    assert entry.file_status is FileStatus.AVAILABLE
    assert restored[0].file_status is FileStatus.MISSING


def test_missing_entries_are_restored(tmp_path: Path) -> None:
    """存在しないファイルの行も復元される（消さない）。"""
    entry = create_entry(tmp_path / "ない曲.wav")
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, [entry])
    restored = load_playlist(file_path)

    assert len(restored) == 1
    assert restored[0].is_missing


def test_empty_playlist_round_trip(tmp_path: Path) -> None:
    """空のプレイリストも往復できる。"""
    file_path = tmp_path / "playlist.json"

    save_playlist(file_path, [])

    assert load_playlist(file_path) == []


def test_save_creates_parent_directories(tmp_path: Path, audio_files: list[Path]) -> None:
    """保存先の親ディレクトリが無ければ作る。"""
    file_path = tmp_path / "深い" / "階層" / "playlist.json"

    save_playlist(file_path, [create_entry(audio_files[0])])

    assert file_path.is_file()


def test_save_is_atomic_and_leaves_no_temporary_file(
    tmp_path: Path, audio_files: list[Path]
) -> None:
    """一時ファイルを残さず、既存ファイルを置き換える。"""
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, [create_entry(audio_files[0])])

    save_playlist(file_path, [create_entry(audio_files[1])])

    assert sorted(path.name for path in tmp_path.iterdir() if path.is_file()) == sorted(
        ["playlist.json", *(path.name for path in audio_files)]
    )
    assert len(load_playlist(file_path)) == 1


def test_failed_save_keeps_the_previous_file(
    tmp_path: Path, audio_files: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """書き込みに失敗しても既存ファイルは壊れない。"""
    file_path = tmp_path / "playlist.json"
    original = [create_entry(audio_files[0])]
    save_playlist(file_path, original)

    def failing_replace(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("置き換えに失敗")

    monkeypatch.setattr("sdp.core.playlist.persistence.os.replace", failing_replace)

    with pytest.raises(OSError):
        save_playlist(file_path, [create_entry(audio_files[1])])

    assert [entry.entry_id for entry in load_playlist(file_path)] == [original[0].entry_id]
    assert [path.name for path in tmp_path.glob("*.tmp")] == []


def test_duplicate_ids_are_rejected_before_touching_existing_file(
    tmp_path: Path, audio_files: list[Path]
) -> None:
    """重複IDの保存を拒否し、既存ファイルと一時ファイルを変更しない。"""
    file_path = tmp_path / "playlist.json"
    save_playlist(file_path, [create_entry(audio_files[0])])
    existing_content = file_path.read_bytes()
    duplicate = create_entry(audio_files[1])

    with pytest.raises(ValueError, match="entry_id が重複"):
        save_playlist(file_path, [duplicate, duplicate])

    assert file_path.read_bytes() == existing_content
    assert list(tmp_path.glob("playlist.json.*.tmp")) == []


# -- 破損データ -------------------------------------------------------------


def test_missing_file_returns_empty_playlist(tmp_path: Path) -> None:
    """保存ファイルが無い初回起動は空プレイリストとして扱う。"""
    assert load_playlist(tmp_path / "存在しない.json") == []


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_schema_version_requires_an_exact_integer(tmp_path: Path, version: object) -> None:
    """bool・float・文字列・nullをschema version 1として受理しない。"""
    file_path = tmp_path / "playlist.json"
    file_path.write_text(
        json.dumps({"schema_version": version, "entries": []}),
        encoding="utf-8",
    )

    with pytest.raises(PlaylistFileError, match="schema_version"):
        load_playlist(file_path)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("{壊れた", "JSON として不正"),
        ("[]", "オブジェクトではない"),
        ('{"entries": []}', "schema_version が無い"),
        ('{"schema_version": 999, "entries": []}', "未対応のバージョン"),
        ('{"schema_version": 1}', "entries が無い"),
        ('{"schema_version": 1, "entries": {}}', "entries が配列でない"),
        ('{"schema_version": 1, "entries": ["x"]}', "要素がオブジェクトでない"),
        ('{"schema_version": 1, "entries": [{"path": "C:/a.wav"}]}', "entry_id が無い"),
        (
            '{"schema_version": 1, "entries": [{"entry_id": "", "path": "C:/a.wav"}]}',
            "空の entry_id",
        ),
        ('{"schema_version": 1, "entries": [{"entry_id": "a"}]}', "path が無い"),
        ('{"schema_version": 1, "entries": [{"entry_id": "a", "path": 1}]}', "path が文字列でない"),
        (
            '{"schema_version": 1, "entries": ['
            '{"entry_id": "a", "path": "C:/a.wav"}, {"entry_id": "a", "path": "C:/b.wav"}]}',
            "entry_id の重複",
        ),
    ],
)
def test_corrupted_file_raises_explicit_error(tmp_path: Path, content: str, reason: str) -> None:
    """壊れたデータは黙って解釈せず、明示的なエラーにする。"""
    file_path = tmp_path / "playlist.json"
    file_path.write_text(content, encoding="utf-8")

    with pytest.raises(PlaylistFileError) as error:
        load_playlist(file_path)

    assert str(error.value), reason


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """未知のキーは無視する（将来の追加項目で読めなくならないように）。"""
    file_path = tmp_path / "playlist.json"
    file_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "generated_by": "将来の項目",
                "entries": [
                    {"entry_id": "a", "path": "C:/music/曲.wav", "rating": 5},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    entries = load_playlist(file_path)

    assert len(entries) == 1
    assert entries[0].entry_id == "a"

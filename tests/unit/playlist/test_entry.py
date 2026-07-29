"""PlaylistEntry の契約を検証する（Qt 不要）。"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sdp.core.playlist.entry import (
    FileStatus,
    PlaylistEntry,
    create_entry,
    new_entry_id,
    normalize_path,
    probe_file_status,
)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "テスト 音源.wav"
    path.write_bytes(b"RIFF----WAVEfmt ")
    return path


# -- entry_id ---------------------------------------------------------------


def test_new_entry_ids_are_unique() -> None:
    """発行のたびに異なる ID になる。"""
    identifiers = {new_entry_id() for _ in range(1000)}

    assert len(identifiers) == 1000


def test_same_path_added_twice_gets_different_ids(audio_file: Path) -> None:
    """同じパスを複数回追加しても別の ID になる（重複追加を許可するため）。"""
    first = create_entry(audio_file)
    second = create_entry(audio_file)

    assert first.entry_id != second.entry_id
    assert first.path == second.path


def test_entry_id_is_a_json_friendly_string() -> None:
    """entry_id は JSON へそのまま保存できる文字列。"""
    entry_id = new_entry_id()

    assert isinstance(entry_id, str)
    assert entry_id


def test_empty_entry_id_is_rejected(audio_file: Path) -> None:
    """空の entry_id は拒否する。"""
    with pytest.raises(ValueError):
        PlaylistEntry(entry_id="", path=audio_file)


def test_given_entry_id_is_preserved(audio_file: Path) -> None:
    """復元時に渡した entry_id をそのまま保持する。"""
    entry = create_entry(audio_file, entry_id="restored-id")

    assert entry.entry_id == "restored-id"


# -- パス -------------------------------------------------------------------


def test_relative_path_is_normalized_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相対パスは作業ディレクトリ依存のまま保持せず、絶対パスへ正規化する。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "曲.wav").write_bytes(b"x")

    entry = create_entry(Path("曲.wav"))

    assert entry.path.is_absolute()
    assert entry.path == (tmp_path / "曲.wav").resolve()


def test_relative_path_is_rejected_by_the_dataclass(audio_file: Path) -> None:
    """正規化を経ないパスは受け付けない（正規化地点を 1 か所に保つ）。"""
    del audio_file
    with pytest.raises(ValueError):
        PlaylistEntry(entry_id="id", path=Path("relative.wav"))


def test_missing_path_can_be_kept(tmp_path: Path) -> None:
    """存在しないパスも保持できる（欠損行を消さないため）。"""
    entry = create_entry(tmp_path / "ない曲.wav")

    assert entry.path.is_absolute()
    assert entry.file_status is FileStatus.MISSING


def test_direct_construction_probes_missing_status(tmp_path: Path) -> None:
    """直接構築しても欠損ファイルをAVAILABLEとして保持しない。"""
    entry = PlaylistEntry(entry_id="missing", path=tmp_path / "ない曲.wav")

    assert entry.file_status is FileStatus.MISSING


def test_japanese_and_space_are_preserved(audio_file: Path) -> None:
    """日本語と空白を含むパスをそのまま保持する。"""
    entry = create_entry(audio_file)

    assert entry.path.name == "テスト 音源.wav"
    assert entry.display_name == "テスト 音源.wav"


def test_unknown_extension_is_accepted(tmp_path: Path) -> None:
    """拡張子で拒否しない（対応可否は再生時に判定する）。"""
    path = tmp_path / "拡張子なし"
    path.write_bytes(b"x")

    entry = create_entry(path)

    assert entry.path == path.resolve()
    assert entry.file_status is FileStatus.AVAILABLE


def test_normalize_path_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`~` を展開する。"""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert normalize_path(Path("~/曲.wav")) == (tmp_path / "曲.wav").resolve()


# -- 欠損状態 ---------------------------------------------------------------


def test_probe_file_status_distinguishes_available_and_missing(
    audio_file: Path, tmp_path: Path
) -> None:
    """利用可能と欠損を区別する。"""
    assert probe_file_status(audio_file) is FileStatus.AVAILABLE
    assert probe_file_status(tmp_path / "ない曲.wav") is FileStatus.MISSING


def test_directory_is_treated_as_missing(tmp_path: Path) -> None:
    """ディレクトリは通常ファイルではないため欠損として扱う。"""
    assert probe_file_status(tmp_path) is FileStatus.MISSING


def test_refresh_detects_deleted_file(audio_file: Path) -> None:
    """ファイルが削除されたら欠損になる。"""
    entry = create_entry(audio_file)
    audio_file.unlink()

    refreshed = entry.with_refreshed_status()

    assert refreshed.file_status is FileStatus.MISSING
    assert refreshed.is_missing
    assert refreshed.entry_id == entry.entry_id
    assert refreshed.path == entry.path


def test_refresh_detects_restored_file(tmp_path: Path) -> None:
    """ファイルが復元されたら利用可能へ戻る。"""
    path = tmp_path / "戻る曲.wav"
    entry = create_entry(path)
    assert entry.is_missing
    path.write_bytes(b"x")

    refreshed = entry.with_refreshed_status()

    assert refreshed.file_status is FileStatus.AVAILABLE


def test_refresh_returns_self_when_unchanged(audio_file: Path) -> None:
    """状態が変わらなければ同一オブジェクトを返す（変更検出に使える）。"""
    entry = create_entry(audio_file)

    assert entry.with_refreshed_status() is entry


# -- 不変性 -----------------------------------------------------------------


def test_entry_is_immutable(audio_file: Path) -> None:
    """エントリは不変。"""
    entry = create_entry(audio_file)

    with pytest.raises(FrozenInstanceError):
        entry.path = audio_file  # type: ignore[misc]


def test_entry_has_no_playback_or_ui_state(audio_file: Path) -> None:
    """再生状態・UI 状態を持たない（メタデータは P2-D で追加した値なので別）。"""
    entry = create_entry(audio_file)

    for forbidden in ("is_current", "is_selected", "current", "selected", "playback_error"):
        assert not hasattr(entry, forbidden), forbidden

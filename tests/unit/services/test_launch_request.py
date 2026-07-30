"""Qt非依存の起動引数解析契約を検証する。"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sdp.services.launch_request import LaunchRequest, parse_launch_request


def test_request_is_immutable(tmp_path: Path) -> None:
    """起動要求は生成後に書き換えられない。"""
    request = LaunchRequest((tmp_path.resolve(),))

    with pytest.raises(FrozenInstanceError):
        request.paths = ()  # type: ignore[misc]


def test_request_rejects_relative_paths() -> None:
    """IPCを含む境界以降では絶対パスだけを保持する。"""
    with pytest.raises(ValueError, match="絶対パス"):
        LaunchRequest((Path("relative.wav"),))


@pytest.mark.parametrize("value", [0, 1, None, "true"])
def test_request_requires_strict_activate_window_bool(value: object) -> None:
    """Window前面化意図はbool相当値を暗黙に受理しない。"""
    with pytest.raises(TypeError, match="activate_window"):
        LaunchRequest(activate_window=value)  # type: ignore[arg-type]


def test_no_arguments_is_a_normal_empty_request(tmp_path: Path) -> None:
    """引数なしはエラーではなく空要求になる。"""
    assert parse_launch_request([], tmp_path) == LaunchRequest()


def test_absolute_path_is_kept(tmp_path: Path) -> None:
    """絶対パスは正規化したうえで保持する。"""
    path = (tmp_path / "tone.wav").resolve()

    request = parse_launch_request([str(path)], tmp_path)

    assert request.paths == (path,)
    assert request.ignored_arguments == ()


def test_relative_path_uses_startup_current_directory(tmp_path: Path) -> None:
    """相対パスはprocess途中のcwdではなく、渡された起動時cwd基準で絶対化する。"""
    request = parse_launch_request(["music/track.mp3"], tmp_path)

    assert request.paths == ((tmp_path / "music" / "track.mp3").resolve(),)


def test_unicode_spaces_and_os_split_arguments_are_not_reparsed(tmp_path: Path) -> None:
    """OS分割済みの空白・日本語を含む1引数をそのまま1パスとして扱う。"""
    request = parse_launch_request(["日本語 フォルダー/曲 名.flac"], tmp_path)

    assert request.paths == ((tmp_path / "日本語 フォルダー" / "曲 名.flac").resolve(),)


def test_duplicates_are_preserved(tmp_path: Path) -> None:
    """同一ファイルの重複指定は既存playlist契約どおり別entry候補として維持する。"""
    path = (tmp_path / "same.wav").resolve()

    request = parse_launch_request([str(path), str(path)], tmp_path)

    assert request.paths == (path, path)


def test_missing_path_is_accepted_as_a_missing_entry(tmp_path: Path) -> None:
    """存在しないパスはPlaylistModelと同じく欠損entryとして受理する。"""
    missing = (tmp_path / "missing.opus").resolve()

    assert parse_launch_request([str(missing)], tmp_path).paths == (missing,)


def test_unknown_extension_is_not_rejected(tmp_path: Path) -> None:
    """デコード可否を拡張子で断定しない既存契約を維持する。"""
    path = (tmp_path / "audio.unknown").resolve()

    assert parse_launch_request([str(path)], tmp_path).paths == (path,)


def test_directory_is_left_to_existing_playlist_validation(tmp_path: Path) -> None:
    """CLIでI/Oせず、ディレクトリ判定はPlaylistModelの追加経路に任せる。"""
    directory = tmp_path / "folder"
    directory.mkdir()
    path = (tmp_path / "valid.m4a").resolve()

    request = parse_launch_request([str(directory), str(path)], tmp_path)

    assert request.paths == (directory.resolve(), path)
    assert request.ignored_arguments == ()


def test_parser_does_not_check_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """応答しないnetwork pathを想定し、起動引数解析でis_dirを呼ばない。"""

    def fail_if_checked(path: Path) -> bool:
        del path
        raise AssertionError("起動引数解析でPath.is_dir()を呼んではいけません")

    monkeypatch.setattr(Path, "is_dir", fail_if_checked)

    request = parse_launch_request([r"\\offline-server\music\track.wav"], tmp_path)

    assert request.paths == (Path(r"\\offline-server\music\track.wav"),)


def test_unrepresentable_path_is_ignored(tmp_path: Path) -> None:
    """Path APIが扱えない値でも他の引数解析を継続する。"""
    path = (tmp_path / "valid.wav").resolve()

    request = parse_launch_request(["bad\0path", str(path)], tmp_path)

    assert request.paths == (path,)
    assert request.ignored_arguments == ("bad\0path",)

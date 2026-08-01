# Mutagen の型情報は形式ごとの共用体に Unknown を含むため、テスト側でも
# タグ書き込みヘルパーの周辺だけ緩める（プロジェクト全体の設定は変えない）。
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false
"""Mutagen によるメタデータ読み取り（純粋関数）の契約を検証する。

タグ付きファイルは、コミット済みのテスト音源を tmp_path へコピーして
Mutagen 自身で書き込む（FFmpeg などの外部プロセスは起動しない）。
"""

import math
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import mutagen
import pytest

from sdp.core.metadata.reader import (
    ARTIST_SEPARATOR,
    MetadataReadError,
    _bitrate_bps,
    _duration_ms,
    _repair_mojibake,
    read_track_metadata,
)
from sdp.core.metadata.types import (
    MetadataStatus,
    TrackMetadata,
    format_bitrate,
    format_duration_ms,
    format_file_size,
)


def copy_source(test_audio_dir: Path, tmp_path: Path, name: str) -> Path:
    """テスト音源を書き込み可能な場所へ複製する（原本は変更しない）。"""
    source = test_audio_dir / name
    assert source.is_file(), source
    destination = tmp_path / name
    shutil.copyfile(source, destination)
    return destination


def write_tags(path: Path, **tags: list[str]) -> None:
    audio = mutagen.File(path, easy=True)
    assert audio is not None
    audio.clear()
    for key, values in tags.items():
        audio[key] = values
    audio.save()


# -- TrackMetadata ----------------------------------------------------------


def test_track_metadata_is_immutable() -> None:
    """メタデータは不変値。"""
    metadata = TrackMetadata(title="曲", artist="人", album="盤", duration_ms=1000)

    with pytest.raises(FrozenInstanceError):
        metadata.title = "別"  # type: ignore[misc]


def test_track_metadata_defaults_are_none() -> None:
    """どれも取得できないことがある。"""
    metadata = TrackMetadata()

    assert (
        metadata.title,
        metadata.artist,
        metadata.album,
        metadata.duration_ms,
        metadata.file_size_bytes,
        metadata.bitrate_bps,
    ) == (
        None,
        None,
        None,
        None,
        None,
        None,
    )


def test_metadata_status_values() -> None:
    """欠損は FileStatus が表すので MetadataStatus へは入れない。"""
    assert {status.name for status in MetadataStatus} == {
        "NOT_REQUESTED",
        "LOADING",
        "LOADED",
        "FAILED",
    }


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [(0, "0:00"), (5_000, "0:05"), (65_000, "1:05"), (3_665_000, "1:01:05"), (-1, "0:00")],
)
def test_format_duration_ms(milliseconds: int, expected: str) -> None:
    """長さの表示は m:ss / h:mm:ss。"""
    assert format_duration_ms(milliseconds) == expected


@pytest.mark.parametrize(
    ("size_bytes", "expected"),
    [(0, "0 B"), (1024, "1.0 KiB"), (1_572_864, "1.5 MiB")],
)
def test_format_file_size(size_bytes: int, expected: str) -> None:
    """サイズは読みやすい2進単位で表示する。"""
    assert format_file_size(size_bytes) == expected


def test_format_bitrate_uses_kbps() -> None:
    """ビットレートは一般的なkbps表記へ変換する。"""
    assert format_bitrate(192_000) == "192 kbps"


# -- タグの読み取り ---------------------------------------------------------


def test_reads_title_artist_and_album(test_audio_dir: Path, tmp_path: Path) -> None:
    """基本のタグを読み取る。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.mp3")
    write_tags(path, title=["テスト曲"], artist=["演奏者"], album=["アルバム名"])

    metadata = read_track_metadata(path)

    assert metadata.title == "テスト曲"
    assert metadata.artist == "演奏者"
    assert metadata.album == "アルバム名"


def test_reads_tags_from_another_format(test_audio_dir: Path, tmp_path: Path) -> None:
    """形式が変わっても共通の名前で読み取れる（形式内部のキーは漏らさない）。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, title=["FLAC の曲"], artist=["奏者"], album=["盤"])

    metadata = read_track_metadata(path)

    assert metadata.title == "FLAC の曲"
    assert metadata.artist == "奏者"
    assert metadata.album == "盤"


def test_reads_japanese_and_spaced_tags(test_audio_dir: Path, tmp_path: Path) -> None:
    """日本語と空白を含むタグをそのまま保持する。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.mp3")
    write_tags(path, title=["日本語 の タイトル"], artist=["山田 太郎"], album=["空白 入り"])

    metadata = read_track_metadata(path)

    assert metadata.title == "日本語 の タイトル"
    assert metadata.artist == "山田 太郎"
    assert metadata.album == "空白 入り"


def test_repairs_cp932_bytes_misdeclared_as_latin1() -> None:
    """Latin-1指定されたCP932の日本語だけを元へ戻す。"""
    mojibake = "日本語タイトル".encode("cp932").decode("latin-1")
    assert _repair_mojibake(mojibake) == "日本語タイトル"
    assert _repair_mojibake("Beyoncé") == "Beyoncé"


def test_joins_multiple_artists(test_audio_dir: Path, tmp_path: Path) -> None:
    """複数アーティストは順序どおり結合する。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, artist=["最初", "次", "最後"])

    metadata = read_track_metadata(path)

    assert metadata.artist == ARTIST_SEPARATOR.join(["最初", "次", "最後"])


def test_single_artist_has_no_separator(test_audio_dir: Path, tmp_path: Path) -> None:
    """1 件なら区切りを付けない。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, artist=["ひとり"])

    assert read_track_metadata(path).artist == "ひとり"


def test_whitespace_is_trimmed(test_audio_dir: Path, tmp_path: Path) -> None:
    """前後の空白は取り除く。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, title=["  余白あり  "], artist=[" 奏者 "])

    metadata = read_track_metadata(path)

    assert metadata.title == "余白あり"
    assert metadata.artist == "奏者"


def test_empty_tags_become_none(test_audio_dir: Path, tmp_path: Path) -> None:
    """空文字だけのタグは None にする。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, title=["   "], artist=["", "  "], album=[""])

    metadata = read_track_metadata(path)

    assert metadata.title is None
    assert metadata.artist is None
    assert metadata.album is None


def test_untagged_file_is_not_a_failure(test_audio_dir: Path, tmp_path: Path) -> None:
    """タグが 1 件も無くても失敗にしない。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.wav")

    metadata = read_track_metadata(path)

    assert metadata.title is None
    assert metadata.artist is None
    assert metadata.album is None


def test_untagged_file_still_has_duration(test_audio_dir: Path, tmp_path: Path) -> None:
    """タグが無くても長さは取得できる。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.wav")

    metadata = read_track_metadata(path)

    assert metadata.duration_ms is not None
    assert metadata.duration_ms == pytest.approx(2000, abs=100)


def test_duration_is_converted_to_milliseconds(test_audio_dir: Path, tmp_path: Path) -> None:
    """秒からミリ秒へ一貫した丸めで変換する。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")

    metadata = read_track_metadata(path)

    assert metadata.duration_ms is not None
    assert metadata.duration_ms > 0
    assert metadata.file_size_bytes == path.stat().st_size
    assert metadata.bitrate_bps is not None
    assert metadata.bitrate_bps > 0


class _StubInfo:
    """``info`` の代わり。Mutagen の StreamInfo は length を実体で持つため、
    型ごとの防御はここで直接確認する。"""

    def __init__(self, length: object) -> None:
        self.length = length


class _StubBitrateInfo:
    def __init__(self, bitrate: object) -> None:
        self.bitrate = bitrate


@pytest.mark.parametrize("length", [math.nan, math.inf, -math.inf, -1.0, "abc", None, True])
def test_invalid_duration_becomes_none(length: object) -> None:
    """NaN・inf・負値・想定外の型は None にする。"""
    assert _duration_ms(_StubInfo(length)) is None


def test_missing_length_attribute_becomes_none() -> None:
    """length を持たない info でも例外にしない。"""
    assert _duration_ms(object()) is None


@pytest.mark.parametrize("bitrate", [math.nan, math.inf, -1, 0, "abc", None, True])
def test_invalid_bitrate_becomes_none(bitrate: object) -> None:
    """正の有限数でないビットレートは不明扱いにする。"""
    assert _bitrate_bps(_StubBitrateInfo(bitrate)) is None


@pytest.mark.parametrize(("seconds", "expected"), [(0, 0), (1.2345, 1234), (2.0, 2000)])
def test_duration_rounding(seconds: float, expected: int) -> None:
    """秒からミリ秒への丸めは一貫している。"""
    assert _duration_ms(_StubInfo(seconds)) == expected


def test_tags_survive_an_unavailable_duration(
    test_audio_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """長さが取れなくても、取得できたタグは捨てない。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.flac")
    write_tags(path, title=["残るタイトル"], artist=["残る奏者"])

    def no_duration(info: object) -> int | None:
        del info
        return None

    monkeypatch.setattr("sdp.core.metadata.reader._duration_ms", no_duration)

    metadata = read_track_metadata(path)

    assert metadata.duration_ms is None
    assert metadata.title == "残るタイトル"
    assert metadata.artist == "残る奏者"


# -- 失敗 -------------------------------------------------------------------


def test_unsupported_file_raises(tmp_path: Path) -> None:
    """未対応の形式は失敗として扱う。"""
    path = tmp_path / "ただのテキスト.txt"
    path.write_text("これは音声ではありません", encoding="utf-8")

    with pytest.raises(MetadataReadError):
        read_track_metadata(path)


def test_corrupted_file_raises(tmp_path: Path) -> None:
    """壊れたファイルは失敗として扱う。"""
    path = tmp_path / "壊れた.mp3"
    path.write_bytes(b"\x00\x01\x02not an mp3")

    with pytest.raises(MetadataReadError):
        read_track_metadata(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    """存在しないファイルは失敗として扱う。"""
    with pytest.raises(MetadataReadError):
        read_track_metadata(tmp_path / "ない曲.mp3")


def test_directory_raises(tmp_path: Path) -> None:
    """ディレクトリも失敗として扱う。"""
    with pytest.raises(MetadataReadError):
        read_track_metadata(tmp_path)


@pytest.mark.parametrize("attribute", ["tags", "info"])
def test_unexpected_extraction_exceptions_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attribute: str
) -> None:
    """属性取得のプログラミングエラーは通常の読取失敗へ変換しない。"""
    path = tmp_path / "壊れたプロパティ.wav"

    class BrokenAudio:
        def __getattribute__(self, name: str) -> object:
            if name == attribute:
                raise RuntimeError(f"{attribute}取得失敗")
            return super().__getattribute__(name)

    def open_broken_audio(requested: Path) -> BrokenAudio:
        del requested
        return BrokenAudio()

    monkeypatch.setattr("sdp.core.metadata.reader._open_audio", open_broken_audio)

    with pytest.raises(RuntimeError, match=f"{attribute}取得失敗"):
        read_track_metadata(path)


def test_reading_does_not_modify_the_file(test_audio_dir: Path, tmp_path: Path) -> None:
    """読み取りで元ファイルを書き換えない。"""
    path = copy_source(test_audio_dir, tmp_path, "sine440.mp3")
    write_tags(path, title=["変わらない"])
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    read_track_metadata(path)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime

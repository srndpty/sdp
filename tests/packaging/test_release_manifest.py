"""リリースmanifestとarchive検査の純粋ロジックを検証する。

実配布物を必要としないよう、ダミーのディレクトリ構造で検査する。
"""

import json
from pathlib import Path

import pytest

from sdp.release_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PackageContents,
    archive_name,
    build_manifest,
    collect_runtime_versions,
    dump_manifest,
    file_sha256,
    find_local_paths,
    normalized_architecture,
    scan_package,
    validate_archive_members,
)

RUNTIME = {
    "python": "3.13.1",
    "pyside6": "6.10.3",
    "qt": "6.10.3",
    "numpy": "2.2.0",
    "mutagen": "1.47.0",
    "pyinstaller": "6.11.1",
    "shiboken6": "6.10.3",
}


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """最小限の配布物に見えるディレクトリを作る。"""
    root = tmp_path / "sdp"
    internal = root / "_internal"
    (internal / "PySide6" / "plugins" / "platforms").mkdir(parents=True)
    (internal / "PySide6" / "plugins" / "multimedia").mkdir(parents=True)
    (root / "sdp.exe").write_bytes(b"MZ\x00\x00")
    (root / "LICENSE").write_text("MIT", encoding="utf-8")
    (internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll").write_bytes(b"q")
    (internal / "PySide6" / "plugins" / "multimedia" / "ffmpegmediaplugin.dll").write_bytes(b"f")
    (internal / "PySide6" / "plugins" / "multimedia" / "windowsmediaplugin.dll").write_bytes(b"w")
    return root


def manifest_of(contents: PackageContents, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "version": "0.0.1",
        "architecture": "x86_64",
        "contents": contents,
        "runtime": RUNTIME,
        "archive_file_name": "sdp-0.0.1-windows-x64.zip",
        "archive_sha256": "a" * 64,
    }
    values.update(overrides)
    return build_manifest(**values)  # type: ignore[arg-type]


# -- archive名とarchitecture -------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [("AMD64", "x86_64"), ("x86_64", "x86_64"), ("ARM64", "arm64"), ("", "unknown")],
)
def test_architecture_names_are_normalized(machine: str, expected: str) -> None:
    """環境ごとの表記ゆれを固定の名前へ揃える。"""
    assert normalized_architecture(machine) == expected


def test_archive_name_contains_version_and_architecture() -> None:
    """archive名にversionとarchitectureを含める。"""
    assert archive_name("0.0.1", "x86_64") == "sdp-0.0.1-windows-x64.zip"
    assert archive_name("1.2.3", "arm64") == "sdp-1.2.3-windows-arm64.zip"


# -- 配布物の走査 -----------------------------------------------------------


def test_scan_counts_files_and_size(package: Path) -> None:
    """ファイル数と合計サイズを集計する。"""
    contents = scan_package(package)

    assert contents.file_count == 5
    assert contents.uncompressed_size == sum(
        item.stat().st_size for item in package.rglob("*") if item.is_file()
    )


def test_scan_collects_load_bearing_plugins(package: Path) -> None:
    """platform／multimedia pluginだけを記録する（DLL全列挙はしない）。"""
    contents = scan_package(package)

    assert contents.platform_plugins == ("qwindows.dll",)
    assert contents.multimedia_plugins == ("ffmpegmediaplugin.dll", "windowsmediaplugin.dll")


def test_content_hash_is_stable_and_ignores_mtime(package: Path, tmp_path: Path) -> None:
    """内容hashは相対pathと中身だけで決まり、mtimeに影響されない。"""
    first = scan_package(package)
    for item in package.rglob("*"):
        if item.is_file():
            item.touch()
    second = scan_package(package)

    assert first.content_sha256 == second.content_sha256

    copied = tmp_path / "copy"
    copied.mkdir()
    for item in sorted(package.rglob("*")):
        target = copied / item.relative_to(package)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    assert scan_package(copied).content_sha256 == first.content_sha256


def test_content_hash_changes_with_content(package: Path) -> None:
    """中身が変わればhashも変わる。"""
    before = scan_package(package).content_sha256
    (package / "sdp.exe").write_bytes(b"MZ\x00\x01")

    assert scan_package(package).content_sha256 != before


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    """SHA-256は標準の計算結果と一致する。"""
    import hashlib

    path = tmp_path / "archive.zip"
    payload = b"sdp release" * 1000
    path.write_bytes(payload)

    assert file_sha256(path) == hashlib.sha256(payload).hexdigest()


# -- manifest ---------------------------------------------------------------


def test_manifest_has_schema_version_and_fixed_key_order(package: Path) -> None:
    """schema versionを持ち、keyの順序が固定される。"""
    manifest = manifest_of(scan_package(package))

    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert list(manifest) == [
        "schema_version",
        "application",
        "version",
        "platform",
        "architecture",
        "packaging",
        "archive",
        "contents",
        "runtime",
        "plugins",
    ]


def test_manifest_records_version_architecture_and_runtime(package: Path) -> None:
    """version・architecture・runtime versionを記録する。"""
    manifest = manifest_of(scan_package(package))

    assert manifest["version"] == "0.0.1"
    assert manifest["architecture"] == "x86_64"
    assert manifest["platform"] == "windows"
    assert manifest["packaging"] == "pyinstaller-onedir"
    assert manifest["runtime"] == dict(sorted(RUNTIME.items()))


def test_manifest_records_the_archive_hash(package: Path) -> None:
    """archive名とSHA-256を記録する。"""
    manifest = manifest_of(scan_package(package))
    archive = manifest["archive"]
    assert isinstance(archive, dict)

    assert archive["file_name"] == "sdp-0.0.1-windows-x64.zip"
    assert archive["sha256"] == "a" * 64


def test_manifest_lists_plugins(package: Path) -> None:
    """load-bearingなpluginを記録する。"""
    manifest = manifest_of(scan_package(package))
    plugins = manifest["plugins"]
    assert isinstance(plugins, dict)

    assert plugins["platforms"] == ["qwindows.dll"]
    assert "ffmpegmediaplugin.dll" in plugins["multimedia"]


def test_manifest_is_stable_for_the_same_input(package: Path) -> None:
    """同じ入力なら完全に同じJSONになる（timestampを持たない）。"""
    contents = scan_package(package)

    assert dump_manifest(manifest_of(contents)) == dump_manifest(manifest_of(contents))


def test_manifest_round_trips_as_utf8_json(package: Path, tmp_path: Path) -> None:
    """UTF-8 JSONとして往復できる。"""
    manifest = manifest_of(scan_package(package))
    path = tmp_path / "sdp.manifest.json"

    path.write_text(dump_manifest(manifest), encoding="utf-8")

    assert json.loads(path.read_text(encoding="utf-8")) == manifest


def test_manifest_has_no_local_paths(package: Path) -> None:
    """絶対path・UNC path・ユーザー名を含めない。"""
    manifest = manifest_of(scan_package(package))

    assert find_local_paths(manifest) == ()
    text = dump_manifest(manifest)
    assert str(package) not in text
    assert "Users" not in text


@pytest.mark.parametrize(
    "value",
    [
        {"runtime": {"python": "C:\\Python313\\python.exe"}},
        {"archive": {"file_name": "\\\\server\\share\\sdp.zip"}},
        {"plugins": {"platforms": ["/home/user/qwindows.dll"]}},
    ],
)
def test_local_paths_are_detected(value: dict[str, object]) -> None:
    """混入したローカルpathを検出できる。"""
    assert find_local_paths(value)


def test_collect_runtime_versions_uses_the_build_environment() -> None:
    """実際のbuild環境からversionを取得する。"""
    runtime = collect_runtime_versions("6.10.3")

    assert runtime["qt"] == "6.10.3"
    assert runtime["python"].count(".") == 2
    for name in ("pyside6", "numpy", "mutagen"):
        assert runtime[name] != "unknown"


def test_missing_qt_version_is_marked_unknown() -> None:
    """Qt versionを取得できない場合も推測しない。"""
    assert collect_runtime_versions(None)["qt"] == "unknown"


# -- archiveメンバー ---------------------------------------------------------


def test_valid_archive_members_pass() -> None:
    """単一rootに収まるメンバーは正常。"""
    members = ["sdp/", "sdp/sdp.exe", "sdp/_internal/Qt6Core.dll", "sdp/LICENSE"]

    assert validate_archive_members(members) == ()


@pytest.mark.parametrize(
    "member",
    ["../evil.txt", "sdp/../../evil.txt", "/absolute.txt", "other/sdp.exe", "sdp2/sdp.exe"],
)
def test_unsafe_or_extra_roots_are_rejected(member: str) -> None:
    """traversalとroot外メンバーを拒否する。"""
    assert validate_archive_members(["sdp/sdp.exe", member])


def test_missing_root_is_reported() -> None:
    """root directoryが無いarchiveを検出する。"""
    assert validate_archive_members([])


def test_backslash_members_are_normalized() -> None:
    """区切り文字が`\\`でも同じ判定になる。"""
    assert validate_archive_members(["sdp\\sdp.exe", "sdp\\_internal\\Qt6Core.dll"]) == ()

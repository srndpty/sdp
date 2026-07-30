"""ZIPリリースに添えるmanifestの生成（Qt非依存の純粋ロジック）。

manifestは「配布物へ何が入っていて、どのruntimeでbuildしたか」を後から確認する
ためのもので、**環境固有の情報を持ち込まない**。

- username・home・repositoryの絶対pathを含めない
- keyの順序を固定し、同じ入力なら同じJSONになる
- ファイル列挙はlocale非依存の順序（POSIX相対path昇順）で行う
"""

import hashlib
import json
import platform
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, cast

MANIFEST_SCHEMA_VERSION = 1
PACKAGING_KIND = "pyinstaller-onedir"

_CHUNK_SIZE = 1024 * 1024
_ARCHITECTURE_ALIASES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64"}
_RUNTIME_DISTRIBUTIONS = ("PySide6", "shiboken6", "numpy", "mutagen", "pyinstaller")
_PLATFORM_PLUGIN_NAMES = ("qwindows.dll",)
_MULTIMEDIA_PLUGIN_PATTERN = re.compile(r"mediaplugin\.dll$", re.IGNORECASE)
# ユーザー名やrepository pathの混入を機械的に見つけるための手掛かり。
_LOCAL_PATH_PATTERN = re.compile(r"(^[A-Za-z]:[\\/])|(^\\\\)|(^/(home|Users)/)")


@dataclass(frozen=True, slots=True)
class PackageContents:
    """配布ディレクトリを走査した結果（pathそのものは持ち出さない）。"""

    file_count: int
    uncompressed_size: int
    content_sha256: str
    platform_plugins: tuple[str, ...]
    multimedia_plugins: tuple[str, ...]


def normalized_architecture(machine: str | None = None) -> str:
    """`platform.machine()` の表記ゆれを固定の名前へ揃える。"""
    raw = (machine if machine is not None else platform.machine()).strip().lower()
    return _ARCHITECTURE_ALIASES.get(raw, raw or "unknown")


def archive_name(version: str, architecture: str) -> str:
    """`sdp-0.0.1-windows-x64.zip` 形式のarchive名を返す。"""
    suffix = "x64" if architecture == "x86_64" else architecture
    return f"sdp-{version}-windows-{suffix}.zip"


def file_sha256(path: Path) -> str:
    """ファイル1件のSHA-256（大きなarchiveでも一定メモリで計算する）。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def scan_package(directory: Path) -> PackageContents:
    """配布ディレクトリのファイル数・合計サイズ・内容hash・pluginを集める。

    ``content_sha256`` は「相対pathと中身」だけから決まるため、mtimeやZIPの
    圧縮方法に左右されない。同一commit・同一環境での再現性確認に使う。
    """
    root = directory.resolve()
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    total_size = 0
    platform_plugins: list[str] = []
    multimedia_plugins: list[str] = []
    for item in files:
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha256(item).encode("ascii"))
        digest.update(b"\0")
        total_size += item.stat().st_size
        name = item.name
        if name.lower() in _PLATFORM_PLUGIN_NAMES:
            platform_plugins.append(name)
        elif _MULTIMEDIA_PLUGIN_PATTERN.search(name):
            multimedia_plugins.append(name)
    return PackageContents(
        file_count=len(files),
        uncompressed_size=total_size,
        content_sha256=digest.hexdigest(),
        platform_plugins=tuple(sorted(platform_plugins)),
        multimedia_plugins=tuple(sorted(multimedia_plugins)),
    )


def compare_package_contents(
    expected: PackageContents,
    actual: PackageContents,
    *,
    expected_label: str = "expected",
    actual_label: str = "actual",
) -> tuple[str, ...]:
    """2つの配布ディレクトリ走査結果が一致するか検査する。

    release pipelineではZIP化前の ``dist/sdp`` と、ZIPを展開した ``sdp/`` を比べる。
    file数・非圧縮サイズ・content hashを別々に報告することで、欠落と内容改変を
    切り分けやすくする。
    """
    failures: list[str] = []
    if expected.file_count != actual.file_count:
        failures.append(
            f"file_countが一致しません: {expected_label}={expected.file_count} "
            f"{actual_label}={actual.file_count}"
        )
    if expected.uncompressed_size != actual.uncompressed_size:
        failures.append(
            f"uncompressed_sizeが一致しません: {expected_label}={expected.uncompressed_size} "
            f"{actual_label}={actual.uncompressed_size}"
        )
    if expected.content_sha256 != actual.content_sha256:
        failures.append(
            f"content_sha256が一致しません: {expected_label}={expected.content_sha256} "
            f"{actual_label}={actual.content_sha256}"
        )
    return tuple(failures)


def collect_runtime_versions(qt_version: str | None = None) -> dict[str, str]:
    """build環境の実際のruntime versionを集める（取得できない場合は ``unknown``）。"""
    versions = {"python": platform.python_version()}
    for name in _RUNTIME_DISTRIBUTIONS:
        try:
            versions[name.lower()] = distribution_version(name)
        except PackageNotFoundError:
            versions[name.lower()] = "unknown"
    versions["qt"] = qt_version if qt_version else "unknown"
    return versions


def build_manifest(
    *,
    version: str,
    architecture: str,
    contents: PackageContents,
    runtime: Mapping[str, str],
    archive_file_name: str,
    archive_sha256: str,
) -> dict[str, Any]:
    """key順を固定したmanifest文書を組み立てる。

    タイムスタンプは含めない。同一commit・同一環境で2回buildしたとき、
    manifestが完全一致することを再現性の確認に使うため。
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "application": "sdp",
        "version": version,
        "platform": "windows",
        "architecture": architecture,
        "packaging": PACKAGING_KIND,
        "archive": {"file_name": archive_file_name, "sha256": archive_sha256},
        "contents": {
            "file_count": contents.file_count,
            "uncompressed_size": contents.uncompressed_size,
            "content_sha256": contents.content_sha256,
        },
        "runtime": {name: runtime[name] for name in sorted(runtime)},
        "plugins": {
            "platforms": list(contents.platform_plugins),
            "multimedia": list(contents.multimedia_plugins),
        },
    }


def dump_manifest(manifest: Mapping[str, Any]) -> str:
    """manifestをUTF-8のJSON文字列へ整形する（keyの順序は保つ）。"""
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def find_local_paths(value: object, *, path: str = "") -> tuple[str, ...]:
    """manifestに絶対path・UNC pathが混ざっていないか検査する。"""
    findings: list[str] = []
    if isinstance(value, Mapping):
        mapping = cast("Mapping[str, object]", value)
        for key, item in mapping.items():
            findings.extend(find_local_paths(item, path=f"{path}.{key}"))
    elif isinstance(value, str):
        if _LOCAL_PATH_PATTERN.search(value):
            findings.append(f"{path or 'root'}: {value}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = cast("Sequence[object]", value)
        for index, item in enumerate(items):
            findings.extend(find_local_paths(item, path=f"{path}[{index}]"))
    return tuple(findings)


def validate_archive_members(members: Iterable[str], *, root: str = "sdp") -> tuple[str, ...]:
    """ZIP内のメンバー名が単一rootに収まり、traversalを含まないか検査する。"""
    failures: list[str] = []
    seen_root = False
    for member in members:
        normalized = member.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            failures.append(f"安全でないメンバーです: {member}")
            continue
        head = normalized.split("/", 1)[0]
        if head != root:
            failures.append(f"root directory以外のメンバーです: {member}")
            continue
        seen_root = True
    if not seen_root:
        failures.append(f"root directoryが見つかりません: {root}/")
    return tuple(failures)

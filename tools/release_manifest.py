"""ZIPリリースのmanifestを生成する（build scriptから呼ぶ薄いCLI）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import qVersion

from sdp import __version__
from sdp.release_manifest import (
    build_manifest,
    collect_runtime_versions,
    dump_manifest,
    file_sha256,
    find_local_paths,
    normalized_architecture,
    scan_package,
)


def main(argv: list[str] | None = None) -> int:
    """配布ディレクトリとZIPからmanifestを書き出す。"""
    parser = argparse.ArgumentParser(description="sdp リリースmanifestを生成します。")
    parser.add_argument("package_directory", type=Path, help="ZIP化した配布ディレクトリ")
    parser.add_argument("archive", type=Path, help="生成済みZIP archive")
    parser.add_argument("output", type=Path, help="manifestの書き出し先")
    arguments = parser.parse_args(argv)

    contents = scan_package(arguments.package_directory)
    manifest = build_manifest(
        version=__version__,
        architecture=normalized_architecture(),
        contents=contents,
        runtime=collect_runtime_versions(qVersion()),
        archive_file_name=arguments.archive.name,
        archive_sha256=file_sha256(arguments.archive),
    )
    local_paths = find_local_paths(manifest)
    if local_paths:
        for finding in local_paths:
            print(f"エラー: manifestへローカルpathが混入しています: {finding}")
        return 1

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(dump_manifest(manifest), encoding="utf-8")
    print(f"manifestを生成しました: {arguments.output.name}")
    print(
        f"  version={manifest['version']} files={contents.file_count} "
        f"size={contents.uncompressed_size / 1024 / 1024:.1f}MiB"
    )
    print(f"  content_sha256={contents.content_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

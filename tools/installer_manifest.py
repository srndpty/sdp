"""installer成果物のmanifestを生成する（build scriptから呼ぶ薄いCLI）。

AppId・ProgID・関連付け拡張子は`packaging/installer.iss`から読み取る
（installer scriptをsource of truthにし、値を二重管理しない）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sdp import __version__
from sdp.inno_script import parse_inno_script
from sdp.installer_contract import app_id, file_associations, prog_id
from sdp.installer_manifest import build_installer_manifest, dump_installer_manifest
from sdp.release_manifest import file_sha256, find_local_paths, normalized_architecture

_DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"


def main(argv: list[str] | None = None) -> int:
    """setup exeからinstaller manifestを書き出す。"""
    parser = argparse.ArgumentParser(description="sdp installerのmanifestを生成します。")
    parser.add_argument("setup_executable", type=Path, help="生成済みのsetup exe")
    parser.add_argument("output", type=Path, help="manifestの書き出し先")
    parser.add_argument("--installer-script", type=Path, default=_DEFAULT_SCRIPT)
    arguments = parser.parse_args(argv)

    script = parse_inno_script(arguments.installer_script.read_text(encoding="utf-8"))
    manifest = build_installer_manifest(
        version=__version__,
        architecture=normalized_architecture(),
        app_id=app_id(script),
        prog_id=prog_id(script),
        file_associations=file_associations(script),
        setup_file_name=arguments.setup_executable.name,
        setup_sha256=file_sha256(arguments.setup_executable),
        setup_size=arguments.setup_executable.stat().st_size,
    )
    local_paths = find_local_paths(manifest)
    if local_paths:
        for finding in local_paths:
            print(f"エラー: manifestへローカルpathが混入しています: {finding}")
        return 1

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(dump_installer_manifest(manifest), encoding="utf-8")
    setup = manifest["setup"]
    print(f"installer manifestを生成しました: {arguments.output.name}")
    associations = len(manifest["file_associations"])
    print(f"  version={manifest['version']} scope={manifest['scope']} 関連付け{associations}件")
    print(f"  sha256={setup['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

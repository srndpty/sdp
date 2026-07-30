"""PyInstaller onedir配布物の構造を検査する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from sdp.package_layout import validate_package_layout


def main(argv: list[str] | None = None) -> int:
    """コマンドラインから構造を検査する。"""
    parser = argparse.ArgumentParser(description="sdp onedir配布物の構造を検査します。")
    parser.add_argument("package_directory", type=Path)
    arguments = parser.parse_args(argv)

    failures = validate_package_layout(arguments.package_directory)
    if failures:
        for failure in failures:
            print(f"エラー: {failure}")
        return 1

    print(f"配布物の構造は正常です: {arguments.package_directory.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

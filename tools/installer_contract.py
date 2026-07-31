"""Inno Setup scriptの契約を検査する（build scriptと手動確認から呼ぶCLI）。

Inno Setup compilerが無い環境でも実行できる。compile前のゲートとして使う。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sdp.inno_script import parse_inno_script
from sdp.installer_contract import (
    app_id,
    file_associations,
    install_directory,
    prog_id,
    validate_installer_contract,
)

_DEFAULT_SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "installer.iss"


def main(argv: list[str] | None = None) -> int:
    """installer.issがP7-Cの契約を満たすかを検査する。"""
    parser = argparse.ArgumentParser(description="sdp installerの契約を検査します。")
    parser.add_argument("script", type=Path, nargs="?", default=_DEFAULT_SCRIPT)
    arguments = parser.parse_args(argv)

    script = parse_inno_script(arguments.script.read_text(encoding="utf-8"))
    failures = validate_installer_contract(script)
    if failures:
        for failure in failures:
            print(f"エラー: {failure}")
        return 1

    print(f"installerの契約は正常です: {arguments.script.name}")
    print(f"  AppId={app_id(script)} ProgID={prog_id(script)}")
    print(f"  install先={install_directory(script)}（per-user / 昇格なし）")
    print(f"  関連付け={' '.join(file_associations(script))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""配布物のライセンス資料を検査する（build scriptと手動確認から呼ぶCLI）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from sdp.license_audit import (
    classify_runtime_files,
    find_missing_texts,
    load_license_manifest,
    summarize,
    summarize_inventory,
    unresolved_components,
)

_DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "packaging" / "licenses-manifest.json"


def main(argv: list[str] | None = None) -> int:
    """宣言済みライセンス原文の同梱と、未解決事項の一覧を報告する。"""
    parser = argparse.ArgumentParser(description="sdp配布物のライセンス資料を検査します。")
    parser.add_argument("package_directory", type=Path)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument(
        "--fail-on-unclassified",
        action="store_true",
        help="どのコンポーネントにも属さないDLL・pydがあれば失敗にする（公開ゲート向け）。",
    )
    arguments = parser.parse_args(argv)

    components = load_license_manifest(arguments.manifest)
    missing = find_missing_texts(components, arguments.package_directory)
    unresolved = unresolved_components(components)
    inventory = classify_runtime_files(components, arguments.package_directory)

    print(summarize(components))
    for component in unresolved:
        print(f"未解決 [{component.status.label}] {component.display_name}: {component.notes}")

    print(summarize_inventory(inventory))
    for relative in inventory.unclassified:
        print(
            "警告: どのコンポーネントにも属さないruntimeファイルです（manifestへの宣言が必要な"
            f"可能性があります）: {relative}"
        )

    if missing:
        for entry in missing:
            print(f"エラー: 宣言したライセンス原文が配布物にありません: {entry}")
        return 1
    if arguments.fail_on_unclassified and inventory.unclassified:
        print("エラー: 未分類のruntimeファイルが残っています（--fail-on-unclassified）。")
        return 1
    if unresolved:
        print(
            "宣言済みの原文はすべて同梱されています。"
            "ただし未解決事項が残るため、外部配布可能とは判断できません。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""配布物のライセンス資料検査を検証する。

実際の`packaging/licenses-manifest.json`が読めること、宣言した原文の欠落を
検出できること、未解決事項を「解決済み」と誤って扱わないことを固定する。
"""

import json
from pathlib import Path

import pytest

from sdp.license_audit import (
    LICENSE_MANIFEST_SCHEMA_VERSION,
    ComponentStatus,
    find_missing_texts,
    load_license_manifest,
    summarize,
    unresolved_components,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "packaging" / "licenses-manifest.json"


def write_manifest(path: Path, components: list[dict[str, object]]) -> Path:
    document = {"schema_version": LICENSE_MANIFEST_SCHEMA_VERSION, "components": components}
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def component(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "example",
        "display_name": "Example",
        "license": "MIT",
        "shipped_texts": ["LICENSE"],
        "status": "resolved",
        "notes": "",
    }
    values.update(overrides)
    return values


# -- 実manifest -------------------------------------------------------------


def test_repository_manifest_loads() -> None:
    """リポジトリのライセンスmanifestが契約どおり読める。"""
    components = load_license_manifest(MANIFEST_PATH)

    assert len(components) >= 8
    identifiers = {item.identifier for item in components}
    for expected in ("sdp", "python", "pyside6", "qt", "ffmpeg", "mutagen", "msvc-runtime"):
        assert expected in identifiers, expected


def test_repository_manifest_declares_runtime_reality() -> None:
    """実際に同梱しているruntimeを漏れなく宣言している。"""
    components = {item.identifier: item for item in load_license_manifest(MANIFEST_PATH)}

    assert "LGPL" in components["pyside6"].license_name
    assert "LGPL-2.1" in components["ffmpeg"].license_name
    assert components["mutagen"].license_name.startswith("GPL-2.0-or-later")
    assert components["pyinstaller-bootloader"].status is ComponentStatus.RESOLVED


def test_repository_manifest_still_has_unresolved_items() -> None:
    """外部配布ブロッカーを「解決済み」と誤記していない。

    LGPL原文の同梱やQtの配布形態が決まるまでは未解決として扱う。
    """
    unresolved = unresolved_components(load_license_manifest(MANIFEST_PATH))

    assert unresolved, "未解決が0件になったら、根拠を添えてこのテストを更新すること"
    assert {item.identifier for item in unresolved} >= {"pyside6", "qt", "ffmpeg", "mutagen"}


def test_declared_texts_exist_in_the_real_package() -> None:
    """dist/sdpがある場合、宣言した原文が実際に同梱されている。"""
    package = REPO_ROOT / "dist" / "sdp"
    if not package.exists():
        pytest.skip("配布物未ビルド")

    assert find_missing_texts(load_license_manifest(MANIFEST_PATH), package) == ()


# -- 検査ロジック -----------------------------------------------------------


def test_missing_text_is_reported(tmp_path: Path) -> None:
    """宣言した原文が無ければ機械的な不備として報告する。"""
    manifest = write_manifest(tmp_path / "m.json", [component(shipped_texts=["LICENSE"])])
    package = tmp_path / "package"
    package.mkdir()

    missing = find_missing_texts(load_license_manifest(manifest), package)

    assert missing == ("Example: LICENSE",)


def test_present_text_passes(tmp_path: Path) -> None:
    """原文があれば不備なしとする。"""
    manifest = write_manifest(tmp_path / "m.json", [component()])
    package = tmp_path / "package"
    package.mkdir()
    (package / "LICENSE").write_text("MIT", encoding="utf-8")

    assert find_missing_texts(load_license_manifest(manifest), package) == ()


@pytest.mark.parametrize(
    "status",
    ["needs_text", "needs_decision", "needs_expert"],
)
def test_unresolved_statuses_are_listed(tmp_path: Path, status: str) -> None:
    """未解決の3分類はいずれも未解決として集計する。"""
    manifest = write_manifest(tmp_path / "m.json", [component(status=status)])

    unresolved = unresolved_components(load_license_manifest(manifest))

    assert len(unresolved) == 1
    assert unresolved[0].status.label


def test_resolved_status_is_not_listed(tmp_path: Path) -> None:
    """解決済みは未解決へ数えない。"""
    manifest = write_manifest(tmp_path / "m.json", [component(status="resolved")])

    assert unresolved_components(load_license_manifest(manifest)) == ()


def test_summary_counts_every_status(tmp_path: Path) -> None:
    """要約に全分類の件数が入る。"""
    manifest = write_manifest(
        tmp_path / "m.json",
        [component(id="a"), component(id="b", status="needs_text")],
    )

    summary = summarize(load_license_manifest(manifest))

    assert "コンポーネント2件" in summary
    assert "解決済み=1" in summary
    assert "文書追加で解決可能=1" in summary


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 99, "components": []},
        {"components": [component()]},
        {"schema_version": 1, "components": {}},
        {"schema_version": 1, "components": []},
    ],
)
def test_invalid_manifest_is_rejected(tmp_path: Path, document: dict[str, object]) -> None:
    """schema versionと構造の不正を既定値へ丸めず失敗させる。"""
    path = tmp_path / "m.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_license_manifest(path)


@pytest.mark.parametrize(
    "invalid",
    [
        component(status="unknown"),
        component(id=""),
        component(shipped_texts="LICENSE"),
        component(license=None),
    ],
)
def test_invalid_component_is_rejected(tmp_path: Path, invalid: dict[str, object]) -> None:
    """component単位の不正も失敗させる。"""
    manifest = write_manifest(tmp_path / "m.json", [invalid])

    with pytest.raises(ValueError):
        load_license_manifest(manifest)


def test_duplicate_component_ids_are_rejected(tmp_path: Path) -> None:
    """同じidを二重に宣言できない。"""
    manifest = write_manifest(tmp_path / "m.json", [component(), component()])

    with pytest.raises(ValueError, match="重複"):
        load_license_manifest(manifest)

"""設計文書と実装のずれを機械的に検出する。

古い契約が文書に残っていると、次の実装時にそれを正として変更されてしまう。
文章の良し悪しは見ず、「実在しないモジュールを書いていないか」「新しい
モジュールを書き漏らしていないか」「schema versionの記載が実装と合っているか」
という、機械的に判定できる点だけを検査する。
"""

import re
from pathlib import Path

import pytest

from sdp.services.settings import SETTINGS_SCHEMA_VERSION
from sdp.services.ui_state import UI_STATE_SCHEMA_VERSION

_REPO_ROOT = Path(__file__).parents[2]
_ARCHITECTURE = _REPO_ROOT / "docs" / "architecture.md"
_README = _REPO_ROOT / "README.md"
_SOURCE_ROOT = _REPO_ROOT / "src" / "sdp"

# 構成図へ列挙する対象（責務の入口になるpackage）。
_DOCUMENTED_PACKAGES = ("services", "ui")


def documented_modules() -> set[str]:
    """architecture.mdの構成図に現れる ``*.py`` の名前。"""
    text = _ARCHITECTURE.read_text(encoding="utf-8")
    return set(re.findall(r"([a-z0-9_]+\.py)", text))


@pytest.mark.parametrize("package", _DOCUMENTED_PACKAGES)
def test_architecture_lists_every_module(package: str) -> None:
    """新しいモジュールを構成図へ書き漏らしていない。"""
    actual = {
        path.name for path in (_SOURCE_ROOT / package).glob("*.py") if path.name != "__init__.py"
    }

    missing = sorted(actual - documented_modules())

    assert missing == [], f"architecture.mdの構成図に未記載のモジュールがあります: {missing}"


def test_architecture_does_not_list_removed_modules() -> None:
    """存在しないモジュールを構成図へ残していない。"""
    existing = {
        path.name
        for root in (_SOURCE_ROOT, _REPO_ROOT / "tools", _REPO_ROOT / "tests")
        for path in root.rglob("*.py")
    }
    # 構成図以外の本文にも .py 名は現れるため、実在確認だけを行う。
    stale = sorted(name for name in documented_modules() if name not in existing)

    assert stale == [], f"architecture.mdが実在しないモジュールを記載しています: {stale}"


def test_readme_saved_values_table_matches_the_schema_versions() -> None:
    """READMEの「保存される項目」が実装のschema versionと一致する。"""
    readme = _README.read_text(encoding="utf-8")
    section = readme.split("### 保存される項目", 1)
    assert len(section) == 2, "READMEに「保存される項目」の一覧がありません"
    table = section[1].split("##", 1)[0]

    assert f"version {SETTINGS_SCHEMA_VERSION}" in table
    assert f"version {UI_STATE_SCHEMA_VERSION}" in table
    # 保存しないものを「保存する」と読める記載を残さない。
    assert "**保存しない**" in table
    for not_saved in ("再生位置", "選択行"):
        assert not_saved in table


def test_readme_does_not_claim_unimplemented_features_as_missing() -> None:
    """実装済みの機能を「未実装」欄へ残していない。"""
    readme = _README.read_text(encoding="utf-8")
    unimplemented = readme.split("**未実装**:", 1)[1].split("\n\n", 1)[0]

    for implemented in ("インストーラー", "ファイル関連付け", "アプリアイコン"):
        assert implemented not in unimplemented, (
            f"実装済みの機能が未実装として記載されています: {implemented}"
        )

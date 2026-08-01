"""設計文書と実装のずれを機械的に検出する。

古い契約が文書に残っていると、次の実装時にそれを正として変更されてしまう。
文章の良し悪しは見ず、「実在しないモジュールを書いていないか」「新しい
モジュールを書き漏らしていないか」「schema versionの記載が実装と合っているか」
という、機械的に判定できる点だけを検査する。
"""

import re
import tomllib
from pathlib import Path

import pytest

from sdp.services.settings import SETTINGS_SCHEMA_VERSION
from sdp.services.ui_state import UI_STATE_SCHEMA_VERSION

_REPO_ROOT = Path(__file__).parents[2]
_ARCHITECTURE = _REPO_ROOT / "docs" / "architecture.md"
_README = _REPO_ROOT / "README.md"
_SOURCE_ROOT = _REPO_ROOT / "src" / "sdp"
_TESTING_STRATEGY = _REPO_ROOT / "docs" / "testing-strategy.md"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_LICENSE_MANIFEST = _REPO_ROOT / "packaging" / "licenses-manifest.json"

# 構成図へ列挙する対象（責務の入口になるpackage）。
_DOCUMENTED_PACKAGES = ("services", "ui")


def _source_packages() -> set[str]:
    """``src/sdp`` 直下のpackage名。構成図parserのdirectory誤検出を防ぐ。"""
    return {
        path.name
        for path in _SOURCE_ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }


def documented_modules() -> set[str]:
    """architecture.mdの構成図が示す、``src/sdp`` からの相対path。

    basenameだけで比べると、``services/foo.py`` の記載漏れを ``ui/foo.py`` の
    記載が隠してしまう。構成図はpackageごとの節に分かれているので、
    直前に現れたpackage名と組み合わせて相対pathへ復元する。

    直前の ``xxx/`` は ``src/sdp`` 直下に実在するpackageのときだけpackageとして
    採用する。構成図外の本文にある ``docs/`` などを誤ってpackageにしない。
    """
    packages = _source_packages()
    documented: set[str] = set()
    package = ""
    for line in _ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        directory = re.search(r"([a-z0-9_]+)/\s*$", line)
        if directory is not None:
            token = directory.group(1)
            package = token if token in packages else ""
            continue
        module = re.search(r"([a-z0-9_]+\.py)", line)
        if module is not None:
            documented.add(f"{package}/{module.group(1)}" if package else module.group(1))
    return documented


def documented_module_names() -> set[str]:
    """相対pathの末尾（実在確認用）。"""
    return {path.rsplit("/", 1)[-1] for path in documented_modules()}


@pytest.mark.parametrize("package", _DOCUMENTED_PACKAGES)
def test_architecture_lists_every_module(package: str) -> None:
    """新しいモジュールを構成図へ書き漏らしていない。"""
    actual = {
        f"{package}/{path.name}"
        for path in (_SOURCE_ROOT / package).glob("*.py")
        if path.name != "__init__.py"
    }

    missing = sorted(actual - documented_modules())

    assert missing == [], f"architecture.mdの構成図に未記載のモジュールがあります: {missing}"


def test_architecture_does_not_list_removed_modules() -> None:
    """存在しないモジュールを構成図へ残していない。

    package付きの記載（``services/foo.py`` など）は **完全な相対path** で実在を
    確認する。basenameだけで比べると、``services/foo.py`` を消しても
    ``tests/foo.py`` が残っていれば見逃してしまう。

    package未特定の記載（構成図外の本文で触れる ``tools/gen_app_icon.py`` など）は
    どのpackageの話か決められないため、repository内のどこかに実在すればよい。
    """
    other_roots = (_REPO_ROOT / "tools", _REPO_ROOT / "tests", _REPO_ROOT / "spike")
    stale: list[str] = []
    for relative in documented_modules():
        if "/" in relative:
            if not (_SOURCE_ROOT / relative).is_file():
                stale.append(relative)
            continue
        found = any(_SOURCE_ROOT.rglob(relative)) or any(
            any(root.rglob(relative)) for root in other_roots if root.is_dir()
        )
        if not found:
            stale.append(relative)

    assert sorted(stale) == [], (
        f"architecture.mdが実在しないモジュールを記載しています: {sorted(stale)}"
    )


def test_testing_strategy_matches_the_audio_ci_jobs() -> None:
    """testing-strategy.mdのCI記述が、実際のworkflowのjob IDと一致する。"""
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    strategy = _TESTING_STRATEGY.read_text(encoding="utf-8")

    job_ids = set(re.findall(r"^  ([a-z0-9-]+):$", workflow, flags=re.MULTILINE))
    assert {"audio-codec-check", "audio-device-observation"} <= job_ids
    # 分割前の単一ジョブ名を文書へ残していない。
    assert "audio-observation" not in strategy
    for job in ("audio-codec-check", "audio-device-observation"):
        assert job in strategy, f"testing-strategy.mdがCIジョブ {job} を記載していません"


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


def test_project_license_is_consistently_gpl3() -> None:
    """metadata・README・配布manifest・Windows resourceのlicense表記を一致させる。"""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = _LICENSE_MANIFEST.read_text(encoding="utf-8")
    version_info = (_REPO_ROOT / "packaging" / "windows-version-info.txt").read_text(
        encoding="utf-8"
    )

    assert pyproject["project"]["license"] == "GPL-3.0-only"
    assert "GPL-3.0-only" in _README.read_text(encoding="utf-8")
    assert '"license": "GPL-3.0-only"' in manifest
    assert "GPL-3.0-only" in version_info

"""配布物のライセンス資料が揃っているかを機械的に検査する（Qt非依存）。

`packaging/licenses-manifest.json` に「配布物へ実際に含まれるコンポーネント」と
「同梱しているライセンス原文」を宣言し、次の2種類を区別して報告する。

- **error**: 宣言した原文が配布物に無い（機械的な不備。修正しないと配布物が壊れている）
- **unresolved**: 原文の追加や配布形態の判断がまだ必要（人が決める事項）

このモジュールは法務判断をしない。「未解決が0件だから配布可能」とも結論づけない。

加えて、配布物の主要runtimeファイル（DLL・pyd）をmanifestの ``file_patterns`` で
コンポーネントへ分類し、どのコンポーネントにも属さないファイルを ``unclassified``
として洗い出す。PyInstaller hookの変更で新しいDLL・pluginが同梱されても、manifestへ
宣言し忘れていれば検知できるようにするためのもの（完全な自動ライセンス判定はしない）。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, cast

LICENSE_MANIFEST_SCHEMA_VERSION = 1

# inventoryで分類対象とする主要runtimeファイル（実行時にロードされる二進物だけを見る）。
_INVENTORY_SUFFIXES = frozenset({".dll", ".pyd"})


class ComponentStatus(Enum):
    """外部配布に向けた解決状況。"""

    RESOLVED = "resolved"
    NEEDS_TEXT = "needs_text"
    NEEDS_DECISION = "needs_decision"
    NEEDS_EXPERT = "needs_expert"

    @property
    def is_resolved(self) -> bool:
        return self is ComponentStatus.RESOLVED

    @property
    def label(self) -> str:
        return {
            ComponentStatus.RESOLVED: "解決済み",
            ComponentStatus.NEEDS_TEXT: "文書追加で解決可能",
            ComponentStatus.NEEDS_DECISION: "配布形態の判断が必要",
            ComponentStatus.NEEDS_EXPERT: "外部専門家確認推奨",
        }[self]


@dataclass(frozen=True, slots=True)
class LicenseComponent:
    """配布物に含まれる1コンポーネントのライセンス状況。"""

    identifier: str
    display_name: str
    license_name: str
    shipped_texts: tuple[str, ...]
    status: ComponentStatus
    notes: str
    file_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    """配布物の主要runtimeファイルをコンポーネントへ割り当てた結果。"""

    classified: tuple[tuple[str, str], ...]
    unclassified: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.classified) + len(self.unclassified)


def load_license_manifest(path: Path) -> tuple[LicenseComponent, ...]:
    """ライセンスmanifestを読み込む（不正な内容は :class:`ValueError`）。"""
    document = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    version = document.get("schema_version")
    if version != LICENSE_MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"未対応のライセンスmanifest schema_versionです: {version!r}")
    raw_components = document.get("components")
    if type(raw_components) is not list:
        raise ValueError("componentsが配列ではありません")

    components: list[LicenseComponent] = []
    seen: set[str] = set()
    for raw in cast("list[Any]", raw_components):
        if type(raw) is not dict:
            raise ValueError(f"componentがオブジェクトではありません: {raw!r}")
        entry = cast("dict[str, Any]", raw)
        identifier = _required_string(entry, "id")
        if identifier in seen:
            raise ValueError(f"componentのidが重複しています: {identifier}")
        seen.add(identifier)
        try:
            status = ComponentStatus(_required_string(entry, "status"))
        except ValueError as error:
            raise ValueError(f"{identifier}のstatusが不正です: {entry.get('status')!r}") from error
        texts = entry.get("shipped_texts", [])
        if not isinstance(texts, list) or any(not isinstance(item, str) for item in texts):  # pyright: ignore[reportUnknownVariableType]
            raise ValueError(f"{identifier}のshipped_textsが文字列配列ではありません")
        patterns = entry.get("file_patterns", [])
        if not isinstance(patterns, list) or any(not isinstance(item, str) for item in patterns):  # pyright: ignore[reportUnknownVariableType]
            raise ValueError(f"{identifier}のfile_patternsが文字列配列ではありません")
        components.append(
            LicenseComponent(
                identifier=identifier,
                display_name=_required_string(entry, "display_name"),
                license_name=_required_string(entry, "license"),
                shipped_texts=tuple(cast("list[str]", texts)),
                status=status,
                notes=str(entry.get("notes", "")),
                file_patterns=tuple(cast("list[str]", patterns)),
            )
        )
    if not components:
        raise ValueError("componentsが空です")
    return tuple(components)


def find_missing_texts(
    components: Sequence[LicenseComponent], package_directory: Path
) -> tuple[str, ...]:
    """宣言済みのライセンス原文が配布物に存在するかを確認する。"""
    root = package_directory.resolve()
    missing: list[str] = []
    for component in components:
        for relative in component.shipped_texts:
            if not (root / relative).is_file():
                missing.append(f"{component.display_name}: {relative}")
    return tuple(missing)


def unresolved_components(
    components: Sequence[LicenseComponent],
) -> tuple[LicenseComponent, ...]:
    """外部配布前に人が決める必要が残っているコンポーネント。"""
    return tuple(component for component in components if not component.status.is_resolved)


def summarize(components: Sequence[LicenseComponent]) -> str:
    """状況の要約（レポートとログ用）。"""
    counts = dict.fromkeys(ComponentStatus, 0)
    for component in components:
        counts[component.status] += 1
    parts = [f"{status.label}={counts[status]}" for status in ComponentStatus]
    return f"コンポーネント{len(components)}件: " + " / ".join(parts)


def classify_runtime_files(
    components: Sequence[LicenseComponent], package_directory: Path
) -> RuntimeInventory:
    """配布物のDLL・pydを ``file_patterns`` でコンポーネントへ割り当てる。

    どのコンポーネントにも属さないファイルは ``unclassified`` として返す。複数の
    patternが一致する場合は、より具体的（=長い）patternを持つコンポーネントを採る。
    """
    root = package_directory.resolve()
    binaries = sorted(
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in _INVENTORY_SUFFIXES
    )
    classified: list[tuple[str, str]] = []
    unclassified: list[str] = []
    for relative in binaries:
        owner = _classify_one(relative, components)
        if owner is None:
            unclassified.append(relative)
        else:
            classified.append((relative, owner))
    return RuntimeInventory(tuple(classified), tuple(unclassified))


def summarize_inventory(inventory: RuntimeInventory) -> str:
    """runtime inventoryの要約（レポートとログ用）。"""
    counts: dict[str, int] = {}
    for _relative, identifier in inventory.classified:
        counts[identifier] = counts.get(identifier, 0) + 1
    breakdown = ", ".join(f"{identifier}={counts[identifier]}" for identifier in sorted(counts))
    return (
        f"runtime DLL/pyd {inventory.total}件: "
        f"分類済み{len(inventory.classified)}件（{breakdown}） / "
        f"未分類{len(inventory.unclassified)}件"
    )


def _classify_one(relative: str, components: Sequence[LicenseComponent]) -> str | None:
    """1ファイルを、最も具体的なpatternに一致したコンポーネントへ割り当てる。"""
    path = PurePosixPath(relative)
    best_identifier: str | None = None
    best_score = -1
    for component in components:
        for pattern in component.file_patterns:
            if path.full_match(pattern, case_sensitive=False) and len(pattern) > best_score:
                best_score = len(pattern)
                best_identifier = component.identifier
    return best_identifier


def _required_string(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if type(value) is not str or not value:
        raise ValueError(f"{key}が文字列ではありません: {value!r}")
    return value

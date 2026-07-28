"""UI 層が具体的な再生実装へ依存していないことを AST で検査する。

文字列検索ではコメントや docstring を誤検出するため、標準ライブラリの ast を使う。
新しい依存は追加しない。
"""

import ast
from pathlib import Path

import pytest

UI_DIR = Path(__file__).resolve().parents[3] / "src" / "sdp" / "ui"

FORBIDDEN_MODULES = {
    "sdp.core.playback.qt_backend",
    "PySide6.QtMultimedia",
}
FORBIDDEN_NAMES = {
    "QtMultimediaBackend",
    "QMediaPlayer",
    "QAudioOutput",
    "QAudioBufferOutput",
}


def ui_modules() -> list[Path]:
    modules = sorted(UI_DIR.rglob("*.py"))
    assert modules, f"UI モジュールが見つかりません: {UI_DIR}"
    return modules


def imported_modules_and_names(source: str) -> tuple[set[str], set[str]]:
    """import 文が参照するモジュール名と、取り込む名前を返す。"""
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
                names.add(alias.name.rsplit(".", maxsplit=1)[-1])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            names.update(alias.name for alias in node.names)
    return modules, names


def forbidden_modules(modules: set[str]) -> set[str]:
    """禁止モジュール自身と、その配下に一致するimportを返す。"""
    return {
        module
        for module in modules
        if any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_MODULES
        )
    }


def test_parent_module_import_is_expanded_to_forbidden_child() -> None:
    """親モジュールからqt_backendをimportする迂回も検出する。"""
    modules, _ = imported_modules_and_names("from sdp.core.playback import qt_backend")

    assert forbidden_modules(modules) == {"sdp.core.playback.qt_backend"}


@pytest.mark.parametrize(
    "module_path", ui_modules(), ids=lambda path: path.relative_to(UI_DIR).as_posix()
)
def test_ui_module_does_not_import_playback_implementation(module_path: Path) -> None:
    """UI から qt_backend・QMediaPlayer・QAudioOutput を import しない。"""
    modules, names = imported_modules_and_names(module_path.read_text(encoding="utf-8"))

    forbidden = forbidden_modules(modules)
    assert not forbidden, f"{module_path.name}: {forbidden}"
    assert not (names & FORBIDDEN_NAMES), f"{module_path.name}: {names & FORBIDDEN_NAMES}"

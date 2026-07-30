"""起動要求と単一instanceの依存境界を固定する。"""

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "sdp"


def imported_modules(path: Path) -> set[str]:
    """sourceがimportするmodule名を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_launch_request_is_qt_independent() -> None:
    """LaunchRequestの解釈にQtやUIを必要としない。"""
    modules = imported_modules(SOURCE_ROOT / "services" / "launch_request.py")

    assert not any(module.startswith("PySide6") for module in modules)
    assert not any(module.startswith("sdp.ui") for module in modules)


def test_single_instance_does_not_own_application_dependencies() -> None:
    """IPC層にUI・playlist・playback・保存設定を持ち込まない。"""
    modules = imported_modules(SOURCE_ROOT / "services" / "single_instance.py")
    forbidden_prefixes = (
        "PySide6.QtWidgets",
        "sdp.core.playback",
        "sdp.core.playlist",
        "sdp.services.settings",
        "sdp.services.ui_state",
        "sdp.services.playlist_session",
        "sdp.ui",
    )

    assert not any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for module in modules
        for forbidden in forbidden_prefixes
    )

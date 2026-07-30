"""PyInstaller onedir配布物の構造を検査する純粋ロジック。"""

from pathlib import Path
from typing import Final

_FORBIDDEN_DIRECTORIES: Final = frozenset(
    {".git", ".pytest_cache", "__pycache__", "pytest", "ruff", "test_audio", "tests"}
)
_FORBIDDEN_USER_FILES: Final = frozenset(
    {"playlist.json", "settings.json", "ui-state.json", "sdp.log"}
)


def validate_package_layout(package_directory: Path) -> tuple[str, ...]:
    """配布ディレクトリの不足物と開発・ユーザーデータ混入を返す。"""
    root = package_directory.resolve()
    failures: list[str] = []

    if not (root / "sdp.exe").is_file():
        failures.append("sdp.exeがありません")
    if not (root / "_internal").is_dir():
        failures.append("_internalディレクトリがありません")

    required_patterns = {
        "Python DLL": "python3*.dll",
        "Qt Core": "Qt6Core.dll",
        "Qt GUI": "Qt6Gui.dll",
        "Qt Widgets": "Qt6Widgets.dll",
        "Qt Network": "Qt6Network.dll",
        "Qt Multimedia": "Qt6Multimedia.dll",
        "Qt platform plugin": "qwindows.dll",
        "Visual C++ Runtime": "VCRUNTIME*.dll",
    }
    for label, pattern in required_patterns.items():
        if not any(root.rglob(pattern)):
            failures.append(f"{label}がありません（{pattern}）")

    for item in root.rglob("*"):
        if item.is_dir() and item.name.lower() in _FORBIDDEN_DIRECTORIES:
            failures.append(f"開発用ディレクトリが混入しています: {item.relative_to(root)}")
        if item.is_file() and item.name.lower() in _FORBIDDEN_USER_FILES:
            failures.append(f"ユーザーデータが混入しています: {item.relative_to(root)}")
        if item.is_file() and item.suffix.lower() in {".py", ".pyc"}:
            failures.append(f"Pythonソース／キャッシュが混入しています: {item.relative_to(root)}")

    return tuple(failures)

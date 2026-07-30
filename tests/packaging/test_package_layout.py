"""Windows onedir配布物の構造契約を検証する。"""

from pathlib import Path

from sdp.package_layout import validate_package_layout


def _create_minimum_package(root: Path) -> None:
    """検査に必要な最小のダミー配布物を作る。"""
    (root / "_internal" / "PySide6" / "plugins" / "platforms").mkdir(parents=True)
    for relative in (
        "sdp.exe",
        "_internal/python313.dll",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/Qt6Gui.dll",
        "_internal/PySide6/Qt6Widgets.dll",
        "_internal/PySide6/Qt6Network.dll",
        "_internal/PySide6/Qt6Multimedia.dll",
        "_internal/PySide6/plugins/platforms/qwindows.dll",
        "_internal/VCRUNTIME140.dll",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_minimum_onedir_layout_is_accepted(tmp_path: Path) -> None:
    """exe、Python、必要Qt DLLとplatform pluginが揃えば成功する。"""
    _create_minimum_package(tmp_path)

    assert validate_package_layout(tmp_path) == ()


def test_missing_runtime_and_development_files_are_reported(tmp_path: Path) -> None:
    """不足runtimeと開発・ユーザーファイルの混入を同時に報告する。"""
    (tmp_path / "tests").mkdir()
    (tmp_path / "source.py").touch()
    (tmp_path / "settings.json").touch()

    failures = validate_package_layout(tmp_path)

    assert any("sdp.exe" in failure for failure in failures)
    assert any("Qt Multimedia" in failure for failure in failures)
    assert any("開発用ディレクトリ" in failure for failure in failures)
    assert any("Pythonソース" in failure for failure in failures)
    assert any("ユーザーデータ" in failure for failure in failures)


def test_real_package_layout_when_built() -> None:
    """dist/sdpが存在する場合は実配布物も同じ契約で検査する。"""
    package = Path(__file__).parents[2] / "dist" / "sdp"
    if not package.exists():
        return

    assert validate_package_layout(package) == ()

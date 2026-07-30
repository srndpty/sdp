"""Windows onedir配布物の構造契約を検証する。"""

from pathlib import Path

import pytest

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
        "_internal/PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
        "_internal/PySide6/plugins/multimedia/windowsmediaplugin.dll",
        "_internal/PySide6/avcodec-61.dll",
        "_internal/PySide6/avformat-61.dll",
        "_internal/PySide6/avutil-59.dll",
        "_internal/PySide6/swresample-5.dll",
        "_internal/VCRUNTIME140.dll",
        "_internal/LICENSE",
        "_internal/THIRD_PARTY_NOTICES.txt",
        "_internal/licenses/Python/LICENSE.txt",
        "_internal/licenses/PySide6/LicenseRef-Qt-Commercial.txt",
        "_internal/licenses/numpy/LICENSE.txt",
        "_internal/licenses/mutagen/COPYING",
        "_internal/licenses/pyinstaller/COPYING.txt",
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


def test_missing_multimedia_backend_and_license_are_reported(tmp_path: Path) -> None:
    """backend plugin、FFmpeg runtime、必須licenseの欠落を個別に報告する。"""
    _create_minimum_package(tmp_path)
    (tmp_path / "_internal/PySide6/plugins/multimedia/ffmpegmediaplugin.dll").unlink()
    (tmp_path / "_internal/PySide6/avcodec-61.dll").unlink()
    (tmp_path / "_internal/licenses/mutagen/COPYING").unlink()

    failures = validate_package_layout(tmp_path)

    assert any("Qt FFmpeg media plugin" in failure for failure in failures)
    assert any("FFmpeg avcodec" in failure for failure in failures)
    assert any("Mutagen license" in failure for failure in failures)


def test_real_package_layout_when_built() -> None:
    """dist/sdpが存在する場合は実配布物も同じ契約で検査する。"""
    package = Path(__file__).parents[2] / "dist" / "sdp"
    if not package.exists():
        pytest.skip("配布物未ビルド")

    assert validate_package_layout(package) == ()

"""semantic versionからWindows version resourceへの変換規則を検証する。"""

import re
import tomllib
from pathlib import Path

import pytest

from sdp import __version__
from sdp.windows_version import (
    WINDOWS_VERSION_FIELD_COUNT,
    format_version_tuple,
    is_pre_release,
    render_version_info,
    windows_file_version,
)

_REPO_ROOT = Path(__file__).parents[2]
_TEMPLATE = _REPO_ROOT / "packaging" / "windows-version-info.txt"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.0.1", (0, 0, 1, 0)),
        ("1.2.3", (1, 2, 3, 0)),
        ("0.0.0", (0, 0, 0, 0)),
        ("10.20.30", (10, 20, 30, 0)),
        ("65535.65535.65535", (65535, 65535, 65535, 0)),
    ],
)
def test_release_versions_map_to_four_integer_fields(
    version: str, expected: tuple[int, int, int, int]
) -> None:
    """MAJOR.MINOR.PATCHは第4要素0の4要素へ変換される。"""
    assert windows_file_version(version) == expected
    assert len(windows_file_version(version)) == WINDOWS_VERSION_FIELD_COUNT


@pytest.mark.parametrize("version", ["1.2.3rc1", "1.2.3-rc.1", "1.2.3a1", "1.2.3.dev1", "1.2.3b2"])
def test_pre_release_keeps_numeric_fields_of_the_base_version(version: str) -> None:
    """pre-releaseは数値4要素へ反映せず、直前のMAJOR.MINOR.PATCHを使う。"""
    assert windows_file_version(version) == (1, 2, 3, 0)
    assert is_pre_release(version) is True


def test_local_version_is_ignored_in_numeric_fields() -> None:
    """未インストール時のフォールバック ``0.0.0+unknown`` も変換できる。"""
    assert windows_file_version("0.0.0+unknown") == (0, 0, 0, 0)
    assert is_pre_release("0.0.0+unknown") is False


@pytest.mark.parametrize(
    "version",
    ["", "1", "1.2", "1.2.3.4", "v1.2.3", "1.2.x", "-1.2.3", "1.2.3 extra", "01.2.3"],
)
def test_invalid_versions_are_rejected(version: str) -> None:
    """解釈できないversionは黙って0へ落とさず例外にする。"""
    with pytest.raises(ValueError):
        windows_file_version(version)
    with pytest.raises(ValueError):
        is_pre_release(version)


def test_field_larger_than_16bit_is_rejected() -> None:
    """Windowsのversion要素は16bitに収まる必要がある。"""
    with pytest.raises(ValueError):
        windows_file_version("65536.0.0")


def test_format_version_tuple_requires_four_fields() -> None:
    """PyInstallerのversion fileへ書く形式は4要素固定である。"""
    assert format_version_tuple((0, 0, 1, 0)) == "(0, 0, 1, 0)"
    with pytest.raises(ValueError):
        format_version_tuple((0, 0, 1))  # pyright: ignore[reportArgumentType]


def test_pyproject_version_is_convertible_and_matches_package_version() -> None:
    """versionのsourceはpyproject.tomlひとつであり、常に変換できる。"""
    document = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = document["project"]["version"]
    assert declared == __version__
    assert windows_file_version(declared) == (0, 0, 1, 0)


def test_version_info_template_is_rendered_without_placeholders() -> None:
    """実テンプレートへversionを差し込むと、未置換のプレースホルダが残らない。"""
    rendered = render_version_info(_TEMPLATE.read_text(encoding="utf-8"), "1.2.3")

    assert "filevers=(1, 2, 3, 0)" in rendered
    assert "prodvers=(1, 2, 3, 0)" in rendered
    assert "StringStruct('FileVersion', '1.2.3')" in rendered
    assert "StringStruct('ProductVersion', '1.2.3')" in rendered
    assert not re.search(r"\{[a-z_]+\}", rendered)


def test_version_info_template_declares_required_fields() -> None:
    """FileDescription等のWindows表示項目を落としていない。"""
    rendered = render_version_info(_TEMPLATE.read_text(encoding="utf-8"), __version__)

    for field, value in (
        ("FileDescription", "sdp"),
        ("ProductName", "sdp"),
        ("CompanyName", "sdp contributors"),
        ("OriginalFilename", "sdp.exe"),
        ("InternalName", "sdp"),
    ):
        assert f"StringStruct('{field}', '{value}')" in rendered

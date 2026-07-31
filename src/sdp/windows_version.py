"""semantic versionとWindows version resourceの対応（Qt非依存の純粋ロジック）。

Windowsのversion resourceは ``FILEVERSION`` / ``PRODUCTVERSION`` を
**16bit整数4要素**で持つ。一方 ``pyproject.toml`` のversionは ``MAJOR.MINOR.PATCH``
（将来はpre-releaseやlocal versionを伴い得る）である。この差を吸収する規則を
1か所へ集約し、build scriptとspecの双方が同じ結論を使えるようにする。

規則:

- ``0.0.1`` → ``(0, 0, 1, 0)``、``1.2.3`` → ``(1, 2, 3, 0)``。
  第4要素はbuild番号用に予約し、**常に0**とする（build時刻等を入れない。
  同一commitからのbuildで version resource が変わらないようにするため）。
- pre-release（``1.2.3rc1`` / ``1.2.3-rc.1``）とlocal version（``+unknown``）は
  **数値4要素へは反映しない**。数値は直前の ``MAJOR.MINOR.PATCH`` を使い、
  pre-release識別子は文字列項目（``FileVersion`` / ``ProductVersion``）にだけ残す。
  Windowsの数値比較でpre-releaseを正式版より小さく見せる方法は無いため、
  「数値は同じ、表示文字列で区別する」という割り切りを明示する。
- 上記で解釈できないversionは例外にする（黙って0.0.0.0へ落とさない）。
"""

import re
from typing import Final

WINDOWS_VERSION_FIELD_COUNT: Final = 4
_MAX_FIELD_VALUE: Final = 0xFFFF

# MAJOR.MINOR.PATCH と、任意のpre-release（PEP 440 の a1/b1/rc1 と -rc.1 の双方）、
# 任意のlocal version（+unknown）を受け付ける。
_VERSION_PATTERN: Final = re.compile(
    r"""
    ^
    (?P<major>0|[1-9][0-9]*)
    \.(?P<minor>0|[1-9][0-9]*)
    \.(?P<patch>0|[1-9][0-9]*)
    (?P<pre>(?:[-.]?(?:a|b|c|rc|alpha|beta|pre|dev)[-.]?[0-9]*)+)?
    (?P<local>\+[0-9A-Za-z.]+)?
    $
    """,
    re.VERBOSE,
)


def windows_file_version(version: str) -> tuple[int, int, int, int]:
    """semantic versionをWindowsの4要素整数へ変換する。

    :raises ValueError: 解釈できないversion、または65535を超える要素があるとき。
    """
    match = _VERSION_PATTERN.match(version.strip())
    if match is None:
        raise ValueError(f"Windows version resourceへ変換できないversionです: {version!r}")
    fields = (int(match["major"]), int(match["minor"]), int(match["patch"]), 0)
    too_large = [value for value in fields if value > _MAX_FIELD_VALUE]
    if too_large:
        raise ValueError(f"version要素が16bitの範囲を超えています: {version!r}")
    return fields


def is_pre_release(version: str) -> bool:
    """pre-release識別子を含むversionかどうか（数値4要素では表現しない部分）。"""
    match = _VERSION_PATTERN.match(version.strip())
    if match is None:
        raise ValueError(f"Windows version resourceへ変換できないversionです: {version!r}")
    return match["pre"] is not None


def format_version_tuple(fields: tuple[int, int, int, int]) -> str:
    """PyInstallerのversion fileへ書く ``(0, 0, 1, 0)`` 形式へ整形する。"""
    if len(fields) != WINDOWS_VERSION_FIELD_COUNT:  # pyright: ignore[reportUnnecessaryComparison]
        raise ValueError(f"version resourceは4要素である必要があります: {fields!r}")
    return "(" + ", ".join(str(value) for value in fields) + ")"


def render_version_info(template: str, version: str) -> str:
    """PyInstallerのversion file templateへversionを差し込む。

    templateは ``{version_tuple}`` と ``{version}`` だけを置換対象にする
    （installer側と同じ規則をここへ集約し、二重管理を避ける）。
    """
    version_tuple = format_version_tuple(windows_file_version(version))
    return template.format(version_tuple=version_tuple, version=version)

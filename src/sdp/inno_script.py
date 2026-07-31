"""Inno Setup scriptの限定parser（Qt非依存の純粋ロジック）。

Inno Setup compilerが無い環境でも、installerの契約
（per-user、HKCUのみ、対象拡張子、uninstall時のユーザーデータ保持など）を
pytestで検査できるようにするためのもの。

**完全なInno Setup言語のparserではない。** 次だけを扱う。

- ``[Section]`` の切り替えと ``;`` 始まりの行コメント
- ``#define NAME value`` と、値中の ``{#NAME}`` 展開
- ``[Setup]`` などの ``key=value`` 行
- ``[Files]`` などの ``Name: Value; Name: Value`` 形式のパラメータ行
- ``"..."`` で囲んだ値と、その中の ``""``（リテラルの二重引用符）

条件付きコンパイル（``#if`` 系）は解釈せず、``#error`` などの前処理行は無視する。
``[Code]`` は構文解析せず、生テキストとして保持する。
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

_PARAMETER_SECTIONS: Final = frozenset(
    {
        "components",
        "dirs",
        "files",
        "icons",
        "ini",
        "installdelete",
        "languages",
        "registry",
        "run",
        "tasks",
        "types",
        "uninstalldelete",
        "uninstallrun",
    }
)
_DIRECTIVE_SECTIONS: Final = frozenset({"setup", "messages", "custommessages", "langoptions"})
_RAW_SECTIONS: Final = frozenset({"code"})


@dataclass(frozen=True, slots=True)
class InnoEntry:
    """``[Files]`` などの1行（``Name: Value`` の並び）。"""

    section: str
    parameters: Mapping[str, str]
    line_number: int

    def value(self, name: str) -> str | None:
        """パラメータ名（大文字小文字を無視）で値を引く。"""
        lowered = name.lower()
        for key, value in self.parameters.items():
            if key.lower() == lowered:
                return value
        return None

    def flags(self) -> frozenset[str]:
        """``Flags:`` を小文字の集合として返す。"""
        raw = self.value("Flags") or ""
        return frozenset(item.lower() for item in raw.split() if item)


@dataclass(frozen=True, slots=True)
class InnoScript:
    """解析済みのInno Setup script。"""

    defines: Mapping[str, str]
    directives: Mapping[str, Mapping[str, str]]
    entries: tuple[InnoEntry, ...]
    code: str
    source: str
    referenced_defines: frozenset[str]
    """``{#NAME}`` として参照された名前（``#define`` 済みかどうかを問わない）。"""

    def setup(self, name: str) -> str | None:
        """``[Setup]`` のdirectiveを引く（大文字小文字を無視、最後の指定が有効）。"""
        return self.directive("Setup", name)

    def directive(self, section: str, name: str) -> str | None:
        """任意のdirective sectionから値を引く。"""
        values = self.directives.get(section.lower(), {})
        lowered = name.lower()
        for key, value in values.items():
            if key.lower() == lowered:
                return value
        return None

    def section_entries(self, section: str) -> tuple[InnoEntry, ...]:
        """指定sectionのパラメータ行だけを返す。"""
        lowered = section.lower()
        return tuple(entry for entry in self.entries if entry.section == lowered)


def parse_inno_script(source: str) -> InnoScript:
    """Inno Setup scriptを解析する。"""
    defines: dict[str, str] = {}
    referenced: set[str] = set()
    directives: dict[str, dict[str, str]] = {}
    entries: list[InnoEntry] = []
    code_lines: list[str] = []
    section = ""

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if section in _RAW_SECTIONS and not line.startswith("["):
            code_lines.append(raw_line)
            continue
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if line.startswith("#"):
            name, value = _parse_define(line)
            if name is not None:
                defines[name] = _expand(value, defines, referenced)
            continue
        if section in _DIRECTIVE_SECTIONS:
            key, separator, value = line.partition("=")
            if separator:
                directives.setdefault(section, {})[key.strip()] = _expand(
                    value.strip(), defines, referenced
                )
            continue
        if section in _PARAMETER_SECTIONS:
            parameters = _parse_parameters(line, defines, referenced)
            if parameters:
                entries.append(InnoEntry(section, parameters, line_number))

    return InnoScript(
        defines=defines,
        directives=directives,
        entries=tuple(entries),
        code="\n".join(code_lines),
        source=source,
        referenced_defines=frozenset(referenced),
    )


def _parse_define(line: str) -> tuple[str | None, str]:
    """``#define NAME value`` を解析する（他の前処理行は ``None``）。"""
    body = line[1:].strip()
    keyword, _, rest = body.partition(" ")
    if keyword.lower() != "define":
        return None, ""
    name, _, value = rest.strip().partition(" ")
    return (name.strip() or None), _unquote(value.strip())


def _parse_parameters(
    line: str, defines: Mapping[str, str], referenced: set[str]
) -> dict[str, str]:
    """``Name: Value; Name: Value`` を辞書へ分解する。"""
    parameters: dict[str, str] = {}
    for part in _split_unquoted(line, ";"):
        name, separator, value = part.partition(":")
        if not separator:
            continue
        parameters[name.strip()] = _expand(_unquote(value.strip()), defines, referenced)
    return parameters


def _split_unquoted(text: str, separator: str) -> Iterator[str]:
    """引用符の外にある区切り文字だけで分割する。"""
    buffer: list[str] = []
    in_quotes = False
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            # 引用符内の "" はリテラルの引用符なので区切り判定を変えない。
            if in_quotes and index + 1 < len(text) and text[index + 1] == '"':
                buffer.append('""')
                index += 2
                continue
            in_quotes = not in_quotes
            buffer.append(character)
        elif character == separator and not in_quotes:
            yield "".join(buffer).strip()
            buffer = []
        else:
            buffer.append(character)
        index += 1
    remaining = "".join(buffer).strip()
    if remaining:
        yield remaining


def _unquote(value: str) -> str:
    """``"..."`` を外し、内部の ``""`` を ``"`` へ戻す。"""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value


def _expand(value: str, defines: Mapping[str, str], referenced: set[str]) -> str:
    """``{#NAME}`` を ``#define`` 済みの値へ置換する（未定義はそのまま残す）。"""
    result: list[str] = []
    index = 0
    while index < len(value):
        start = value.find("{#", index)
        if start < 0:
            result.append(value[index:])
            break
        end = value.find("}", start)
        if end < 0:
            result.append(value[index:])
            break
        name = value[start + 2 : end].strip()
        referenced.add(name)
        result.append(value[index:start])
        result.append(defines.get(name, value[start : end + 1]))
        index = end + 1
    return "".join(result)


def registry_roots(entries: Sequence[InnoEntry]) -> tuple[str, ...]:
    """``[Registry]`` 行のRootを重複なく返す。"""
    roots: list[str] = []
    for entry in entries:
        root = (entry.value("Root") or "").strip()
        if root and root not in roots:
            roots.append(root)
    return tuple(roots)

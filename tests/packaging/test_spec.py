"""PyInstaller specの配布契約を検証する。"""

import ast
import re
from pathlib import Path

_SPEC = Path(__file__).parents[2] / "packaging" / "sdp.spec"


def _call(tree: ast.AST, name: str) -> ast.Call:
    """指定名のcallをspec ASTから1件返す。"""
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]
    assert len(calls) == 1
    return calls[0]


def _keyword(call: ast.Call, name: str) -> ast.expr:
    """callの必須keyword値を返す。"""
    values = [keyword.value for keyword in call.keywords if keyword.arg == name]
    assert len(values) == 1
    return values[0]


def test_spec_is_portable_onedir_windowed_configuration() -> None:
    """specは相対基準のonedir・windowed・UPX無効構成である。"""
    source = _SPEC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    analysis = _call(tree, "Analysis")
    executable = _call(tree, "EXE")
    collect = _call(tree, "COLLECT")

    assert "src" in ast.unparse(_keyword(analysis, "pathex"))
    assert "ENTRY_SCRIPT" in ast.unparse(analysis.args[0])
    assert ast.literal_eval(_keyword(executable, "console")) is False
    assert ast.literal_eval(_keyword(executable, "exclude_binaries")) is True
    assert ast.literal_eval(_keyword(executable, "upx")) is False
    assert ast.literal_eval(_keyword(collect, "upx")) is False
    assert ast.literal_eval(_keyword(collect, "name")) == "sdp"
    assert not re.search(r"[A-Za-z]:[\\/]", source)


def test_spec_collects_metadata_and_only_declared_resources() -> None:
    """version metadataとライセンスだけをdataに加え、開発assetを収集しない。"""
    source = _SPEC.read_text(encoding="utf-8")

    assert 'copy_metadata("sdp")' in source
    assert '"LICENSE"' in source
    assert '"THIRD_PARTY_NOTICES.txt"' in source
    assert "collect_all" not in source
    assert "test_audio" not in source
    assert "assets" not in source
    assert "if missing:" in source
    assert "必須ライセンスファイルを検出できません" in source
    assert "if not python_license.is_file():" in source

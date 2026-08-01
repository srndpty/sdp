"""Windows配布物の外部Runtime参照を正規化する処理を検証する。"""

from pathlib import Path

from sdp.package_runtime import replace_hashed_msvc_imports


def test_replace_hashed_msvc_imports_preserves_binary_size(tmp_path: Path) -> None:
    """私有名importを標準名とNUL paddingへ置換する。"""
    extension = tmp_path / "_internal" / "numpy" / "_core" / "sample.pyd"
    extension.parent.mkdir(parents=True)
    dependency = b"msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
    original = b"prefix\0" + dependency + b"\0suffix"
    extension.write_bytes(original)

    changed = replace_hashed_msvc_imports(tmp_path)

    updated = extension.read_bytes()
    assert changed == (Path("_internal/numpy/_core/sample.pyd"),)
    assert len(updated) == len(original)
    assert dependency not in updated
    assert b"MSVCP140.dll\0" in updated


def test_replace_hashed_msvc_imports_ignores_unrelated_extensions(
    tmp_path: Path,
) -> None:
    """対象importを持たないpydは変更しない。"""
    extension = tmp_path / "_internal" / "numpy" / "sample.pyd"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"KERNEL32.dll\0")

    assert replace_hashed_msvc_imports(tmp_path) == ()
    assert extension.read_bytes() == b"KERNEL32.dll\0"

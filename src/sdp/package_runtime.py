"""Windows配布物を外部VC++ Runtime参照へ正規化する処理。"""

import re
from pathlib import Path

_HASHED_MSVC_IMPORT = re.compile(rb"msvcp140-[0-9a-f]+\.dll", re.IGNORECASE)
_SYSTEM_MSVC_IMPORT = b"MSVCP140.dll"


def _replace_hashed_import(match: re.Match[bytes]) -> bytes:
    """PE import名の領域長を変えず、標準名とNUL paddingを返す。"""
    padding = len(match.group()) - len(_SYSTEM_MSVC_IMPORT)
    if padding < 1:
        raise ValueError("MSVCP import名を安全に置換できません")
    return _SYSTEM_MSVC_IMPORT + (b"\0" * padding)


def replace_hashed_msvc_imports(package_directory: Path) -> tuple[Path, ...]:
    """NumPyの私有名MSVCP importをRedistributableの標準名へ戻す。

    delvewheelで処理されたNumPy wheelは、同梱DLLとの衝突を避けるため
    ``MSVCP140.dll`` のimport名をハッシュ付きの名前へ書き換えている。
    配布物からそのDLLを除外する場合は、同じ長さの領域をNULで埋めながら
    標準名へ戻し、Windows loaderがsystemのVC++ Runtimeを解決できるようにする。
    """
    changed: list[Path] = []
    numpy_directory = package_directory / "_internal" / "numpy"
    for path in sorted(numpy_directory.rglob("*.pyd")):
        original = path.read_bytes()
        updated, count = _HASHED_MSVC_IMPORT.subn(_replace_hashed_import, original)
        if count:
            path.write_bytes(updated)
            changed.append(path.relative_to(package_directory))
    return tuple(changed)

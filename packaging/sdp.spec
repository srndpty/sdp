# -*- mode: python ; coding: utf-8 -*-
"""sdp Windows onedir配布物の唯一のPyInstaller設定。"""

from importlib.metadata import distribution
from pathlib import Path
import shutil
import sys

from PyInstaller.utils.hooks import copy_metadata


REPO_ROOT = Path(SPECPATH).resolve().parent
ENTRY_SCRIPT = REPO_ROOT / "src" / "sdp" / "__main__.py"


def license_files(distribution_name, target_name, required_names):
    """インストール済みwheelが提供するライセンス原文を収集する。"""
    package = distribution(distribution_name)
    collected = []
    for item in package.files or ():
        normalized = str(item).replace("\\", "/")
        marker = ".dist-info/licenses/"
        if marker not in normalized.lower():
            continue
        relative = normalized[normalized.lower().index(marker) + len(marker) :]
        collected.append(
            (str(Path(package.locate_file(item)).resolve()), f"licenses/{target_name}/{Path(relative).parent}")
        )
    collected_names = {Path(source).name for source, _destination in collected}
    missing = set(required_names) - collected_names
    if missing:
        raise RuntimeError(
            f"{distribution_name}の必須ライセンスファイルを検出できません: {sorted(missing)}"
        )
    return collected


datas = copy_metadata("sdp")
project_documents = (REPO_ROOT / "LICENSE", REPO_ROOT / "THIRD_PARTY_NOTICES.txt")
for document in project_documents:
    if not document.is_file():
        raise RuntimeError(f"配布用文書がありません: {document.name}")
    datas.append((str(document), "."))

required_licenses = {
    "PySide6": {"LicenseRef-Qt-Commercial.txt"},
    "PySide6-Essentials": {"LicenseRef-Qt-Commercial.txt"},
    "PySide6-Addons": {"LicenseRef-Qt-Commercial.txt"},
    "shiboken6": {"LicenseRef-Qt-Commercial.txt"},
    "numpy": {"LICENSE.txt"},
    "mutagen": {"COPYING"},
    "pyinstaller": {"COPYING.txt"},
}
for package_name, required_names in required_licenses.items():
    datas += license_files(package_name, package_name, required_names)

python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise RuntimeError("PythonのLICENSE.txtを検出できません")
datas.append((str(python_license), "licenses/Python"))


analysis = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(REPO_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyInstaller",
        "coverage",
        "pre_commit",
        "pyright",
        "pytest",
        "ruff",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="sdp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="sdp",
)

# COLLECTはdatasを_internalへ置くため、利用者がZIP展開直後に読める位置
# （sdp.exeと同じ階層）へライセンス文書を複製する。原文一式は_internal/licenses/に残す。
package_root = Path(DISTPATH) / "sdp"
for document in project_documents:
    shutil.copy2(document, package_root / document.name)

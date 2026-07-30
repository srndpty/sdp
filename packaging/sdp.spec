# -*- mode: python ; coding: utf-8 -*-
"""sdp Windows onedir配布物の唯一のPyInstaller設定。"""

from importlib.metadata import distribution
from pathlib import Path
import sys

from PyInstaller.utils.hooks import copy_metadata


REPO_ROOT = Path(SPECPATH).resolve().parent
ENTRY_SCRIPT = REPO_ROOT / "src" / "sdp" / "__main__.py"


def license_files(distribution_name, target_name):
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
    return collected


datas = copy_metadata("sdp")
datas += [
    (str(REPO_ROOT / "LICENSE"), "."),
    (str(REPO_ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
]

for package_name in (
    "PySide6",
    "PySide6-Essentials",
    "PySide6-Addons",
    "shiboken6",
    "numpy",
    "mutagen",
    "pyinstaller",
):
    datas += license_files(package_name, package_name)

python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
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

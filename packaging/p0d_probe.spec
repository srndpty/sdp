# P0-D 検証用 exe の PyInstaller spec（onedir）。
#
# これは技術検証専用であり、製品版の spec ではない。
# 製品版 packaging/sdp.spec は P7 で別途作成する。混同しないこと。
#
# 方針:
#   - onedir を使う（onefile は使わない）
#   - PyInstaller 標準の PySide6 hook をまず使う。
#     最初から PySide6 全体を無条件に collect しない
#   - 不足が実測された場合にのみ hidden import / binary 収集を追加する
#   - 不要 Qt モジュールの積極的な除外は行わない（サイズ最適化は P7）
#   - UPX は使わない
#
# ビルド:
#   uv run pyinstaller --clean --noconfirm packaging/p0d_probe.spec

from pathlib import Path

# SPECPATH は PyInstaller が spec 実行時に設定する。
REPO_ROOT = Path(SPECPATH).parent  # noqa: F821
ENTRY_SCRIPT = REPO_ROOT / "spike" / "p0d_packaged_probe.py"

analysis = Analysis(  # noqa: F821
    [str(ENTRY_SCRIPT)],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    # 音源は exe へ埋め込まない。--audio-dir で外部から渡す。
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="p0d_probe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="p0d_probe",
)

"""installer成果物に添えるmanifestの生成（Qt非依存の純粋ロジック）。

ZIP配布物の :mod:`sdp.release_manifest` と同じ方針で作る。

- 環境固有の情報（username、build host、絶対path）を持ち込まない
- keyの順序を固定し、同じ入力なら同じJSONになる
- timestampを持たない（同一入力の再現性を確認できるようにする）

installer固有の関心事として、install scope、privileges、関連付け対象、
既定アプリを変更しないこと、uninstallでユーザーデータを保持することを記録する。
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sdp.installer_contract import (
    INSTALL_DIRECTORY_DISPLAY,
    INSTALLER_KIND,
    USER_DATA_DIRECTORY_DISPLAY,
)

INSTALLER_MANIFEST_SCHEMA_VERSION: Final = 1
INSTALL_SCOPE: Final = "per-machine"
INSTALL_PRIVILEGES: Final = "admin"
DEFAULT_APPLICATION_POLICY: Final = "not-modified"
REGISTRY_SCOPE: Final = "HKLM"
DISTRIBUTION_STATUS: Final = "technical-verification-only"
"""ライセンス未解決事項が残るため、公開配布物ではないことをmanifestにも残す。"""


def installer_name(version: str, architecture: str) -> str:
    """``sdp-0.0.1-windows-x64-setup.exe`` 形式のinstaller名を返す。"""
    suffix = "x64" if architecture == "x86_64" else architecture
    return f"sdp-{version}-windows-{suffix}-setup.exe"


def build_installer_manifest(
    *,
    version: str,
    architecture: str,
    app_id: str,
    prog_id: str,
    file_associations: Sequence[str],
    setup_file_name: str,
    setup_sha256: str,
    setup_size: int,
) -> dict[str, Any]:
    """key順を固定したinstaller manifestを組み立てる。"""
    return {
        "schema_version": INSTALLER_MANIFEST_SCHEMA_VERSION,
        "application": "sdp",
        "version": version,
        "platform": "windows",
        "architecture": architecture,
        "installer": INSTALLER_KIND,
        "scope": INSTALL_SCOPE,
        "privileges": INSTALL_PRIVILEGES,
        "app_id": app_id,
        "install_directory": INSTALL_DIRECTORY_DISPLAY,
        "user_data_directory": USER_DATA_DIRECTORY_DISPLAY,
        "user_data_removed_on_uninstall": False,
        "registry_scope": REGISTRY_SCOPE,
        "prog_id": prog_id,
        "file_associations": list(file_associations),
        "default_application": DEFAULT_APPLICATION_POLICY,
        "distribution": DISTRIBUTION_STATUS,
        "setup": {
            "file_name": setup_file_name,
            "sha256": setup_sha256,
            "size": setup_size,
        },
    }


def dump_installer_manifest(manifest: Mapping[str, Any]) -> str:
    """manifestをUTF-8のJSON文字列へ整形する（keyの順序は保つ）。"""
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"

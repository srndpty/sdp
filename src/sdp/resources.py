"""開発実行とPyInstaller実行のresource基準を1か所に集約する。"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    """PyInstallerのfrozen processかどうかを返す。"""
    return bool(getattr(sys, "frozen", False))


def application_base_directory() -> Path:
    """exeまたはrepositoryの基準ディレクトリを返す。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_base_directory() -> Path:
    """PyInstallerの`_internal`またはrepository rootを返す。"""
    if is_frozen():
        # PyInstaller 6はbundled moduleの__file__をbundle内の絶対pathにする。
        return Path(__file__).resolve().parents[1]
    return application_base_directory()


def resource_path(relative_path: str | Path) -> Path:
    """bundle内resourceのpathを返す。絶対pathと親参照は拒否する。"""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("resource pathはbundle内の相対pathにしてください")
    return resource_base_directory() / relative

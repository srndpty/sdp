"""installer manifestの生成規則を検証する。"""

import json
from pathlib import Path
from typing import Any

import pytest

from sdp.inno_script import parse_inno_script
from sdp.installer_contract import (
    APP_ID,
    AUDIO_FILE_EXTENSIONS,
    PROG_ID,
    file_associations,
)
from sdp.installer_manifest import (
    INSTALLER_MANIFEST_SCHEMA_VERSION,
    build_installer_manifest,
    dump_installer_manifest,
    installer_name,
)
from sdp.release_manifest import find_local_paths

_INSTALLER = Path(__file__).parents[2] / "packaging" / "installer.iss"


def manifest_of(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "version": "0.0.1",
        "architecture": "x86_64",
        "app_id": APP_ID,
        "prog_id": PROG_ID,
        "file_associations": AUDIO_FILE_EXTENSIONS,
        "setup_file_name": "sdp-0.0.1-windows-x64-setup.exe",
        "setup_sha256": "b" * 64,
        "setup_size": 71_000_000,
    }
    values.update(overrides)
    return build_installer_manifest(**values)


@pytest.mark.parametrize(
    ("version", "architecture", "expected"),
    [
        ("0.0.1", "x86_64", "sdp-0.0.1-windows-x64-setup.exe"),
        ("1.2.3", "x86_64", "sdp-1.2.3-windows-x64-setup.exe"),
        ("0.0.1", "arm64", "sdp-0.0.1-windows-arm64-setup.exe"),
    ],
)
def test_installer_name_follows_the_release_naming(
    version: str, architecture: str, expected: str
) -> None:
    """ZIP配布物と同じ命名規則（version・platform・architecture）にする。"""
    assert installer_name(version, architecture) == expected


def test_manifest_records_the_install_contract() -> None:
    """scope・privileges・関連付け・既定アプリ方針をmanifestへ残す。"""
    manifest = manifest_of()

    assert manifest["schema_version"] == INSTALLER_MANIFEST_SCHEMA_VERSION
    assert manifest["application"] == "sdp"
    assert manifest["version"] == "0.0.1"
    assert manifest["platform"] == "windows"
    assert manifest["architecture"] == "x86_64"
    assert manifest["installer"] == "inno-setup"
    assert manifest["scope"] == "per-user"
    assert manifest["privileges"] == "lowest"
    assert manifest["registry_scope"] == "HKCU"
    assert manifest["app_id"] == APP_ID
    assert manifest["prog_id"] == PROG_ID
    assert manifest["file_associations"] == list(AUDIO_FILE_EXTENSIONS)
    assert manifest["default_application"] == "not-modified"
    assert manifest["user_data_removed_on_uninstall"] is False
    assert manifest["install_directory"] == r"%LOCALAPPDATA%\Programs\sdp"
    assert manifest["user_data_directory"] == r"%LOCALAPPDATA%\sdp"


def test_manifest_records_setup_hash_and_size() -> None:
    """setup exeのSHA-256とサイズを記録する。"""
    manifest = manifest_of()

    assert manifest["setup"] == {
        "file_name": "sdp-0.0.1-windows-x64-setup.exe",
        "sha256": "b" * 64,
        "size": 71_000_000,
    }


def test_manifest_states_the_build_is_not_publishable() -> None:
    """ライセンス未解決のため、公開配布物と誤認させない値を持つ。"""
    assert manifest_of()["distribution"] == "technical-verification-only"


def test_manifest_has_no_local_paths_or_user_names() -> None:
    """絶対path・UNC path・username・build hostを持ち込まない。"""
    assert find_local_paths(manifest_of()) == ()


def test_manifest_key_order_is_fixed_and_round_trips() -> None:
    """同じ入力なら同じJSONになり、読み直しても内容が変わらない。"""
    manifest = manifest_of()
    text = dump_installer_manifest(manifest)

    assert text == dump_installer_manifest(manifest_of())
    assert text.endswith("\n")
    restored = json.loads(text)
    assert restored == manifest
    assert list(restored) == list(manifest)
    assert list(restored)[:3] == ["schema_version", "application", "version"]


def test_manifest_associations_come_from_the_installer_script() -> None:
    """拡張子一覧は installer.iss を source of truth にし、二重管理しない。"""
    script = parse_inno_script(_INSTALLER.read_text(encoding="utf-8"))
    manifest = manifest_of(file_associations=file_associations(script))

    assert manifest["file_associations"] == list(AUDIO_FILE_EXTENSIONS)
    assert len(manifest["file_associations"]) == 7

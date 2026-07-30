"""developmentとPyInstallerのresource path契約を検証する。"""

import sys
from pathlib import Path

import pytest

from sdp import resources


def test_development_paths_use_repository_root() -> None:
    """通常実行ではrepository rootをapplication・resource基準にする。"""
    expected = Path(resources.__file__).resolve().parents[2]

    assert resources.application_base_directory() == expected
    assert resources.resource_base_directory() == expected


def test_frozen_paths_use_executable_and_internal_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """frozen時はsource treeではなくexeと`_internal`を基準にする。"""
    executable = tmp_path / "sdp" / "sdp.exe"
    bundled_module = executable.parent / "_internal" / "sdp" / "resources.py"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(resources, "__file__", str(bundled_module))

    assert resources.application_base_directory() == executable.parent
    assert resources.resource_base_directory() == executable.parent / "_internal"
    assert resources.resource_path("THIRD_PARTY_NOTICES.txt") == (
        executable.parent / "_internal" / "THIRD_PARTY_NOTICES.txt"
    )


@pytest.mark.parametrize("value", ["../secret", Path("C:/absolute.txt")])
def test_resource_path_rejects_escape(value: str | Path) -> None:
    """resource resolverでbundle外へ脱出できない。"""
    with pytest.raises(ValueError, match="bundle内"):
        resources.resource_path(value)

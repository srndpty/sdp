"""ユーザーデータ内の波形キャッシュ保存先を検証する。"""

from pathlib import Path

import pytest

from sdp.services.user_paths import default_waveform_cache_directory


def test_waveform_cache_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """波形cacheを音源横ではなくLOCALAPPDATA配下へ集約する。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_waveform_cache_directory() == tmp_path / "sdp" / "cache" / "waveforms"

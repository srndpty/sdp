"""配布版smoke test scriptのprocess管理を検証する。"""

from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "package-smoke.ps1"


def test_selftest_process_has_timeout_and_kill_fallback() -> None:
    """loader errorでもsmoke testが無期限に残留しない。"""
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "WaitForExit(20000)" in source
    assert "$process.Kill()" in source
    assert "WaitForExit(5000)" in source
    assert "$process.Dispose()" in source

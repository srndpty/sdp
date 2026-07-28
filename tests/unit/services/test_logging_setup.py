"""ログ設定の契約を検証する。"""

import logging
from pathlib import Path

import pytest

from sdp.services import logging_setup


@pytest.fixture(autouse=True)
def restore_root_logger() -> object:
    """テストがルートロガーへ加えた変更を元へ戻す。"""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield None
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def test_configure_logging_writes_utf8_log_file(tmp_path: Path) -> None:
    """指定したディレクトリへ UTF-8 のログファイルを作る。"""
    log_path = logging_setup.configure_logging(log_directory=tmp_path)

    logging.getLogger("sdp.test").error("日本語のログ")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path == tmp_path / "sdp.log"
    assert "日本語のログ" in log_path.read_text(encoding="utf-8")


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """複数回呼んでもハンドラーを重複追加しない。"""
    logging_setup.configure_logging(log_directory=tmp_path)
    after_first = len(logging.getLogger().handlers)

    logging_setup.configure_logging(log_directory=tmp_path)

    assert len(logging.getLogger().handlers) == after_first


def test_rotating_handler_is_configured(tmp_path: Path) -> None:
    """ローテーション設定（サイズ上限と世代数）が入っている。"""
    logging_setup.configure_logging(log_directory=tmp_path)

    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if handler.get_name() == "sdp-rotating-file"
    ]
    assert len(handlers) == 1
    handler = handlers[0]
    assert getattr(handler, "maxBytes", None) == logging_setup.MAX_BYTES
    assert getattr(handler, "backupCount", None) == logging_setup.BACKUP_COUNT


def test_default_log_directory_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """通常は %LOCALAPPDATA%\\sdp\\logs を使う。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert logging_setup.default_log_directory() == tmp_path / "sdp" / "logs"


def test_default_log_directory_falls_back_without_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALAPPDATA が無い環境では一時ディレクトリ配下を使う。"""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    directory = logging_setup.default_log_directory()

    assert directory.name == "logs"
    assert directory.parent.name == "sdp"


def test_install_excepthook_logs_and_chains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """未捕捉例外をログへ記録し、元の excepthook も呼ぶ。"""
    monkeypatch.setattr(logging_setup, "_excepthook_installed", False)
    called: list[str] = []

    def previous_hook(*args: object) -> None:
        del args
        called.append("previous")

    monkeypatch.setattr("sys.excepthook", previous_hook)
    del tmp_path

    logging_setup.install_excepthook()
    import sys

    with caplog.at_level(logging.CRITICAL):
        try:
            raise RuntimeError("テスト用の未捕捉例外")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())  # type: ignore[arg-type]

    assert called == ["previous"]
    assert "未捕捉の例外" in caplog.text


def test_install_excepthook_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """多重インストールしない。"""
    import sys

    monkeypatch.setattr(logging_setup, "_excepthook_installed", False)
    original = sys.excepthook
    monkeypatch.setattr("sys.excepthook", original)

    logging_setup.install_excepthook()
    after_first = sys.excepthook
    logging_setup.install_excepthook()

    assert sys.excepthook is after_first

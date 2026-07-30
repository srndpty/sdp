"""sdp のエントリポイント。

依存の組み立てと UI の構築は :mod:`sdp.app` が行う。ここでは呼び出すだけにする。
"""

import logging
import sys

from sdp import app
from sdp.cli import CliMode, parse_cli_arguments
from sdp.selftest import run_selftest
from sdp.services import logging_setup

_logger = logging.getLogger(__name__)

INVALID_ARGUMENT_EXIT_CODE = 2


def main(argv: list[str] | None = None) -> int:
    """アプリを起動し、終了コードを返す。"""
    if argv is None:
        return app.run()
    raw_argv = list(argv)
    command = parse_cli_arguments(raw_argv[1:] if raw_argv else ())
    if command.mode is CliMode.SELFTEST:
        return run_selftest(raw_argv)
    if command.mode is CliMode.INVALID:
        logging_setup.configure_logging()
        logging_setup.install_excepthook()
        _logger.error("%s", command.error_message)
        return INVALID_ARGUMENT_EXIT_CODE
    return app.run(raw_argv)


def cli_main() -> int:
    """実processのargvを解析して起動するconsole entry point。"""
    return main(list(sys.argv))


if __name__ == "__main__":
    sys.exit(cli_main())

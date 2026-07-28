"""sdp のエントリポイント。

依存の組み立てと UI の構築は :mod:`sdp.app` が行う。ここでは呼び出すだけにする。
"""

import sys

from sdp import app


def main() -> int:
    """アプリを起動し、終了コードを返す。"""
    return app.run()


if __name__ == "__main__":
    sys.exit(main())

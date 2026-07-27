"""sdp のエントリポイント。

プレイヤー本体は未実装のため、現時点では開発中である旨を表示して正常終了する。
実際の起動処理は P1（再生基盤）以降で実装する。
"""

import sys

from sdp import __version__


def main() -> int:
    """開発中であることを表示して正常終了する。"""
    print(f"sdp {__version__} — 開発初期段階です。プレイヤー機能はまだ実装されていません。")
    print("開発計画: docs/development-plan.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

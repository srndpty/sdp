"""sdp (sound player) — Windows 11 向け個人用ローカル音声プレイヤー。

現在は開発初期段階であり、再生機能は未実装。
開発計画は docs/development-plan.md を参照。
"""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__: str = version("sdp")
except PackageNotFoundError:
    # 未インストール状態（リポジトリから直接実行した場合など）のフォールバック。
    __version__ = "0.0.0+unknown"

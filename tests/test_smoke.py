"""開発基盤が正しく構成されていることを確認する最小限のスモークテスト。"""

import sdp
from sdp.__main__ import main


def test_package_is_importable() -> None:
    """src レイアウトのパッケージが解決できることを確認する。"""
    assert sdp.__name__ == "sdp"


def test_version_string_is_available() -> None:
    """インストール済みメタデータからバージョン文字列を取得できることを確認する。"""
    assert isinstance(sdp.__version__, str)
    assert sdp.__version__


def test_entry_point_exits_normally() -> None:
    """`python -m sdp` 相当の処理が終了コード 0 で終わることを確認する。"""
    assert main() == 0

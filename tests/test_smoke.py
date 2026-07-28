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


def test_entry_point_is_callable() -> None:
    """エントリポイントが呼び出し可能であることを確認する。

    実際の起動は GUI のイベントループへ入るため、ここでは呼び出さない。
    app.run への委譲は tests/qt/test_app_wiring.py で検証する。
    """
    assert callable(main)

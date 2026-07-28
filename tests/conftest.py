"""テスト全体の共通設定。

- Qt を GUI なしで動かすため offscreen プラットフォームを既定にする
  （[docs/testing-strategy.md](../docs/testing-strategy.md) §3）。
- この conftest があることで pytest が `tests/` を sys.path へ追加するため、
  テストから `fakes.*` を import できる。
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TEST_AUDIO_DIR = Path(__file__).resolve().parent.parent / "assets" / "test_audio"


@pytest.fixture(scope="session")
def test_audio_dir() -> Path:
    """コミット済みの自己生成テスト音源のディレクトリ。

    生成には FFmpeg CLI が必要だが、音源自体はリポジトリへコミットしてあるため
    テスト実行時に FFmpeg を呼び出さない（[docs/p0-report.md](../docs/p0-report.md) §2.3）。
    """
    if not _TEST_AUDIO_DIR.is_dir():
        pytest.fail(f"テスト音源ディレクトリが見つかりません: {_TEST_AUDIO_DIR}")
    return _TEST_AUDIO_DIR

"""テスト全体の共通設定。

- Qt を GUI なしで動かすため offscreen プラットフォームを既定にする
  （[docs/testing-strategy.md](../docs/testing-strategy.md) §3）。
- この conftest があることで pytest が `tests/` を sys.path へ追加するため、
  テストから `fakes.*` を import できる。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

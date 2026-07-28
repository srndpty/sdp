"""プレイリスト。

エントリのデータ構造（`entry.py`）、Qt のモデル（`model.py`）、
JSON 永続化（`persistence.py`）で構成する。
再生状態（現在の曲・再生位置・リピート・シャッフル）は持たず、
PlaybackController が entry_id で参照する。
"""

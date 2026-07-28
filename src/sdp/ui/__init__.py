"""UI 層。

UI が触ってよいのは PlaybackController までとし、QtMultimediaBackend や
QMediaPlayer / QAudioOutput を直接 import・操作しない
（[AGENTS.md](../../../AGENTS.md) の責務分離の方針。テストで自動検査している）。
"""

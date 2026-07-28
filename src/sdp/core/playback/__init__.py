"""再生制御。

UI が触ってよいのは :class:`~sdp.core.playback.controller.PlaybackController` までで、
具体的な再生実装（QMediaPlayer 等）は :mod:`sdp.core.playback.backend` の
契約の背後に隠す（[AGENTS.md](../../../../AGENTS.md) の責務分離の方針）。
"""

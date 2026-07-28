"""再生バックエンドの契約。

将来の差し替え（ADR-0001 で不採用とした mpv 等）に必要な最小限だけを定義する。
実装は P1-B 以降で追加する QtMultimediaBackend と、テスト用の FakePlaybackBackend。

抽象基底に ``abc.ABC`` を使わないのは、PySide6 の ``QObject`` のメタクラスと
``ABCMeta`` が衝突するため。未実装メソッドは ``NotImplementedError`` で示す。
"""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from sdp.core.playback.types import MediaStatus, PlaybackError, PlaybackState


class PlaybackBackend(QObject):
    """1 つの音源を再生する実体への最小インターフェース。

    通知はすべてシグナルで行い、状態の問い合わせはプロパティで行う。
    プレイリストや UI の知識を持たない。
    """

    state_changed = Signal(PlaybackState)
    position_changed = Signal(int)
    """再生位置（ミリ秒）。"""

    duration_changed = Signal(int)
    """音源の総再生時間（ミリ秒）。未確定のあいだは 0。"""

    volume_changed = Signal(float)
    muted_changed = Signal(bool)
    playback_rate_changed = Signal(float)
    pitch_compensation_changed = Signal(bool)
    media_status_changed = Signal(MediaStatus)
    error_occurred = Signal(PlaybackError)

    # -- 操作 ---------------------------------------------------------------

    def load(self, path: Path) -> None:
        """音源を読み込む。

        存在検査は PlaybackController が済ませてある。実際に再生できるかどうかは
        読み込んでみるまで分からないため（ADR-0001 の制約 3）、
        失敗は :attr:`error_occurred` と :attr:`media_status_changed` で通知する。
        """
        raise NotImplementedError

    def play(self) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def seek(self, position_ms: int) -> None:
        """再生位置（ミリ秒）を変更する。値の妥当性は Controller が検証済み。"""
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        """音量を 0.0〜1.0 で設定する。値の妥当性は Controller が検証済み。"""
        raise NotImplementedError

    def set_muted(self, muted: bool) -> None:
        raise NotImplementedError

    def set_playback_rate(self, rate: float) -> None:
        """再生速度を設定する。

        読み戻し値は精度が落ちうる（ADR-0001 の制約 2）。要求値の真値は
        Controller が保持するため、Backend は読み戻し値をそのまま通知してよい。
        """
        raise NotImplementedError

    def set_pitch_compensation(self, enabled: bool) -> None:
        """ピッチ補正（time-stretch）の ON/OFF を設定する。"""
        raise NotImplementedError

    # -- 状態 ---------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        raise NotImplementedError

    @property
    def position_ms(self) -> int:
        raise NotImplementedError

    @property
    def duration_ms(self) -> int:
        raise NotImplementedError

    @property
    def volume(self) -> float:
        raise NotImplementedError

    @property
    def muted(self) -> bool:
        raise NotImplementedError

    @property
    def playback_rate(self) -> float:
        raise NotImplementedError

    @property
    def pitch_compensation(self) -> bool:
        raise NotImplementedError

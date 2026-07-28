"""PlaybackController のテスト用 Backend。

PlaybackBackend の契約のうち、テストに必要な範囲だけを実装する。
QtMultimediaBackend の内部実装（QMediaPlayer の状態遷移など）は模倣しない。
"""

import struct
from pathlib import Path

from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.types import MediaStatus, PlaybackError, PlaybackState


def to_float32(value: float) -> float:
    """float32 へ丸めた値を返す。

    QMediaPlayer の playbackRate 読み戻しが float32 精度であること
    （ADR-0001 の制約 2）を模擬するために使う。
    """
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


class FakePlaybackBackend(PlaybackBackend):
    """呼び出しを記録し、シグナルを任意に発火できる Backend。"""

    def __init__(
        self,
        *,
        state: PlaybackState = PlaybackState.NO_MEDIA,
        position_ms: int = 0,
        duration_ms: int = 0,
        volume: float = 1.0,
        muted: bool = False,
        playback_rate: float = 1.0,
        pitch_compensation: bool = True,
    ) -> None:
        super().__init__()
        self._state = state
        self._position_ms = position_ms
        self._duration_ms = duration_ms
        self._volume = volume
        self._muted = muted
        self._playback_rate = playback_rate
        self._pitch_compensation = pitch_compensation

        self.calls: list[tuple[str, tuple[object, ...]]] = []
        """呼び出された操作の記録。``("seek", (1500,))`` のような形。"""

        self.load_error: PlaybackError | None = None
        """設定すると load 時に error_occurred を発火する。"""

        self.float32_rate_readback = False
        """True にすると playback_rate_changed を float32 へ丸めて通知する。"""

        self.effective_volume: float | None = None
        self.effective_muted: bool | None = None
        self.effective_playback_rate: float | None = None
        self.effective_pitch_compensation: bool | None = None
        """設定時に同期通知する補正後の実効値。None の項目は要求値をそのまま使う。"""

        self.setter_errors: dict[str, Exception] = {}
        """操作名に対応する setter から送出するテスト用例外。"""

    # -- 記録の参照 ---------------------------------------------------------

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def call_args(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args in self.calls if called == name]

    # -- 操作 ---------------------------------------------------------------

    def load(self, path: Path) -> None:
        self.calls.append(("load", (path,)))
        if self.load_error is not None:
            self.error_occurred.emit(self.load_error)
            return
        self._state = PlaybackState.STOPPED
        self.media_status_changed.emit(MediaStatus.LOADED)
        self.state_changed.emit(self._state)

    def play(self) -> None:
        self.calls.append(("play", ()))
        self._set_state(PlaybackState.PLAYING)

    def pause(self) -> None:
        self.calls.append(("pause", ()))
        self._set_state(PlaybackState.PAUSED)

    def stop(self) -> None:
        self.calls.append(("stop", ()))
        self._set_state(PlaybackState.STOPPED)

    def seek(self, position_ms: int) -> None:
        self.calls.append(("seek", (position_ms,)))
        self.emit_position(position_ms)

    def set_volume(self, volume: float) -> None:
        self.calls.append(("set_volume", (volume,)))
        self._raise_setter_error("set_volume")
        self._volume = self.effective_volume if self.effective_volume is not None else volume
        self.volume_changed.emit(self._volume)

    def set_muted(self, muted: bool) -> None:
        self.calls.append(("set_muted", (muted,)))
        self._raise_setter_error("set_muted")
        self._muted = self.effective_muted if self.effective_muted is not None else muted
        self.muted_changed.emit(self._muted)

    def set_playback_rate(self, rate: float) -> None:
        self.calls.append(("set_playback_rate", (rate,)))
        self._raise_setter_error("set_playback_rate")
        effective_rate = (
            self.effective_playback_rate if self.effective_playback_rate is not None else rate
        )
        self._playback_rate = (
            to_float32(effective_rate) if self.float32_rate_readback else effective_rate
        )
        self.playback_rate_changed.emit(self._playback_rate)

    def set_pitch_compensation(self, enabled: bool) -> None:
        self.calls.append(("set_pitch_compensation", (enabled,)))
        self._raise_setter_error("set_pitch_compensation")
        self._pitch_compensation = (
            self.effective_pitch_compensation
            if self.effective_pitch_compensation is not None
            else enabled
        )
        self.pitch_compensation_changed.emit(self._pitch_compensation)

    # -- テストからの発火 ---------------------------------------------------

    def emit_position(self, position_ms: int) -> None:
        self._position_ms = position_ms
        self.position_changed.emit(position_ms)

    def emit_duration(self, duration_ms: int) -> None:
        self._duration_ms = duration_ms
        self.duration_changed.emit(duration_ms)

    def emit_state(self, state: PlaybackState) -> None:
        self._state = state
        self.state_changed.emit(state)

    def emit_media_status(self, status: MediaStatus) -> None:
        self.media_status_changed.emit(status)

    def emit_error(self, error: PlaybackError) -> None:
        self.error_occurred.emit(error)

    # -- 状態 ---------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def position_ms(self) -> int:
        return self._position_ms

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def playback_rate(self) -> float:
        return self._playback_rate

    @property
    def pitch_compensation(self) -> bool:
        return self._pitch_compensation

    # -- 内部 ---------------------------------------------------------------

    def _set_state(self, state: PlaybackState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _raise_setter_error(self, operation: str) -> None:
        error = self.setter_errors.get(operation)
        if error is not None:
            raise error

"""再生制御。UI から再生を操作する唯一の窓口。

UI は Backend を直接触らず、常に PlaybackController を経由する
（[AGENTS.md](../../../../AGENTS.md) の責務分離の方針）。
"""

import logging
import math
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
    PlaybackState,
)

_logger = logging.getLogger(__name__)

_RATE_RELATIVE_TOLERANCE = 1e-6
"""再生速度の比較許容誤差。

Backend からの読み戻しは float32 精度になりうる（ADR-0001 の制約 2。
45/33 で相対 1e-8 程度ずれる）ため、厳密な等値比較をしてはならない。
"""

_VOLUME_ABSOLUTE_TOLERANCE = 1e-6
"""音量の比較許容誤差。Qt 側で float 変換が挟まるため厳密比較を避ける。"""


class PlaybackController(QObject):
    """現在の音源と再生操作を管理する。

    責務:

    - 現在の source（``Path`` または ``None``）の保持
    - 操作の Backend への転送と、その前段での値の検証
    - Backend からの通知を UI 向けシグナルへ中継
    - ユーザーが要求した設定値の保持と、Backend が補正した実効値との同期
    - エラーの正規化（UI が受け取るのは常に :class:`PlaybackError` のみ）

    エラーの区別:

    - **ユーザー入力由来**（欠損ファイル、ディレクトリの指定）は例外ではなく
      :attr:`error_occurred` で通知する。UI はステータス表示とログで扱う。
    - **プログラミングエラー**（負のシーク位置、範囲外の音量、0 以下の再生速度）は
      ``ValueError`` を送出する。暗黙の clamp で呼び出し側のバグを隠さない。

    同じ値の再設定は、Backend を呼ばず通知もしない（no-op）ものとして統一する。
    """

    # Path | None と PlaybackState の合併は PySide6 の Signal では型指定できないため
    # object を使う。実際に流れるのは ``Path | None`` のみ。
    source_changed = Signal(object)

    state_changed = Signal(PlaybackState)
    position_changed = Signal(int)
    duration_changed = Signal(int)
    volume_changed = Signal(float)
    muted_changed = Signal(bool)
    playback_rate_changed = Signal(float)
    pitch_compensation_changed = Signal(bool)
    media_status_changed = Signal(MediaStatus)
    error_occurred = Signal(PlaybackError)

    def __init__(self, backend: PlaybackBackend, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._backend = backend
        self._source: Path | None = None

        # ユーザーが要求した値の真値。Backend の読み戻しより優先する。
        self._volume = backend.volume
        self._muted = backend.muted
        self._playback_rate = backend.playback_rate
        self._pitch_compensation = backend.pitch_compensation

        backend.state_changed.connect(self._on_backend_state_changed)
        backend.position_changed.connect(self._on_backend_position_changed)
        backend.duration_changed.connect(self._on_backend_duration_changed)
        backend.media_status_changed.connect(self._on_backend_media_status_changed)
        backend.volume_changed.connect(self._on_backend_volume_changed)
        backend.muted_changed.connect(self._on_backend_muted_changed)
        backend.playback_rate_changed.connect(self._on_backend_playback_rate_changed)
        backend.pitch_compensation_changed.connect(self._on_backend_pitch_compensation_changed)
        backend.error_occurred.connect(self._on_backend_error_occurred)

    # -- 現在の状態 ---------------------------------------------------------

    @property
    def source(self) -> Path | None:
        """現在読み込んでいる音源のパス。未読み込みなら ``None``。"""
        return self._source

    @property
    def state(self) -> PlaybackState:
        return self._backend.state

    @property
    def position_ms(self) -> int:
        return self._backend.position_ms

    @property
    def duration_ms(self) -> int:
        return self._backend.duration_ms

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def playback_rate(self) -> float:
        """再生速度の現在値。

        float32 の読み戻し誤差内ならユーザーの要求値を返す。許容誤差を超えて
        Backend が補正した場合は、その実効値を返す。
        """
        return self._playback_rate

    @property
    def pitch_compensation(self) -> bool:
        """ピッチ補正の要求値、または Backend が補正した実効値。"""
        return self._pitch_compensation

    # -- 操作 ---------------------------------------------------------------

    def load(self, path: Path) -> None:
        """音源を読み込む。

        拡張子や ``QMediaFormat`` の列挙で対応可否を判定しない
        （ADR-0001 の制約 3。列挙に無い Vorbis / Opus が実際には再生できた）。
        存在する通常ファイルであれば Backend へ渡し、実際の失敗は
        Backend からの :attr:`error_occurred` で扱う。

        読み込みに進めなかった場合は現在の source を変更せず、
        :attr:`error_occurred` で通知する。
        """
        try:
            resolved_path = path.resolve(strict=True)
        except OSError:
            self._emit_error(
                PlaybackErrorCode.SOURCE_NOT_FOUND,
                "ファイルが見つかりません。",
                f"存在しないパス: {path}",
            )
            return
        if resolved_path.is_dir():
            self._emit_error(
                PlaybackErrorCode.SOURCE_NOT_A_FILE,
                "フォルダーは再生できません。",
                f"ディレクトリが指定された: {resolved_path}",
            )
            return
        if not resolved_path.is_file():
            self._emit_error(
                PlaybackErrorCode.SOURCE_NOT_FOUND,
                "ファイルが見つかりません。",
                f"通常ファイルではないパス: {resolved_path}",
            )
            return

        # Backend.load() は media_status_changed などを同期通知しうる。
        # UI が新しい source を認識してから読み込み状態を受け取れるよう、先に通知する。
        self._source = resolved_path
        self.source_changed.emit(resolved_path)
        self._backend.load(resolved_path)

    def play(self) -> None:
        self._backend.play()

    def pause(self) -> None:
        self._backend.pause()

    def stop(self) -> None:
        self._backend.stop()

    def seek(self, position_ms: int) -> None:
        """再生位置（ミリ秒）を変更する。

        契約:

        - 負の位置は ``ValueError``。
        - duration が確定している（1 以上の）場合、duration を超える位置は ``ValueError``。
        - duration が未確定（0）の場合は上限を検証せず Backend へ転送する。
          読み込み直後は ``durationChanged`` がまだ届いておらず、
          「前回位置の復元」のような正当な seek まで拒否してしまうため。
          範囲外だった場合の扱いは Backend（Qt）に委ねる。
        """
        if position_ms < 0:
            raise ValueError(f"シーク位置が負です: {position_ms}")
        duration_ms = self._backend.duration_ms
        if duration_ms > 0 and position_ms > duration_ms:
            raise ValueError(
                f"シーク位置が duration({duration_ms}ms) を超えています: {position_ms}"
            )
        self._backend.seek(position_ms)

    def set_volume(self, volume: float) -> None:
        """音量を 0.0〜1.0 で設定する。範囲外と NaN は ``ValueError``。"""
        if math.isnan(volume) or not (0.0 <= volume <= 1.0):
            raise ValueError(f"音量は 0.0〜1.0 で指定してください: {volume}")
        if volume == self._volume:
            return
        previous = self._volume
        self._volume = volume
        try:
            self._backend.set_volume(volume)
        except Exception:
            self._volume = previous
            raise
        # Backend が同期的に異なる実効値を通知した場合、そのハンドラーが通知済み。
        if math.isclose(self._volume, volume, abs_tol=_VOLUME_ABSOLUTE_TOLERANCE):
            self.volume_changed.emit(volume)

    def set_muted(self, muted: bool) -> None:
        if muted == self._muted:
            return
        previous = self._muted
        self._muted = muted
        try:
            self._backend.set_muted(muted)
        except Exception:
            self._muted = previous
            raise
        if self._muted == muted:
            self.muted_changed.emit(muted)

    def set_playback_rate(self, rate: float) -> None:
        """再生速度を設定する。0 以下・NaN・無限大は ``ValueError``。

        UI 上の範囲（0.5〜2.0 倍）は UI の方針であり、ここでは強制しない。
        """
        if not math.isfinite(rate) or rate <= 0.0:
            raise ValueError(f"再生速度は 0 より大きい有限の値で指定してください: {rate}")
        if rate == self._playback_rate:
            return
        previous = self._playback_rate
        self._playback_rate = rate
        try:
            self._backend.set_playback_rate(rate)
        except Exception:
            self._playback_rate = previous
            raise
        if math.isclose(
            self._playback_rate,
            rate,
            rel_tol=_RATE_RELATIVE_TOLERANCE,
        ):
            self.playback_rate_changed.emit(rate)

    def set_pitch_compensation(self, enabled: bool) -> None:
        if enabled == self._pitch_compensation:
            return
        previous = self._pitch_compensation
        self._pitch_compensation = enabled
        try:
            self._backend.set_pitch_compensation(enabled)
        except Exception:
            self._pitch_compensation = previous
            raise
        if self._pitch_compensation == enabled:
            self.pitch_compensation_changed.emit(enabled)

    # -- Backend からの通知 -------------------------------------------------

    def _on_backend_state_changed(self, state: PlaybackState) -> None:
        self.state_changed.emit(state)

    def _on_backend_position_changed(self, position_ms: int) -> None:
        self.position_changed.emit(position_ms)

    def _on_backend_duration_changed(self, duration_ms: int) -> None:
        self.duration_changed.emit(duration_ms)

    def _on_backend_media_status_changed(self, status: MediaStatus) -> None:
        self.media_status_changed.emit(status)

    def _on_backend_volume_changed(self, volume: float) -> None:
        if math.isclose(volume, self._volume, abs_tol=_VOLUME_ABSOLUTE_TOLERANCE):
            return
        self._volume = volume
        self.volume_changed.emit(volume)

    def _on_backend_muted_changed(self, muted: bool) -> None:
        if muted == self._muted:
            return
        self._muted = muted
        self.muted_changed.emit(muted)

    def _on_backend_playback_rate_changed(self, rate: float) -> None:
        """Backend が通知してきた実効速度を照合する。

        読み戻しは float32 精度で要求値と厳密には一致しない（ADR-0001 の制約 2）。
        許容誤差内なら要求値を真値として保持し、UI へ再通知しない。
        誤差を超える場合は Backend 側で実際に別の値になったということなので、
        その値を新しい要求値として採用する。
        """
        if math.isclose(rate, self._playback_rate, rel_tol=_RATE_RELATIVE_TOLERANCE):
            return
        self._playback_rate = rate
        self.playback_rate_changed.emit(rate)

    def _on_backend_pitch_compensation_changed(self, enabled: bool) -> None:
        if enabled == self._pitch_compensation:
            return
        self._pitch_compensation = enabled
        self.pitch_compensation_changed.emit(enabled)

    def _on_backend_error_occurred(self, error: PlaybackError) -> None:
        _logger.error("再生エラー: %s (%s)", error.code.name, error.detail)
        self.error_occurred.emit(error)

    # -- 内部 ---------------------------------------------------------------

    def _emit_error(self, code: PlaybackErrorCode, message: str, detail: str) -> None:
        error = PlaybackError(code=code, message=message, detail=detail)
        _logger.error("再生エラー: %s (%s)", code.name, detail)
        self.error_occurred.emit(error)

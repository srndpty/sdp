"""Qt Multimedia による PlaybackBackend の実装。

ADR-0001 で採用した QMediaPlayer + QAudioOutput をこのモジュールへ閉じ込め、
Qt の enum・QUrl・例外を PlaybackBackend の契約へ変換する。
Controller と UI はこのモジュールを import しない。

QAudioBufferOutput（可視化用の PCM タップ）はここでは接続しない。
PcmTap とリングバッファは P5 の責務であり、将来用の未使用オブジェクトを
先に持たせない（[AGENTS.md](../../../../AGENTS.md) の方針）。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
    PlaybackState,
)

_logger = logging.getLogger(__name__)

_PLAYBACK_STATE_MAP: dict[QMediaPlayer.PlaybackState, PlaybackState] = {
    QMediaPlayer.PlaybackState.StoppedState: PlaybackState.STOPPED,
    QMediaPlayer.PlaybackState.PlayingState: PlaybackState.PLAYING,
    QMediaPlayer.PlaybackState.PausedState: PlaybackState.PAUSED,
}
"""Qt の再生状態からアプリ内状態への写像。

``NO_MEDIA`` は Qt 側に対応する値がなく、source の有無から Backend が判定する
（Qt は source 未設定でも ``StoppedState`` を返すため）。
"""

_MEDIA_STATUS_MAP: dict[QMediaPlayer.MediaStatus, MediaStatus] = {
    QMediaPlayer.MediaStatus.NoMedia: MediaStatus.NO_MEDIA,
    QMediaPlayer.MediaStatus.LoadingMedia: MediaStatus.LOADING,
    QMediaPlayer.MediaStatus.LoadedMedia: MediaStatus.LOADED,
    QMediaPlayer.MediaStatus.StalledMedia: MediaStatus.STALLED,
    QMediaPlayer.MediaStatus.BufferingMedia: MediaStatus.BUFFERING,
    QMediaPlayer.MediaStatus.BufferedMedia: MediaStatus.BUFFERED,
    QMediaPlayer.MediaStatus.EndOfMedia: MediaStatus.END_OF_MEDIA,
    QMediaPlayer.MediaStatus.InvalidMedia: MediaStatus.INVALID_MEDIA,
}
"""Qt のメディア状況からアプリ内 MediaStatus への写像（既知の全値を明示的に列挙する）。"""

_ERROR_CODE_MAP: dict[QMediaPlayer.Error, PlaybackErrorCode] = {
    QMediaPlayer.Error.ResourceError: PlaybackErrorCode.RESOURCE_ERROR,
    QMediaPlayer.Error.FormatError: PlaybackErrorCode.FORMAT_ERROR,
    QMediaPlayer.Error.NetworkError: PlaybackErrorCode.NETWORK_ERROR,
    QMediaPlayer.Error.AccessDeniedError: PlaybackErrorCode.ACCESS_DENIED,
}
"""Qt の再生エラーからアプリ内エラーコードへの写像。

``NoError`` は「エラーが無い」ことを表すため写像へ含めず、PlaybackError も作らない。
"""

_ERROR_MESSAGES: dict[PlaybackErrorCode, str] = {
    PlaybackErrorCode.RESOURCE_ERROR: "音声ファイルを読み込めません。",
    PlaybackErrorCode.FORMAT_ERROR: "この音声形式は再生できません。",
    PlaybackErrorCode.NETWORK_ERROR: "音声データの取得中にエラーが発生しました。",
    PlaybackErrorCode.ACCESS_DENIED: "音声ファイルへのアクセスが拒否されました。",
    PlaybackErrorCode.UNKNOWN_ERROR: "音声の再生中に不明なエラーが発生しました。",
}
"""ユーザー向けの日本語メッセージ。技術詳細は PlaybackError.detail 側へ入れる。"""


def _enum_name(value: object) -> str:
    """Qt enum の名前を返す。未知値では repr を返す（変換失敗のログ用）。"""
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else repr(value)


class QtMultimediaBackend(PlaybackBackend):
    """QMediaPlayer と QAudioOutput を所有し、PlaybackBackend の契約を満たす。

    所有する Qt オブジェクトは自身を parent にしており、本オブジェクトの破棄と
    ともに破棄される。外部へは公開しない（UI や Controller は Qt の型に触れない）。

    値の検証（範囲・NaN など）は PlaybackController の責務のため、ここでは
    重複した clamp や独自制限を行わず、Qt API が要求する型へ変換して転送する。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)

        # Qt は source 未設定でも StoppedState を返すため、NO_MEDIA と STOPPED は
        # Qt の playbackState だけでは区別できない。source の有無を自分で保持する。
        self._source_path: Path | None = None
        # 公開プロパティと「最後に通知した状態」を常に一致させるため、状態は
        # ここで保持し、_sync_state() でのみ更新と通知を同時に行う。
        self._state = PlaybackState.NO_MEDIA
        # 内部失敗の報告中に再入して error_occurred を出し続けないためのガード。
        self._reporting_failure = False

        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.errorOccurred.connect(self._on_error_occurred)
        self._player.playbackRateChanged.connect(self._on_playback_rate_changed)
        self._player.pitchCompensationChanged.connect(self._on_pitch_compensation_changed)
        self._audio_output.volumeChanged.connect(self._on_volume_changed)
        self._audio_output.mutedChanged.connect(self._on_muted_changed)

    # -- 操作 ---------------------------------------------------------------

    def load(self, path: Path) -> None:
        """ローカルファイルとして QMediaPlayer へ設定する。

        存在確認と通常ファイル確認は Controller が済ませているため重複させない。
        拡張子や ``QMediaFormat`` の列挙で対応可否を判定せず（ADR-0001 の制約 3）、
        読み込みの失敗は Qt の ``errorOccurred`` と ``mediaStatusChanged`` から通知する。

        ``setSource`` は同期的に ``mediaStatusChanged`` を出す一方で
        ``playbackStateChanged`` を出さないため、先に状態を評価して
        NO_MEDIA → STOPPED を 1 回だけ通知してから source を設定する。
        """
        self._source_path = path
        self._sync_state()
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        # setSource が再生を止めた場合（再生中の差し替え）に追随する。
        # 同じ値なら _sync_state() 側で重複通知を抑制する。
        self._sync_state()

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._player.stop()

    def seek(self, position_ms: int) -> None:
        self._player.setPosition(int(position_ms))

    def set_volume(self, volume: float) -> None:
        self._audio_output.setVolume(float(volume))

    def set_muted(self, muted: bool) -> None:
        self._audio_output.setMuted(bool(muted))

    def set_playback_rate(self, rate: float) -> None:
        self._player.setPlaybackRate(float(rate))

    def set_pitch_compensation(self, enabled: bool) -> None:
        self._player.setPitchCompensation(bool(enabled))

    # -- 状態 ---------------------------------------------------------------

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def position_ms(self) -> int:
        return int(self._player.position())

    @property
    def duration_ms(self) -> int:
        return int(self._player.duration())

    @property
    def volume(self) -> float:
        return float(self._audio_output.volume())

    @property
    def muted(self) -> bool:
        return bool(self._audio_output.isMuted())

    @property
    def playback_rate(self) -> float:
        """Qt からの読み戻し値。

        float32 精度になりうる（ADR-0001 の制約 2）。要求値の保持と許容誤差での
        照合は Controller の責務であり、ここでは真値を別途持たない。
        """
        return float(self._player.playbackRate())

    @property
    def pitch_compensation(self) -> bool:
        return bool(self._player.pitchCompensation())

    # -- Qt シグナルの変換 ---------------------------------------------------

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        # 引数の値だけでは NO_MEDIA と STOPPED を区別できないため、
        # source の有無を含めて評価し直す。
        self._sync_state(state)

    @Slot(int)
    def _on_position_changed(self, position_ms: int) -> None:
        self.position_changed.emit(int(position_ms))

    @Slot(int)
    def _on_duration_changed(self, duration_ms: int) -> None:
        self.duration_changed.emit(int(duration_ms))

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        try:
            mapped = _MEDIA_STATUS_MAP.get(status)
        except Exception:
            self._report_conversion_exception("MediaStatus")
            return
        if mapped is None:
            # 既知値を既定値へ丸めず、観測可能な失敗にする。
            self._report_internal_failure(f"未知の QMediaPlayer.MediaStatus: {_enum_name(status)}")
            return
        self.media_status_changed.emit(mapped)

    @Slot(QMediaPlayer.Error, str)
    def _on_error_occurred(self, error: QMediaPlayer.Error, error_string: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        try:
            code = _ERROR_CODE_MAP.get(error)
            if code is None:
                # 未知の Qt エラー値だけを UNKNOWN_ERROR として扱う（既知値は丸めない）。
                _logger.critical("未知の QMediaPlayer.Error: %s", _enum_name(error))
                code = PlaybackErrorCode.UNKNOWN_ERROR
            # 通常の再生エラーは Controller がログへ記録する契約のため、ここでは記録しない。
            playback_error = PlaybackError(
                code=code,
                message=_ERROR_MESSAGES[code],
                detail=(
                    f"QMediaPlayer.Error.{_enum_name(error)} / "
                    f"errorString={error_string!r} / source={self._source_path}"
                ),
            )
        except Exception:
            self._report_conversion_exception("Error")
            return
        self.error_occurred.emit(playback_error)

    @Slot(float)
    def _on_playback_rate_changed(self, rate: float) -> None:
        self.playback_rate_changed.emit(float(rate))

    @Slot(bool)
    def _on_pitch_compensation_changed(self, enabled: bool) -> None:
        self.pitch_compensation_changed.emit(bool(enabled))

    @Slot(float)
    def _on_volume_changed(self, volume: float) -> None:
        self.volume_changed.emit(float(volume))

    @Slot(bool)
    def _on_muted_changed(self, muted: bool) -> None:
        self.muted_changed.emit(bool(muted))

    # -- 内部 ---------------------------------------------------------------

    def _sync_state(self, qt_state: QMediaPlayer.PlaybackState | None = None) -> None:
        """状態を評価し、変化していれば 1 回だけ通知する。

        ``qt_state`` は playbackStateChanged が渡してきた値。省略時は
        QMediaPlayer の現在値を読む（load 時のように通知を伴わない評価で使う）。

        状態の保持と通知をここへ集約することで、``state`` プロパティと
        最後に通知した値が常に一致する。同値の重複通知もここで抑制する。
        """
        current_qt_state = self._player.playbackState() if qt_state is None else qt_state
        try:
            state = self._compute_state(current_qt_state)
        except Exception:
            self._report_conversion_exception("PlaybackState")
            return
        if state is None:
            self._report_internal_failure(
                f"未知の QMediaPlayer.PlaybackState: {_enum_name(current_qt_state)}"
            )
            return
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _compute_state(self, qt_state: QMediaPlayer.PlaybackState) -> PlaybackState | None:
        """状態を返す。変換できない場合は ``None``（状態を捏造しない）。

        Qt は source 未設定でも ``StoppedState`` を返すため、
        NO_MEDIA の判定は Qt の値ではなく source の有無で行う。
        """
        if self._source_path is None:
            return PlaybackState.NO_MEDIA
        return _PLAYBACK_STATE_MAP.get(qt_state)

    def _report_conversion_exception(self, boundary: str) -> None:
        """変換スロット内の予期しない例外を、スタックトレース付きで観測可能にする。

        PySide6 はスロット内の例外を握り潰して処理を継続するため、
        例外のまま逃がすと失敗が記録されずに再生だけが不整合になる。
        """
        _logger.exception("%s の変換で予期しない例外が発生", boundary)
        self._report_internal_failure(
            f"{boundary} の変換で例外が発生（詳細はログを参照）",
            write_log=False,
        )

    def _report_internal_failure(self, detail: str, *, write_log: bool = True) -> None:
        """変換境界での契約違反を、ログと UNKNOWN_ERROR で観測可能にする。

        PySide6 は Qt シグナルから呼ばれたスロット内の例外を呼び出し元へ伝播させず
        処理を継続する（P0-C で確認）。そのため変換の失敗を例外のまま逃がさず、
        ここで観測可能な失敗へ変換する。再入時は通知を繰り返さない。
        """
        if self._reporting_failure:
            _logger.critical("再入した Backend 内部エラーのため通知を抑制: %s", detail)
            return
        self._reporting_failure = True
        try:
            if write_log:
                _logger.critical("Backend 内部エラー: %s", detail)
            self.error_occurred.emit(
                PlaybackError(
                    code=PlaybackErrorCode.UNKNOWN_ERROR,
                    message=_ERROR_MESSAGES[PlaybackErrorCode.UNKNOWN_ERROR],
                    detail=f"{detail} / source={self._source_path}",
                )
            )
        finally:
            self._reporting_failure = False

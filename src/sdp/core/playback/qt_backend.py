"""Qt Multimedia による PlaybackBackend の実装。

ADR-0001 で採用した QMediaPlayer + QAudioOutput をこのモジュールへ閉じ込め、
Qt の enum・QUrl・例外を PlaybackBackend の契約へ変換する。
Controller と UI はこのモジュールを import しない。

可視化用の PCM 供給口として、load 世代ごとの QAudioBufferOutput と、世代を
検査済みの :attr:`QtMultimediaBackend.audio_buffer_received` を持つ。これは
PlaybackBackend の一般インターフェースには**含めない**（Qt Multimedia 固有の
補助ポートとして扱う。mpv へ差し替えた場合に持ち込めないため）。
接続するのは composition root と PcmTap だけで、UI 層は参照しない。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioOutput, QMediaPlayer

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


class _PlayerSession(QObject):
    """1 回の :meth:`QtMultimediaBackend.load` に対応する player の通知へ、その
    load の世代と source を添える中継。

    Qt のシグナル自体には発生元の source を識別する情報が無いため、世代は
    「通知を出した player の identity」へ結び付ける必要がある。Backend 側の
    可変フィールドを受信時に読むと、遅延通知にも現在世代が付いてしまう。

    closure（lambda）で束縛しない理由は、connection 側に Backend への強参照が
    残り、Backend と Qt オブジェクトが相互参照で破棄されなくなるため。
    この中継は player の子として生き、Backend を参照しない。

    PCM（``audioBufferReceived``）も同じ扱いにする。QAudioBuffer にも source を
    識別する情報が無く、音声出力側の buffering で遅れて届きうるため、Backend で
    1 つの QAudioBufferOutput を共有すると前 source の PCM を除外できない。
    """

    playback_state_changed = Signal(int, QMediaPlayer.PlaybackState)
    position_changed = Signal(int, int)
    duration_changed = Signal(int, int)
    media_status_changed = Signal(int, QMediaPlayer.MediaStatus)
    error_occurred = Signal(int, object, QMediaPlayer.Error, str)
    """(世代, source（``Path | None``）, Qt エラー, Qt のエラー文字列)。"""

    playback_rate_changed = Signal(int, float)
    pitch_compensation_changed = Signal(int, bool)
    audio_buffer_received = Signal(int, object)
    """(世代, 再生中PCMの ``QAudioBuffer``)。"""

    def __init__(
        self,
        player: QMediaPlayer,
        generation: int,
        source: Path | None,
        audio_buffer_output: QAudioBufferOutput,
    ) -> None:
        super().__init__(player)
        self._generation = generation
        self._source = source
        audio_buffer_output.audioBufferReceived.connect(self._on_audio_buffer_received)
        player.playbackStateChanged.connect(self._on_playback_state_changed)
        player.positionChanged.connect(self._on_position_changed)
        player.durationChanged.connect(self._on_duration_changed)
        player.mediaStatusChanged.connect(self._on_media_status_changed)
        player.errorOccurred.connect(self._on_error_occurred)
        player.playbackRateChanged.connect(self._on_playback_rate_changed)
        player.pitchCompensationChanged.connect(self._on_pitch_compensation_changed)

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        self.playback_state_changed.emit(self._generation, state)

    @Slot(int)
    def _on_position_changed(self, position_ms: int) -> None:
        self.position_changed.emit(self._generation, position_ms)

    @Slot(int)
    def _on_duration_changed(self, duration_ms: int) -> None:
        self.duration_changed.emit(self._generation, duration_ms)

    @Slot(QMediaPlayer.MediaStatus)
    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        self.media_status_changed.emit(self._generation, status)

    @Slot(QMediaPlayer.Error, str)
    def _on_error_occurred(self, error: QMediaPlayer.Error, error_string: str) -> None:
        self.error_occurred.emit(self._generation, self._source, error, error_string)

    @Slot(float)
    def _on_playback_rate_changed(self, rate: float) -> None:
        self.playback_rate_changed.emit(self._generation, rate)

    @Slot(bool)
    def _on_pitch_compensation_changed(self, enabled: bool) -> None:
        self.pitch_compensation_changed.emit(self._generation, enabled)

    @Slot(object)
    def _on_audio_buffer_received(self, buffer: object) -> None:
        self.audio_buffer_received.emit(self._generation, buffer)


class QtMultimediaBackend(PlaybackBackend):
    """QMediaPlayer と QAudioOutput を所有し、PlaybackBackend の契約を満たす。

    所有する Qt オブジェクトは自身を parent にしており、本オブジェクトの破棄と
    ともに破棄される。外部へは公開しない（UI や Controller は Qt の型に触れない）。

    **QMediaPlayer と QAudioBufferOutput は load ごとに作り直す。** Qt のシグナルは
    発生元の source を識別できないため、1 つの player・1 つの PCM 出力を使い回すと
    遅延通知の由来が分からなくなる（:meth:`load` を参照）。QAudioOutput だけは
    世代をまたいで同一のものを付け替える（音量・ミュートの実体を保つため）。
    PCM の受け手は :attr:`audio_buffer_received` へ接続するので、player を
    作り直しても接続先は変わらない。

    値の検証（範囲・NaN など）は PlaybackController の責務のため、ここでは
    重複した clamp や独自制限を行わず、Qt API が要求する型へ変換して転送する。
    """

    audio_buffer_received = Signal(object)
    """再生中PCM（``QAudioBuffer``）。現在の load 世代のものだけを通知する。

    Qt Multimedia 固有の補助ポートであり、:class:`PlaybackBackend` の契約ではない
    （mpv へ差し替えた場合に持ち込めないため）。接続するのは composition root と
    :class:`~sdp.services.pcm_tap.PcmTap` だけ。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio_output = QAudioOutput(self)

        # Qt は source 未設定でも StoppedState を返すため、NO_MEDIA と STOPPED は
        # Qt の playbackState だけでは区別できない。source の有無を自分で保持する。
        self._source_path: Path | None = None
        # 直近の load() が渡した読み込み世代。現在世代のplayerを識別するために使う。
        self._load_generation = 0
        # 公開プロパティと「最後に通知した状態」を常に一致させるため、状態は
        # ここで保持し、_sync_state() でのみ更新と通知を同時に行う。
        self._state = PlaybackState.NO_MEDIA
        # 内部失敗の報告中に再入して error_occurred を出し続けないためのガード。
        self._reporting_failure = False
        self._player = self._create_player(self._load_generation)
        self._audio_output.volumeChanged.connect(self._on_volume_changed)
        self._audio_output.mutedChanged.connect(self._on_muted_changed)

    # -- 操作 ---------------------------------------------------------------

    def load(self, path: Path, generation: int) -> None:
        """ローカルファイルとして、この世代専用の QMediaPlayer へ設定する。

        存在確認と通常ファイル確認は Controller が済ませているため重複させない。
        拡張子や ``QMediaFormat`` の列挙で対応可否を判定せず（ADR-0001 の制約 3）、
        読み込みの失敗は Qt の ``errorOccurred`` と ``mediaStatusChanged`` から通知する。

        **load ごとに QMediaPlayer（と PCM 出力）を作り直す。** Qt のシグナルには
        発生元の source を識別する情報が無く、1 つの QMediaPlayer を使い回すと、
        前 source から遅れて届いた通知にも「現在の世代」を後付けしてしまう
        （受信時の可変フィールドを読むことになるため）。世代を player の identity へ
        結び付け、player の子である :class:`_PlayerSession` が保持することで、
        旧 player の遅延通知は旧世代のまま通知される。

        旧 player は停止して破棄予約する（出力は明示的に外さない。
        :meth:`_retire_player`）。タイムラインの 0 へのリセットは、旧世代の通知として
        捨てられるためここで通知し直す。
        """
        previous = self._player
        previous_position_ms = previous.position()
        previous_duration_ms = previous.duration()
        self._source_path = path
        # 以後の通知はこの世代のものとして扱う。旧playerの後始末が同期で通知を
        # 出しうるため、旧playerの破棄予約より前に更新する。
        self._load_generation = generation
        self._retire_player(previous)
        # 再生速度とピッチ補正は QMediaPlayer が持つため、新しい player へ引き継ぐ。
        self._player = self._create_player(
            generation,
            playback_rate=previous.playbackRate(),
            pitch_compensation=previous.pitchCompensation(),
        )
        if previous_position_ms != 0:
            self.position_changed.emit(0)
        if previous_duration_ms != 0:
            self.duration_changed.emit(0)
        # 新しい player は StoppedState のため、NO_MEDIA → STOPPED を 1 回だけ通知する。
        self._sync_state()
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        # setSource が状態を変えた場合に追随する。
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

    # -- Qt 固有の補助ポート -------------------------------------------------

    def _on_audio_buffer_received(self, generation: int, buffer: object) -> None:
        """現在世代のPCMだけを供給口へ流す。

        QAudioBuffer にも source を識別する情報が無く、音声出力側の buffering で
        遅れて届きうる（Qt の QAudioBufferOutput ドキュメント）。旧 player の
        PCM を新しい source のものとして可視化へ混ぜない。
        """
        if generation != self._load_generation:
            return
        self.audio_buffer_received.emit(buffer)

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
    #
    # 各ハンドラーの ``generation`` は、通知を出した QMediaPlayer を作ったときの
    # 読み込み世代（player の子である _PlayerSession が保持する）。受信時点の可変フィールドでは
    # ないため、旧 player の遅延通知でも旧世代のまま届く。

    def _on_playback_state_changed(
        self, generation: int, state: QMediaPlayer.PlaybackState
    ) -> None:
        if generation != self._load_generation:
            # 破棄待ちの旧 player が出した状態。現在の音源の状態ではない。
            return
        # 引数の値だけでは NO_MEDIA と STOPPED を区別できないため、
        # source の有無を含めて評価し直す。
        self._sync_state(state)

    def _on_position_changed(self, generation: int, position_ms: int) -> None:
        if generation != self._load_generation:
            return
        self.position_changed.emit(int(position_ms))

    def _on_duration_changed(self, generation: int, duration_ms: int) -> None:
        if generation != self._load_generation:
            return
        self.duration_changed.emit(int(duration_ms))

    def _on_media_status_changed(self, generation: int, status: QMediaPlayer.MediaStatus) -> None:
        try:
            mapped = _MEDIA_STATUS_MAP.get(status)
        except Exception:
            self._report_conversion_exception("MediaStatus")
            return
        if mapped is None:
            # 既知値を既定値へ丸めず、観測可能な失敗にする。
            self._report_internal_failure(f"未知の QMediaPlayer.MediaStatus: {_enum_name(status)}")
            return
        # 旧世代でも捨てずに通知する。除外は受け手（PlaybackController）の責務で、
        # Backend の責務は「どの load 由来かを正しく添えること」。
        self.media_status_changed.emit(mapped, generation)

    def _on_error_occurred(
        self,
        generation: int,
        source: Path | None,
        error: QMediaPlayer.Error,
        error_string: str,
    ) -> None:
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
                    f"errorString={error_string!r} / source={source}"
                ),
                # 現在の source ではなく、エラーを出した player の source を添える。
                generation=generation,
                source=source,
            )
        except Exception:
            self._report_conversion_exception("Error")
            return
        self.error_occurred.emit(playback_error)

    def _on_playback_rate_changed(self, generation: int, rate: float) -> None:
        if generation != self._load_generation:
            return
        self.playback_rate_changed.emit(float(rate))

    def _on_pitch_compensation_changed(self, generation: int, enabled: bool) -> None:
        if generation != self._load_generation:
            return
        self.pitch_compensation_changed.emit(bool(enabled))

    @Slot(float)
    def _on_volume_changed(self, volume: float) -> None:
        self.volume_changed.emit(float(volume))

    @Slot(bool)
    def _on_muted_changed(self, muted: bool) -> None:
        self.muted_changed.emit(bool(muted))

    # -- 内部: player の世代交代 ---------------------------------------------

    def _create_player(
        self,
        generation: int,
        *,
        playback_rate: float | None = None,
        pitch_compensation: bool | None = None,
    ) -> QMediaPlayer:
        """この世代専用の QMediaPlayer を作り、世代を固定して接続する。

        速度とピッチ補正は QMediaPlayer 側の状態のため、接続より前に引き継ぐ
        （引き継ぎ自体を変更として通知しない）。
        """
        player = QMediaPlayer(self)
        player.setAudioOutput(self._audio_output)
        # format を指定しない QAudioBufferOutput は「デコード直後のネイティブ形式」
        # を通知する（再サンプリングを挟まない）。音声出力は QAudioOutput 側で
        # 従来どおり継続する（P0-C §8、P5-A probe で実測）。
        # player の子にすることで、旧世代の PCM 出力は旧 player とともに消える。
        audio_buffer_output = QAudioBufferOutput(player)
        player.setAudioBufferOutput(audio_buffer_output)
        if playback_rate is not None:
            player.setPlaybackRate(playback_rate)
        if pitch_compensation is not None:
            player.setPitchCompensation(pitch_compensation)

        # 世代と source は player の子である中継が保持する（受信時に読み直さない）。
        session = _PlayerSession(player, generation, self._source_path, audio_buffer_output)
        session.playback_state_changed.connect(self._on_playback_state_changed)
        session.position_changed.connect(self._on_position_changed)
        session.duration_changed.connect(self._on_duration_changed)
        session.media_status_changed.connect(self._on_media_status_changed)
        session.error_occurred.connect(self._on_error_occurred)
        session.playback_rate_changed.connect(self._on_playback_rate_changed)
        session.pitch_compensation_changed.connect(self._on_pitch_compensation_changed)
        session.audio_buffer_received.connect(self._on_audio_buffer_received)
        return player

    def _retire_player(self, player: QMediaPlayer) -> None:
        """旧世代の player を停止し、破棄を予約する。

        シグナル接続は切らない。旧 player から遅れて届く通知を、旧世代の通知として
        観測可能にするため（受け手が世代で除外できる）。破棄されるまでのあいだ
        音を出し続けないよう、停止する。

        親（Backend）からは外さない。``setParent(None)`` で Python 所有へ移すと、
        参照が切れた時点で C++ デストラクタが即座に走り、デコード中の player を
        その場で壊しうる。所有は Qt 側に残したまま、event loop の次のターンで
        破棄させる。

        音声出力は、ここで ``None`` を設定して外さない。同じ ``QAudioOutput`` を
        新しい player へ設定した時点で Qt 側が旧 player から外すため、明示的な
        切り離しを重ねると二重に外れて異常終了する（ADR-0001 の制約 13。
        ``QAudioBufferOutput`` で実測）。停止だけしておけば、破棄までのあいだに
        音は出ない。PCM 出力は player の子なので、破棄と同時に消える。
        """
        player.stop()
        player.deleteLater()

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
                # 変換境界の契約違反はプログラミングエラーであり、特定のsourceに
                # 属さない。世代を添えず、受け手の世代フィルターで消さない。
                PlaybackError(
                    code=PlaybackErrorCode.UNKNOWN_ERROR,
                    message=_ERROR_MESSAGES[PlaybackErrorCode.UNKNOWN_ERROR],
                    detail=f"{detail} / source={self._source_path}",
                )
            )
        finally:
            self._reporting_failure = False

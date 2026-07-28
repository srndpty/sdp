# pyright: reportPrivateUsage=false
"""QtMultimediaBackend の契約を、音声デバイスなしで検証する。

実際に音を鳴らす再生パイプラインの検証は `tests/audio/` 側（audio マーカー）にある。

Qt 由来の通知は、所有している QMediaPlayer / QAudioOutput の Qt シグナルを
テストから直接発火させて再現する。これらは `QObject.findChildren` で取得しており、
テストのために Backend の公開 API を増やしてはいない。
"""

import gc
import logging
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from sdp.core.playback import qt_backend as qt_backend_module
from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
    PlaybackState,
)

LOAD_TIMEOUT_MS = 10_000
"""load 完了を待つ上限。失敗時に無期限で待たないために明示する。"""


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[QtMultimediaBackend]:
    del qtbot  # QApplication の用意のみに使う
    instance = QtMultimediaBackend()
    yield instance


def player_of(backend: QtMultimediaBackend) -> QMediaPlayer:
    """所有されている QMediaPlayer を QObject の標準 API で取り出す（テスト専用）。"""
    children = backend.findChildren(QMediaPlayer)
    assert len(children) == 1
    return children[0]


def audio_output_of(backend: QtMultimediaBackend) -> QAudioOutput:
    children = backend.findChildren(QAudioOutput)
    assert len(children) == 1
    return children[0]


def write_silent_wav(path: Path, duration_ms: int) -> None:
    """指定した長さの無音WAVをテスト用に生成する。"""
    sample_rate = 8_000
    frame_count = sample_rate * duration_ms // 1_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frame_count)


# -- 生成と初期値 -----------------------------------------------------------


def test_backend_can_be_created(backend: QtMultimediaBackend) -> None:
    """PlaybackBackend の実装として生成できる。"""
    assert isinstance(backend, PlaybackBackend)


def test_initial_state_is_no_media(backend: QtMultimediaBackend) -> None:
    """source 未設定の初期状態は NO_MEDIA。"""
    assert backend.state is PlaybackState.NO_MEDIA


def test_initial_position_and_duration_are_zero(backend: QtMultimediaBackend) -> None:
    """初期の位置と長さは 0 ミリ秒。"""
    assert backend.position_ms == 0
    assert backend.duration_ms == 0


def test_qt_objects_are_owned_and_not_exposed(backend: QtMultimediaBackend) -> None:
    """QMediaPlayer / QAudioOutput を子として所有し、公開プロパティにしていない。"""
    assert len(backend.findChildren(QMediaPlayer)) == 1
    assert len(backend.findChildren(QAudioOutput)) == 1
    exposed = [name for name in dir(backend) if not name.startswith("_")]
    for name in exposed:
        assert not isinstance(getattr(backend, name), QMediaPlayer | QAudioOutput)


def test_all_backend_operations_are_overridden() -> None:
    """PlaybackBackend の操作とプロパティをすべて実装している。

    基底は ABCMeta ではなく NotImplementedError による契約のため、
    実装漏れを型検査では検出できない。上書きの有無で確認する。
    """
    members = [
        "load",
        "play",
        "pause",
        "stop",
        "seek",
        "set_volume",
        "set_muted",
        "set_playback_rate",
        "set_pitch_compensation",
        "state",
        "position_ms",
        "duration_ms",
        "volume",
        "muted",
        "playback_rate",
        "pitch_compensation",
    ]
    for name in members:
        assert getattr(QtMultimediaBackend, name) is not getattr(PlaybackBackend, name), name


# -- enum 写像の完全性 ------------------------------------------------------


def test_playback_state_map_covers_all_qt_values() -> None:
    """Qt の PlaybackState 全値が明示的に写像されている（値が増えたら失敗する）。"""
    assert set(qt_backend_module._PLAYBACK_STATE_MAP) == set(QMediaPlayer.PlaybackState)


def test_media_status_map_covers_all_qt_values() -> None:
    """Qt の MediaStatus 全値が明示的に写像され、既定値へ丸めていない。"""
    assert set(qt_backend_module._MEDIA_STATUS_MAP) == set(QMediaPlayer.MediaStatus)
    assert set(qt_backend_module._MEDIA_STATUS_MAP.values()) == set(MediaStatus)


def test_error_map_covers_all_qt_errors_except_no_error() -> None:
    """NoError を除く Qt の全エラー値が明示的に写像されている。"""
    assert set(qt_backend_module._ERROR_CODE_MAP) == set(QMediaPlayer.Error) - {
        QMediaPlayer.Error.NoError
    }
    assert QMediaPlayer.Error.NoError not in qt_backend_module._ERROR_CODE_MAP
    assert PlaybackErrorCode.UNKNOWN_ERROR not in qt_backend_module._ERROR_CODE_MAP.values()


def test_every_reported_error_code_has_a_japanese_message() -> None:
    """Backend が通知しうる全コードにユーザー向けメッセージがある。"""
    reported = set(qt_backend_module._ERROR_CODE_MAP.values()) | {PlaybackErrorCode.UNKNOWN_ERROR}
    assert reported <= set(qt_backend_module._ERROR_MESSAGES)
    for message in qt_backend_module._ERROR_MESSAGES.values():
        assert message.endswith("。")


# -- エラー変換 -------------------------------------------------------------


def test_no_error_does_not_report_a_playback_error(backend: QtMultimediaBackend) -> None:
    """NoError からは PlaybackError を作らない。"""
    spy = QSignalSpy(backend.error_occurred)

    player_of(backend).errorOccurred.emit(QMediaPlayer.Error.NoError, "")

    assert spy.count() == 0


@pytest.mark.parametrize(
    ("qt_error", "expected_code", "expected_message"),
    [
        (
            QMediaPlayer.Error.ResourceError,
            PlaybackErrorCode.RESOURCE_ERROR,
            "音声ファイルを読み込めません。",
        ),
        (
            QMediaPlayer.Error.FormatError,
            PlaybackErrorCode.FORMAT_ERROR,
            "この音声形式は再生できません。",
        ),
        (
            QMediaPlayer.Error.NetworkError,
            PlaybackErrorCode.NETWORK_ERROR,
            "音声データの取得中にエラーが発生しました。",
        ),
        (
            QMediaPlayer.Error.AccessDeniedError,
            PlaybackErrorCode.ACCESS_DENIED,
            "音声ファイルへのアクセスが拒否されました。",
        ),
    ],
)
def test_qt_error_is_converted(
    backend: QtMultimediaBackend,
    test_audio_dir: Path,
    qt_error: QMediaPlayer.Error,
    expected_code: PlaybackErrorCode,
    expected_message: str,
) -> None:
    """Qt の各エラーがコード・日本語メッセージ・技術詳細へ変換される。"""
    source = test_audio_dir / "sine440.wav"
    backend.load(source)
    spy = QSignalSpy(backend.error_occurred)

    player_of(backend).errorOccurred.emit(qt_error, "backend error text")

    assert spy.count() == 1
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is expected_code
    assert error.message == expected_message
    # detail には Qt の enum 名・errorString・現在の source を含める。
    assert qt_error.name in error.detail
    assert "backend error text" in error.detail
    assert str(source) in error.detail
    # 技術詳細はユーザー向けメッセージへ混ぜない。
    assert error.message not in error.detail


def test_missing_media_status_mapping_reports_internal_error(
    backend: QtMultimediaBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MediaStatus写像の欠落をUNKNOWN_ERRORとして通知する。"""
    monkeypatch.delitem(
        qt_backend_module._MEDIA_STATUS_MAP,
        QMediaPlayer.MediaStatus.LoadedMedia,
    )
    spy = QSignalSpy(backend.error_occurred)

    backend._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)

    assert spy.count() == 1
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is PlaybackErrorCode.UNKNOWN_ERROR
    assert "LoadedMedia" in error.detail


def test_state_conversion_exception_reports_error_without_fabricating_state(
    backend: QtMultimediaBackend,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """状態変換の例外を記録し、公開stateを変更しない。"""

    def fail_conversion(
        _self: QtMultimediaBackend, _state: QMediaPlayer.PlaybackState
    ) -> PlaybackState | None:
        raise RuntimeError("injected conversion failure")

    monkeypatch.setattr(QtMultimediaBackend, "_compute_state", fail_conversion)
    spy = QSignalSpy(backend.error_occurred)

    with caplog.at_level(logging.ERROR):
        backend._on_playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)

    assert backend.state is PlaybackState.NO_MEDIA
    assert spy.count() == 1
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is PlaybackErrorCode.UNKNOWN_ERROR
    assert "PlaybackState の変換で例外" in error.detail
    assert "injected conversion failure" in caplog.text
    assert caplog.text.count("Backend 内部エラー") == 0


def test_missing_error_message_reports_conversion_failure(
    backend: QtMultimediaBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """エラーメッセージ写像の欠落を変換失敗として通知する。"""
    monkeypatch.delitem(
        qt_backend_module._ERROR_MESSAGES,
        PlaybackErrorCode.FORMAT_ERROR,
    )
    spy = QSignalSpy(backend.error_occurred)

    backend._on_error_occurred(QMediaPlayer.Error.FormatError, "injected error")

    assert spy.count() == 1
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is PlaybackErrorCode.UNKNOWN_ERROR
    assert "Error の変換で例外" in error.detail


def test_internal_failure_reentry_is_suppressed(
    backend: QtMultimediaBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """内部エラー通知中の再入でerror_occurredを繰り返さない。"""
    spy = QSignalSpy(backend.error_occurred)

    def reenter(_error: PlaybackError) -> None:
        backend._report_internal_failure("再入側")

    backend.error_occurred.connect(reenter)

    with caplog.at_level(logging.CRITICAL):
        backend._report_internal_failure("外側")

    assert spy.count() == 1
    assert "再入した Backend 内部エラーのため通知を抑制: 再入側" in caplog.text


# -- 状態の変換と重複抑制 ---------------------------------------------------


def test_first_load_reports_stopped_once(
    backend: QtMultimediaBackend, test_audio_dir: Path
) -> None:
    """初回 load で NO_MEDIA から STOPPED へ 1 回だけ通知する。"""
    spy = QSignalSpy(backend.state_changed)

    backend.load(test_audio_dir / "sine440.wav")

    assert spy.count() == 1
    assert spy.at(0)[0] is PlaybackState.STOPPED
    assert backend.state is PlaybackState.STOPPED


def test_repeated_stopped_state_is_not_reported_twice(
    backend: QtMultimediaBackend, test_audio_dir: Path
) -> None:
    """Qt から同じ StoppedState が来ても STOPPED を重複通知しない。"""
    backend.load(test_audio_dir / "sine440.wav")
    spy = QSignalSpy(backend.state_changed)

    player_of(backend).playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)
    player_of(backend).playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)

    assert spy.count() == 0
    assert backend.state is PlaybackState.STOPPED


def test_state_property_matches_last_emitted_state(
    backend: QtMultimediaBackend, test_audio_dir: Path
) -> None:
    """公開 state と最後に通知した状態が常に一致する。"""
    backend.load(test_audio_dir / "sine440.wav")
    spy = QSignalSpy(backend.state_changed)
    player = player_of(backend)

    for qt_state, expected in [
        (QMediaPlayer.PlaybackState.PlayingState, PlaybackState.PLAYING),
        (QMediaPlayer.PlaybackState.PausedState, PlaybackState.PAUSED),
        (QMediaPlayer.PlaybackState.StoppedState, PlaybackState.STOPPED),
    ]:
        player.playbackStateChanged.emit(qt_state)
        assert backend.state is expected
        assert spy.at(spy.count() - 1)[0] is expected

    assert spy.count() == 3


def test_state_without_source_stays_no_media(backend: QtMultimediaBackend) -> None:
    """source 未設定のあいだは Qt の StoppedState を STOPPED と解釈しない。"""
    spy = QSignalSpy(backend.state_changed)

    player_of(backend).playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)

    assert spy.count() == 0
    assert backend.state is PlaybackState.NO_MEDIA


# -- 通知の中継 -------------------------------------------------------------


def test_position_and_duration_are_relayed_as_int_milliseconds(
    backend: QtMultimediaBackend,
) -> None:
    """position / duration がミリ秒の int として中継される。"""
    position_spy = QSignalSpy(backend.position_changed)
    duration_spy = QSignalSpy(backend.duration_changed)
    player = player_of(backend)

    player.positionChanged.emit(1234)
    player.durationChanged.emit(9876)

    assert position_spy.at(0)[0] == 1234
    assert duration_spy.at(0)[0] == 9876


def test_media_status_is_converted(backend: QtMultimediaBackend) -> None:
    """Qt の MediaStatus がアプリ内 MediaStatus へ変換されて中継される。"""
    spy = QSignalSpy(backend.media_status_changed)

    player_of(backend).mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)

    assert spy.at(0)[0] is MediaStatus.END_OF_MEDIA


def test_volume_and_muted_changes_are_relayed(backend: QtMultimediaBackend) -> None:
    """音量とミュートの変更が中継され、プロパティへ反映される。"""
    volume_spy = QSignalSpy(backend.volume_changed)
    muted_spy = QSignalSpy(backend.muted_changed)

    backend.set_volume(0.25)
    backend.set_muted(True)

    assert volume_spy.count() == 1
    assert volume_spy.at(0)[0] == pytest.approx(0.25)
    assert backend.volume == pytest.approx(0.25)
    assert muted_spy.at(0)[0] is True
    assert backend.muted is True


def test_playback_rate_readback_is_relayed_as_is(backend: QtMultimediaBackend) -> None:
    """Qt の読み戻し値をそのまま中継する（要求値の保持は Controller の責務）。"""
    spy = QSignalSpy(backend.playback_rate_changed)
    requested = 45 / 33

    backend.set_playback_rate(requested)

    assert spy.count() == 1
    reported = spy.at(0)[0]
    assert reported == backend.playback_rate
    # ADR-0001 の制約 2。Backend 側で要求値を真値として持ち直していない。
    assert reported != requested
    assert reported == pytest.approx(requested, rel=1e-6)


def test_pitch_compensation_change_is_relayed(backend: QtMultimediaBackend) -> None:
    """ピッチ補正の変更が中継される。"""
    spy = QSignalSpy(backend.pitch_compensation_changed)

    backend.set_pitch_compensation(False)

    assert spy.count() == 1
    assert spy.at(0)[0] is False
    assert backend.pitch_compensation is False


def test_transport_operations_without_source_do_not_fabricate_state(
    backend: QtMultimediaBackend,
) -> None:
    """source 未設定での play / pause / stop は状態を捏造しない。

    Controller は再生操作を無条件に転送する契約のため、Backend 側が
    「音源が無いのに PLAYING」のような状態を作らないことを確認する。
    """
    state_spy = QSignalSpy(backend.state_changed)

    backend.play()
    backend.pause()
    backend.stop()

    assert state_spy.count() == 0
    assert backend.state is PlaybackState.NO_MEDIA


def test_seek_is_forwarded_to_the_player(
    backend: QtMultimediaBackend, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """seek が QMediaPlayer の位置設定へ転送される。"""
    backend.load(test_audio_dir / "sine440.wav")
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)

    backend.seek(1000)

    assert backend.position_ms == 1000


# -- 破棄 -------------------------------------------------------------------


def test_owned_qt_objects_are_destroyed_with_the_backend(qtbot: QtBot) -> None:
    """Backend の破棄で QMediaPlayer と QAudioOutput も破棄される。"""
    del qtbot
    backend = QtMultimediaBackend()
    destroyed: list[str] = []

    def record_player(*_: object) -> None:
        destroyed.append("player")

    def record_audio_output(*_: object) -> None:
        destroyed.append("audio_output")

    player_of(backend).destroyed.connect(record_player)
    audio_output_of(backend).destroyed.connect(record_audio_output)

    del backend
    gc.collect()

    assert sorted(destroyed) == ["audio_output", "player"]


# -- load（音を鳴らさない Qt 統合） ------------------------------------------


@pytest.mark.parametrize("relative", ["sine440.wav", "日本語 ディレクトリ/テスト 音源 440Hz.wav"])
def test_load_reaches_loaded_media_without_playing(
    backend: QtMultimediaBackend, test_audio_dir: Path, qtbot: QtBot, relative: str
) -> None:
    """再生を開始せずに読み込みが完了し、duration を取得できる（日本語・空白パス含む）。"""
    source = test_audio_dir / relative
    assert source.is_file(), source
    error_spy = QSignalSpy(backend.error_occurred)
    status_spy = QSignalSpy(backend.media_status_changed)

    backend.load(source)

    assert backend.state is PlaybackState.STOPPED
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    statuses = [status_spy.at(index)[0] for index in range(status_spy.count())]
    assert MediaStatus.LOADING in statuses or MediaStatus.LOADED in statuses
    assert statuses[-1] in {MediaStatus.LOADED, MediaStatus.BUFFERED, MediaStatus.BUFFERING}
    assert error_spy.count() == 0


@pytest.mark.parametrize(
    ("qt_state", "expected_before"),
    [
        (QMediaPlayer.PlaybackState.PlayingState, PlaybackState.PLAYING),
        (QMediaPlayer.PlaybackState.PausedState, PlaybackState.PAUSED),
    ],
)
def test_replacing_source_stops_once_and_resets_timeline(
    backend: QtMultimediaBackend,
    test_audio_dir: Path,
    tmp_path: Path,
    qtbot: QtBot,
    qt_state: QMediaPlayer.PlaybackState,
    expected_before: PlaybackState,
) -> None:
    """再生中・一時停止中のAをBへ差し替え、停止とタイムライン更新を一度だけ通知する。"""
    source_a = test_audio_dir / "sine440.wav"
    source_b = tmp_path / "短いB.wav"
    write_silent_wav(source_b, 750)
    player = player_of(backend)

    backend.load(source_a)
    qtbot.waitUntil(lambda: backend.duration_ms > 1_500, timeout=LOAD_TIMEOUT_MS)
    backend.seek(1_000)
    qtbot.waitUntil(lambda: backend.position_ms >= 900, timeout=LOAD_TIMEOUT_MS)
    player.playbackStateChanged.emit(qt_state)
    assert backend.state is expected_before

    state_spy = QSignalSpy(backend.state_changed)
    position_spy = QSignalSpy(backend.position_changed)
    duration_spy = QSignalSpy(backend.duration_changed)
    status_spy = QSignalSpy(backend.media_status_changed)
    error_spy = QSignalSpy(backend.error_occurred)

    backend.load(source_b)

    assert backend.state is PlaybackState.STOPPED
    assert state_spy.count() == 1
    assert state_spy.at(0)[0] is PlaybackState.STOPPED
    assert backend.position_ms == 0
    qtbot.waitUntil(lambda: 600 <= backend.duration_ms <= 900, timeout=LOAD_TIMEOUT_MS)
    statuses = [status_spy.at(index)[0] for index in range(status_spy.count())]
    assert statuses[-1] is MediaStatus.LOADED
    assert position_spy.count() >= 1
    assert position_spy.at(position_spy.count() - 1)[0] == 0
    assert duration_spy.count() >= 1
    assert duration_spy.at(duration_spy.count() - 1)[0] == backend.duration_ms
    assert Path(player.source().toLocalFile()) == source_b
    assert error_spy.count() == 0


def test_reloading_same_source_resets_position_without_reloading_media(
    backend: QtMultimediaBackend, test_audio_dir: Path, qtbot: QtBot
) -> None:
    """同じsourceの再ロードは先頭へ戻し、statusとdurationを再通知しない。"""
    source = test_audio_dir / "sine440.wav"
    backend.load(source)
    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    duration_ms = backend.duration_ms
    backend.seek(1_000)
    qtbot.waitUntil(lambda: backend.position_ms >= 900, timeout=LOAD_TIMEOUT_MS)
    position_spy = QSignalSpy(backend.position_changed)
    duration_spy = QSignalSpy(backend.duration_changed)
    status_spy = QSignalSpy(backend.media_status_changed)
    error_spy = QSignalSpy(backend.error_occurred)

    backend.load(source)

    assert backend.state is PlaybackState.STOPPED
    assert backend.position_ms == 0
    assert backend.duration_ms == duration_ms
    assert position_spy.count() == 1
    assert position_spy.at(0)[0] == 0
    assert duration_spy.count() == 0
    assert status_spy.count() == 0
    assert error_spy.count() == 0


def test_new_source_supersedes_pending_invalid_source(
    backend: QtMultimediaBackend, test_audio_dir: Path, tmp_path: Path, qtbot: QtBot
) -> None:
    """Aのロード完了前にBへ切り替えると、最終status/errorはBだけに対応する。"""
    invalid_source = tmp_path / "壊れたA.wav"
    invalid_source.write_bytes(b"not a wave file")
    source_b = test_audio_dir / "sine440.wav"
    status_spy = QSignalSpy(backend.media_status_changed)
    error_spy = QSignalSpy(backend.error_occurred)

    backend.load(invalid_source)
    backend.load(source_b)

    qtbot.waitUntil(lambda: backend.duration_ms > 0, timeout=LOAD_TIMEOUT_MS)
    statuses = [status_spy.at(index)[0] for index in range(status_spy.count())]
    assert statuses[-1] is MediaStatus.LOADED
    assert Path(player_of(backend).source().toLocalFile()) == source_b
    assert error_spy.count() == 0


# -- Controller との接続 -----------------------------------------------------


def test_controller_can_drive_the_qt_backend(qtbot: QtBot) -> None:
    """Controller から設定を変更でき、Backend の実値が Controller へ届く。"""
    del qtbot
    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)

    controller.set_volume(0.3)
    controller.set_muted(True)
    controller.set_pitch_compensation(False)

    assert backend.volume == pytest.approx(0.3, abs=1e-6)
    assert controller.volume == pytest.approx(0.3, abs=1e-6)
    assert backend.muted is True
    assert controller.muted is True
    assert backend.pitch_compensation is False
    assert controller.pitch_compensation is False


def test_controller_keeps_requested_rate_against_float32_readback(qtbot: QtBot) -> None:
    """45/33 の要求倍率が Qt の float32 読み戻しで揺れない（ADR-0001 の制約 2）。"""
    del qtbot
    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)
    requested = 45 / 33
    spy = QSignalSpy(controller.playback_rate_changed)

    controller.set_playback_rate(requested)

    assert backend.playback_rate != requested
    assert controller.playback_rate == requested
    assert spy.count() == 1
    assert spy.at(0)[0] == pytest.approx(requested)

"""PlaybackController の公開契約とシグナルを検証する。

private フィールドは覗かず、公開プロパティ・シグナル・Backend への呼び出し記録だけで
検証する。
"""

import gc
import math
import weakref
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtTest import QSignalSpy
from pytestqt.qtbot import QtBot

from fakes.fake_playback_backend import FakePlaybackBackend, to_float32
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import (
    MediaStatus,
    PlaybackError,
    PlaybackErrorCode,
    PlaybackState,
)


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    """duration が確定済みの Backend（qtbot により QApplication を用意する）。"""
    del qtbot
    fake = FakePlaybackBackend(duration_ms=5000, volume=1.0, playback_rate=1.0)
    yield fake


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """存在する通常ファイル。内容は Controller の検査に影響しない。"""
    path = tmp_path / "テスト 音源.wav"
    path.write_bytes(b"RIFF----WAVEfmt ")
    return path


# -- 読み込み ---------------------------------------------------------------


def test_load_passes_existing_path_to_backend(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """存在するファイルを load すると、同じ Path が Backend へ渡る。"""
    spy = QSignalSpy(controller.source_changed)

    controller.load(audio_file)

    assert backend.call_args("load") == [(audio_file, 1)]
    assert controller.source == audio_file
    assert spy.count() == 1
    assert spy.at(0)[0] == audio_file


def test_source_is_notified_before_synchronous_backend_notifications(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """同期的な Backend 通知より先に、新しい source が UI へ通知される。"""
    events: list[tuple[str, object]] = []

    def record_source(source: object) -> None:
        events.append(("source", source))

    def record_status(status: MediaStatus) -> None:
        events.append(("status", status))

    def record_state(state: PlaybackState) -> None:
        events.append(("state", state))

    controller.source_changed.connect(record_source)
    controller.media_status_changed.connect(record_status)
    controller.state_changed.connect(record_state)

    controller.load(audio_file)

    assert events == [
        ("source", audio_file),
        ("status", MediaStatus.LOADED),
        ("state", PlaybackState.STOPPED),
    ]


def test_load_accepts_unknown_extension(
    controller: PlaybackController, backend: FakePlaybackBackend, tmp_path: Path
) -> None:
    """拡張子で対応可否を判定しない（実際の可否は Backend のエラーで判定する）。"""
    path = tmp_path / "no_extension"
    path.write_bytes(b"\x00")

    controller.load(path)

    assert backend.call_args("load") == [(path, 1)]


def test_load_resolves_relative_path(
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ControllerのsourceとBackendへ渡すパスを絶対パスへ正規化する。"""
    source = tmp_path / "relative.wav"
    source.write_bytes(b"RIFF----WAVEfmt ")
    monkeypatch.chdir(tmp_path)

    controller.load(Path("relative.wav"))

    assert controller.source == source
    assert controller.source is not None
    assert controller.source.is_absolute()
    assert backend.call_args("load") == [(source, 1)]


def test_load_missing_file_reports_error_without_calling_backend(
    controller: PlaybackController, backend: FakePlaybackBackend, tmp_path: Path
) -> None:
    """存在しないファイルの load は Backend を呼ばずエラー通知になる。"""
    spy = QSignalSpy(controller.error_occurred)

    controller.load(tmp_path / "ない曲.wav")

    assert backend.call_names() == []
    assert controller.source is None
    assert spy.count() == 1
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is PlaybackErrorCode.SOURCE_NOT_FOUND
    assert error.message
    assert "ない曲.wav" in error.detail


def test_load_directory_is_rejected(
    controller: PlaybackController, backend: FakePlaybackBackend, tmp_path: Path
) -> None:
    """ディレクトリは load できない。"""
    spy = QSignalSpy(controller.error_occurred)

    controller.load(tmp_path)

    assert backend.call_names() == []
    assert controller.source is None
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.code is PlaybackErrorCode.SOURCE_NOT_A_FILE


def test_failed_load_keeps_previous_source(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path, tmp_path: Path
) -> None:
    """load に失敗しても現在の source は変わらない。"""
    controller.load(audio_file)

    controller.load(tmp_path / "ない曲.wav")

    assert controller.source == audio_file
    assert backend.call_args("load") == [(audio_file, 1)]


# -- 転送 -------------------------------------------------------------------


def test_transport_operations_are_forwarded_once_each(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """play / pause / stop が Backend へ 1 回ずつ転送される。"""
    controller.play()
    controller.pause()
    controller.stop()

    assert backend.call_names() == ["play", "pause", "stop"]


def test_backend_notifications_are_relayed(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """position / duration / state / media status が UI 向けへ中継される。"""
    position_spy = QSignalSpy(controller.position_changed)
    duration_spy = QSignalSpy(controller.duration_changed)
    state_spy = QSignalSpy(controller.state_changed)
    status_spy = QSignalSpy(controller.media_status_changed)

    backend.emit_position(1234)
    backend.emit_duration(9876)
    backend.emit_state(PlaybackState.PLAYING)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)

    assert position_spy.at(0)[0] == 1234
    assert duration_spy.at(0)[0] == 9876
    assert state_spy.at(0)[0] is PlaybackState.PLAYING
    assert status_spy.at(0)[0] is MediaStatus.END_OF_MEDIA
    assert controller.position_ms == 1234
    assert controller.duration_ms == 9876
    assert controller.state is PlaybackState.PLAYING


def test_backend_error_is_relayed(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """Backend のエラーが Controller のエラーとして通知される。"""
    spy = QSignalSpy(controller.error_occurred)
    error = PlaybackError(
        code=PlaybackErrorCode.FORMAT_ERROR,
        message="この形式は再生できません。",
        detail="QMediaPlayer.FormatError: unsupported codec",
    )

    backend.emit_error(error)

    assert spy.count() == 1
    assert spy.at(0)[0] == error


def test_load_numbers_generations_in_order(
    controller: PlaybackController, backend: FakePlaybackBackend, tmp_path: Path
) -> None:
    """load ごとに読み込み世代を 1 から連番で採番し、Backend へ渡す。"""
    sources: list[Path] = []
    for name in ("a.wav", "b.wav", "c.wav"):
        path = tmp_path / name
        path.write_bytes(b"RIFF----WAVEfmt ")
        sources.append(path)

    for source in sources:
        controller.load(source)

    assert backend.call_args("load") == [
        (sources[0].resolve(), 1),
        (sources[1].resolve(), 2),
        (sources[2].resolve(), 3),
    ]
    assert controller.load_generation == 3


def test_stale_media_status_is_dropped_at_the_public_boundary(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """前sourceの遅延statusは公開境界で捨てる（受け手ごとの除外に頼らない）。"""
    controller.load(audio_file)
    first_generation = controller.load_generation
    controller.load(audio_file)
    spy = QSignalSpy(controller.media_status_changed)

    backend.emit_media_status(MediaStatus.INVALID_MEDIA, generation=first_generation)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA, generation=first_generation)

    assert spy.count() == 0

    backend.emit_media_status(MediaStatus.LOADED)

    assert spy.count() == 1
    assert spy.at(0)[0] is MediaStatus.LOADED
    assert spy.at(0)[1] == controller.load_generation


def test_stale_backend_error_is_dropped_but_source_less_error_is_relayed(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """前sourceのエラーは捨て、sourceに属さないエラーは世代で消さない。"""
    controller.load(audio_file)
    stale_generation = controller.load_generation
    controller.load(audio_file)
    spy = QSignalSpy(controller.error_occurred)

    backend.emit_error(
        PlaybackError(
            code=PlaybackErrorCode.RESOURCE_ERROR,
            message="音声ファイルを読み込めません。",
            detail="前sourceの遅延エラー",
            generation=stale_generation,
            source=audio_file,
        )
    )

    assert spy.count() == 0

    source_less = PlaybackError(
        code=PlaybackErrorCode.UNKNOWN_ERROR,
        message="音声の再生中に不明なエラーが発生しました。",
        detail="変換境界の内部失敗",
    )
    current = PlaybackError(
        code=PlaybackErrorCode.FORMAT_ERROR,
        message="この音声形式は再生できません。",
        detail="現在sourceのエラー",
        generation=controller.load_generation,
        source=audio_file,
    )
    backend.emit_error(source_less)
    backend.emit_error(current)

    assert [spy.at(index)[0] for index in range(spy.count())] == [source_less, current]


def test_load_failure_from_backend_is_relayed(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """読み込めるかどうかは Backend が判定し、失敗はエラーとして届く。"""
    backend.load_error = PlaybackError(
        code=PlaybackErrorCode.RESOURCE_ERROR,
        message="ファイルを読み込めません。",
        detail="QMediaPlayer.ResourceError",
    )
    spy = QSignalSpy(controller.error_occurred)

    controller.load(audio_file)

    assert backend.call_args("load") == [(audio_file, 1)]
    assert spy.count() == 1
    # load に由来するエラーは、その load の世代と source を伴う
    # （世代を持たないと、前 source の遅延エラーと区別できない）。
    error = spy.at(0)[0]
    assert isinstance(error, PlaybackError)
    assert error.generation == 1
    assert error.source == audio_file


def test_no_media_and_stopped_state_contract(
    controller: PlaybackController, backend: FakePlaybackBackend, audio_file: Path
) -> None:
    """NO_MEDIA は source 未設定時だけで、設定後の非再生状態は STOPPED になる。"""
    assert controller.source is None
    assert controller.state is PlaybackState.NO_MEDIA

    controller.load(audio_file)
    assert controller.source == audio_file
    assert controller.state is PlaybackState.STOPPED

    backend.emit_media_status(MediaStatus.LOADING)
    assert controller.state is PlaybackState.STOPPED

    controller.play()
    assert controller.state is PlaybackState.PLAYING
    controller.pause()
    assert controller.state is PlaybackState.PAUSED

    backend.emit_state(PlaybackState.STOPPED)
    backend.emit_media_status(MediaStatus.END_OF_MEDIA)
    assert controller.state is PlaybackState.STOPPED

    backend.emit_media_status(MediaStatus.INVALID_MEDIA)
    assert controller.state is PlaybackState.STOPPED


# -- 値検証 -----------------------------------------------------------------


@pytest.mark.parametrize("volume", [-0.1, 1.1, math.nan])
def test_invalid_volume_is_rejected(
    controller: PlaybackController, backend: FakePlaybackBackend, volume: float
) -> None:
    """範囲外・NaN の音量は clamp せず ValueError で拒否する。"""
    with pytest.raises(ValueError):
        controller.set_volume(volume)

    assert backend.call_names() == []
    assert controller.volume == 1.0


@pytest.mark.parametrize("rate", [0.0, -1.0, math.nan, math.inf])
def test_invalid_playback_rate_is_rejected(
    controller: PlaybackController, backend: FakePlaybackBackend, rate: float
) -> None:
    """0 以下・NaN・無限大の再生速度を拒否する。"""
    with pytest.raises(ValueError):
        controller.set_playback_rate(rate)

    assert backend.call_names() == []
    assert controller.playback_rate == 1.0


@pytest.mark.parametrize("position_ms", [-1, 5001])
def test_invalid_seek_is_rejected(
    controller: PlaybackController, backend: FakePlaybackBackend, position_ms: int
) -> None:
    """負の位置と duration を明確に超える位置を拒否する。"""
    with pytest.raises(ValueError):
        controller.seek(position_ms)

    assert backend.call_names() == []


def test_seek_within_duration_is_forwarded(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """duration 内（終端を含む）の seek は Backend へ転送される。"""
    controller.seek(0)
    controller.seek(2500)
    controller.seek(5000)

    assert backend.call_args("seek") == [(0,), (2500,), (5000,)]


def test_seek_is_forwarded_while_duration_is_unknown(qtbot: QtBot) -> None:
    """duration 未確定（0）のあいだは上限を検証せず転送する。"""
    del qtbot
    backend = FakePlaybackBackend(duration_ms=0)
    controller = PlaybackController(backend)

    controller.seek(30_000)

    assert backend.call_args("seek") == [(30_000,)]


def test_valid_volume_is_forwarded(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """範囲内の音量は Backend へ転送され、通知される。"""
    spy = QSignalSpy(controller.volume_changed)

    controller.set_volume(0.4)

    assert backend.call_args("set_volume") == [(0.4,)]
    assert controller.volume == 0.4
    assert spy.count() == 1
    assert spy.at(0)[0] == pytest.approx(0.4)


# -- 要求値の保持 -----------------------------------------------------------


def test_pitch_compensation_request_is_kept(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """ピッチ補正の要求値を保持し、Backend へ転送する。"""
    spy = QSignalSpy(controller.pitch_compensation_changed)

    controller.set_pitch_compensation(False)

    assert backend.call_args("set_pitch_compensation") == [(False,)]
    assert controller.pitch_compensation is False
    assert spy.count() == 1
    assert spy.at(0)[0] is False


def test_requested_rate_survives_float32_readback(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """45/33 の要求倍率が float32 読み戻しで上書きされない（ADR-0001 の制約 2）。"""
    backend.float32_rate_readback = True
    requested = 45 / 33
    spy = QSignalSpy(controller.playback_rate_changed)

    controller.set_playback_rate(requested)

    assert backend.call_args("set_playback_rate") == [(requested,)]
    # Backend の読み戻しは float32 相当で要求値と厳密には一致しない。
    assert backend.playback_rate == to_float32(requested)
    assert backend.playback_rate != requested
    # Controller は要求値を真値として保持し、読み戻しでの再通知もしない。
    assert controller.playback_rate == requested
    assert spy.count() == 1
    assert spy.at(0)[0] == pytest.approx(requested)


def test_backend_initiated_changes_are_adopted(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """Controller の要求と異なる値を Backend が通知した場合は、その値を採用する。"""
    volume_spy = QSignalSpy(controller.volume_changed)
    muted_spy = QSignalSpy(controller.muted_changed)
    pitch_spy = QSignalSpy(controller.pitch_compensation_changed)

    backend.volume_changed.emit(0.25)
    backend.muted_changed.emit(True)
    backend.pitch_compensation_changed.emit(False)

    assert controller.volume == pytest.approx(0.25)
    assert controller.muted is True
    assert controller.pitch_compensation is False
    assert volume_spy.count() == 1
    assert muted_spy.count() == 1
    assert pitch_spy.count() == 1


def test_rate_changed_outside_tolerance_is_adopted(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """許容誤差を超える実効速度の変化は、Backend 側の実値として採用する。"""
    spy = QSignalSpy(controller.playback_rate_changed)

    backend.playback_rate_changed.emit(2.0)

    assert controller.playback_rate == 2.0
    assert spy.count() == 1


@pytest.mark.parametrize(
    (
        "effective_attribute",
        "setter_name",
        "signal_name",
        "property_name",
        "requested",
        "effective",
    ),
    [
        ("effective_volume", "set_volume", "volume_changed", "volume", 0.4, 0.25),
        ("effective_muted", "set_muted", "muted_changed", "muted", True, False),
        (
            "effective_playback_rate",
            "set_playback_rate",
            "playback_rate_changed",
            "playback_rate",
            1.7,
            1.5,
        ),
        (
            "effective_pitch_compensation",
            "set_pitch_compensation",
            "pitch_compensation_changed",
            "pitch_compensation",
            False,
            True,
        ),
    ],
)
def test_synchronous_backend_correction_keeps_last_signal_consistent(
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    effective_attribute: str,
    setter_name: str,
    signal_name: str,
    property_name: str,
    requested: object,
    effective: object,
) -> None:
    """Backend の同期補正後に要求値を再通知せず、最後の通知と公開値を一致させる。"""
    setattr(backend, effective_attribute, effective)
    spy = QSignalSpy(getattr(controller, signal_name))

    getattr(controller, setter_name)(requested)

    assert getattr(controller, property_name) == effective
    assert spy.count() == 1
    assert spy.at(0)[0] == effective


@pytest.mark.parametrize(
    ("setter_name", "signal_name", "property_name", "requested", "initial"),
    [
        ("set_volume", "volume_changed", "volume", 0.4, 1.0),
        ("set_muted", "muted_changed", "muted", True, False),
        ("set_playback_rate", "playback_rate_changed", "playback_rate", 1.7, 1.0),
        (
            "set_pitch_compensation",
            "pitch_compensation_changed",
            "pitch_compensation",
            False,
            True,
        ),
    ],
)
def test_backend_setter_exception_restores_cached_value(
    controller: PlaybackController,
    backend: FakePlaybackBackend,
    setter_name: str,
    signal_name: str,
    property_name: str,
    requested: object,
    initial: object,
) -> None:
    """Backend の setter が例外を送出した場合、Controller のキャッシュを元へ戻す。"""
    backend.setter_errors[setter_name] = RuntimeError("Backend setter failed")
    spy = QSignalSpy(getattr(controller, signal_name))

    with pytest.raises(RuntimeError, match="Backend setter failed"):
        getattr(controller, setter_name)(requested)

    assert getattr(controller, property_name) == initial
    assert spy.count() == 0


# -- 同じ値の再設定 ---------------------------------------------------------


def test_setting_same_values_is_a_no_op(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """同じ値の再設定は Backend を呼ばず通知もしない（全設定で同じ方針）。"""
    controller.set_volume(0.4)
    controller.set_muted(True)
    controller.set_playback_rate(1.5)
    controller.set_pitch_compensation(False)
    backend.calls.clear()

    volume_spy = QSignalSpy(controller.volume_changed)
    muted_spy = QSignalSpy(controller.muted_changed)
    rate_spy = QSignalSpy(controller.playback_rate_changed)
    pitch_spy = QSignalSpy(controller.pitch_compensation_changed)

    controller.set_volume(0.4)
    controller.set_muted(True)
    controller.set_playback_rate(1.5)
    controller.set_pitch_compensation(False)

    assert backend.call_names() == []
    assert volume_spy.count() == 0
    assert muted_spy.count() == 0
    assert rate_spy.count() == 0
    assert pitch_spy.count() == 0


# -- 寿命 -------------------------------------------------------------------


def test_controller_is_released_after_deletion(qtbot: QtBot) -> None:
    """Controller を手放したあと、Backend 側に参照が残らない。"""
    del qtbot
    backend = FakePlaybackBackend()
    controller = PlaybackController(backend)
    reference = weakref.ref(controller)

    del controller
    gc.collect()

    assert reference() is None
    # 参照が切れた後の通知でクラッシュしないこと。
    backend.emit_position(100)
    backend.emit_state(PlaybackState.PLAYING)

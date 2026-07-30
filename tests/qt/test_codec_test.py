"""配布版のdecode検査（--codec-test）の判定と副作用のなさを検証する。

実際の`QAudioDecoder`で音源をdecodeするが、音声出力deviceは使わないため
通常のQtテストとして実行できる。
"""

import struct
import wave
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from sdp import codec_test as codec_test_module
from sdp.codec_test import (
    CODEC_TEST_FAILURE,
    CODEC_TEST_SUCCESS,
    CodecTestResult,
    decode_file,
    run_codec_test,
)

FORMATS = ("wav", "mp3", "flac", "ogg", "opus", "m4a")


@pytest.fixture
def silent_wav(tmp_path: Path) -> Path:
    path = tmp_path / "無音 テスト.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44_100)
        stream.writeframes(struct.pack("<2h", 0, 0) * 4_410)
    return path


# -- decode判定 --------------------------------------------------------------


def test_decoding_a_wav_reports_pcm_details(qtbot: QtBot, silent_wav: Path) -> None:
    """WAVをdecodeし、buffer数とformatを取得できる。"""
    del qtbot

    result = decode_file(silent_wav)

    assert result.succeeded is True
    assert result.buffer_count >= 1
    assert result.frame_count > 0
    assert result.sample_rate == 44_100
    assert result.channel_count == 2
    assert result.failure_reason is None


@pytest.mark.parametrize("suffix", FORMATS)
def test_every_bundled_format_decodes(qtbot: QtBot, test_audio_dir: Path, suffix: str) -> None:
    """同梱テスト音源の6形式すべてが実PCMへdecodeできる。"""
    del qtbot
    source = test_audio_dir / f"sine440.{suffix}"
    if not source.is_file():
        pytest.fail(f"テスト音源がありません: {source.name}")

    result = decode_file(source)

    assert result.succeeded is True, result.failure_reason
    assert result.buffer_count >= 1
    assert result.frame_count > 0
    assert result.sample_rate > 0
    assert result.channel_count > 0


def test_missing_file_fails_without_raising(qtbot: QtBot, tmp_path: Path) -> None:
    """存在しないファイルは例外にせず失敗として返す。"""
    del qtbot

    result = decode_file(tmp_path / "ない音源.wav")

    assert result.succeeded is False
    assert result.failure_reason == "ファイルがありません"


def test_broken_file_fails(qtbot: QtBot, tmp_path: Path) -> None:
    """音声でないファイルをdecode成功にしない。"""
    del qtbot
    path = tmp_path / "壊れた.wav"
    path.write_bytes(b"not an audio file" * 10)

    result = decode_file(path)

    assert result.succeeded is False
    assert result.failure_reason is not None


def test_timeout_is_reported_as_failure(qtbot: QtBot, silent_wav: Path) -> None:
    """時間内に終わらないdecodeは失敗にする。"""
    del qtbot

    result = decode_file(silent_wav, timeout_ms=1)

    if result.succeeded:
        pytest.skip("この環境では1ms以内にdecodeが完了したためtimeoutを再現できない")
    assert result.failure_reason is not None


@pytest.mark.parametrize(
    ("buffer_count", "frame_count", "sample_rate", "channel_count", "expected"),
    [
        (0, 0, 44_100, 2, "PCM bufferを1件も取得できませんでした"),
        (1, 0, 44_100, 2, "PCM bufferのframe数が0です"),
        (1, 100, 0, 2, "sample rateが不正です"),
        (1, 100, 44_100, 0, "channel countが不正です"),
    ],
)
def test_metadata_only_results_are_not_successes(
    buffer_count: int, frame_count: int, sample_rate: int, channel_count: int, expected: str
) -> None:
    """metadataが読めただけ・空bufferだけの結果を成功にしない。"""
    reason = codec_test_module._failure_reason(  # pyright: ignore[reportPrivateUsage]
        errors=(),
        timed_out=False,
        finished=True,
        buffer_count=buffer_count,
        frame_count=frame_count,
        sample_rate=sample_rate,
        channel_count=channel_count,
    )

    assert reason == expected


def test_result_summary_mentions_the_file_and_reason(tmp_path: Path) -> None:
    """ログ用の要約に成否と理由が入る。"""
    failure = CodecTestResult(tmp_path / "a.wav", succeeded=False, failure_reason="decodeエラー")
    success = CodecTestResult(
        tmp_path / "a.wav",
        succeeded=True,
        buffer_count=3,
        frame_count=1_000,
        sample_rate=44_100,
        channel_count=2,
    )

    assert "NG" in failure.summary()
    assert "decodeエラー" in failure.summary()
    assert "OK" in success.summary()
    assert "44100Hz" in success.summary()


# -- run_codec_test の契約 ---------------------------------------------------


def test_all_targets_are_tried_even_after_a_failure(
    qtbot: QtBot, tmp_path: Path, silent_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一部が失敗しても残りを検査し、終了コードは1になる。"""
    del qtbot
    decoded: list[str] = []
    original = codec_test_module.decode_file

    def record(path: Path, **kwargs: object) -> CodecTestResult:
        decoded.append(path.name)
        return original(path, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(codec_test_module, "decode_file", record)
    missing = tmp_path / "ない音源.mp3"

    exit_code = run_codec_test(["sdp.exe"], [str(missing), str(silent_wav)])

    assert exit_code == CODEC_TEST_FAILURE
    assert decoded == [missing.name, silent_wav.name]


def test_success_returns_zero(qtbot: QtBot, silent_wav: Path) -> None:
    """全件成功なら0を返す。"""
    del qtbot

    assert run_codec_test(["sdp.exe"], [str(silent_wav)]) == CODEC_TEST_SUCCESS


def test_codec_test_does_not_touch_user_data(
    qtbot: QtBot, silent_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings／playlist／ui-stateとwaveform cacheを作らない。"""
    del qtbot
    local_app_data = tmp_path / "local-app-data"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert run_codec_test(["sdp.exe"], [str(silent_wav)]) == CODEC_TEST_SUCCESS

    created = {item.name for item in local_app_data.rglob("*") if item.is_file()}
    assert created.isdisjoint({"settings.json", "playlist.json", "ui-state.json"})
    assert not any(item.suffix == ".npz" for item in local_app_data.rglob("*"))


def test_codec_test_does_not_show_a_window_or_start_ipc() -> None:
    """GUIも単一instance IPCも使わない。"""
    for forbidden in ("MainWindow", "SingleInstanceService", "QMainWindow", "build_player"):
        assert not hasattr(codec_test_module, forbidden), forbidden


def test_codec_test_does_not_leave_temporary_files(
    qtbot: QtBot, silent_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """検査後に一時ファイルを残さない。"""
    del qtbot
    temporary = tmp_path / "temp"
    temporary.mkdir()
    monkeypatch.setenv("TMP", str(temporary))
    monkeypatch.setenv("TEMP", str(temporary))

    run_codec_test(["sdp.exe"], [str(silent_wav)])

    assert list(temporary.iterdir()) == []

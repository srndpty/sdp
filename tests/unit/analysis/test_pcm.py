"""QAudioBuffer → mono float32 の変換境界を検証する。

正規化そのもののマトリクスは
[test_waveform.py](./test_waveform.py) の ``pcm_bytes_to_mono`` 側で検証済み。
ここは QAudioBuffer 固有の妥当性確認（format・byteCount・constData）を扱う。
QAudioBuffer は値型のため QApplication を必要としない。
"""

import struct
from typing import cast

import numpy as np
import pytest
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat

from sdp.core.analysis.pcm import audio_buffer_to_mono


class StubAudioBuffer:
    """QAudioBuffer のうち変換に使うメソッドだけを持つ差し替え。

    Qt は不正な ``QAudioFormat`` を渡された ``QAudioBuffer`` を無効化して
    format ごとリセットしてしまうため、channel count・未対応 format・
    ``constData() is None``・byteCount 不一致といった防御は実 buffer では
    再現できない。いずれも P0-C / P5-A の probe で実在を確認した条件である。
    """

    def __init__(
        self,
        data: object,
        audio_format_value: QAudioFormat,
        byte_count: int | None = None,
    ) -> None:
        self._data = data
        self._format = audio_format_value
        self._byte_count = byte_count

    def format(self) -> QAudioFormat:
        return self._format

    def constData(self) -> object:
        return self._data

    def byteCount(self) -> int:
        if self._byte_count is not None:
            return self._byte_count
        return len(self._data) if isinstance(self._data, bytes) else 0


def stub_buffer(
    data: object,
    audio_format_value: QAudioFormat,
    byte_count: int | None = None,
) -> QAudioBuffer:
    return cast("QAudioBuffer", StubAudioBuffer(data, audio_format_value, byte_count))


def audio_format(
    sample_format: QAudioFormat.SampleFormat,
    channels: int = 2,
    sample_rate: int = 48_000,
) -> QAudioFormat:
    value = QAudioFormat()
    value.setSampleRate(sample_rate)
    value.setChannelCount(channels)
    value.setSampleFormat(sample_format)
    return value


def buffer_of(data: bytes, audio_format_value: QAudioFormat) -> QAudioBuffer:
    return QAudioBuffer(data, audio_format_value)


# -- sample format ----------------------------------------------------------


def test_converts_uint8_stereo() -> None:
    """UInt8はオフセット128を基準に[-1, 1]へ正規化する。"""
    data = struct.pack("<4B", 128, 128, 255, 1)

    samples, sample_rate = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.UInt8))
    )

    assert sample_rate == 48_000
    assert samples.dtype == np.dtype(np.float32)
    assert samples.tolist() == pytest.approx([0.0, 0.0], abs=1e-2)


def test_converts_int16_stereo_by_channel_average() -> None:
    """Int16のstereoはchannel平均でmono化する。"""
    data = struct.pack("<4h", 16384, 0, -32768, -32768)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Int16))
    )

    assert samples.tolist() == pytest.approx([0.25, -1.0])


def test_converts_int32_mono() -> None:
    """Int32のmonoをそのまま正規化する。"""
    data = struct.pack("<2i", 1073741824, -2147483648)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Int32, channels=1))
    )

    assert samples.tolist() == pytest.approx([0.5, -1.0])


def test_converts_float_stereo() -> None:
    """Floatはスケーリングせずchannel平均だけを行う。"""
    data = struct.pack("<4f", 0.5, -0.5, 1.0, 1.0)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Float))
    )

    assert samples.tolist() == pytest.approx([0.0, 1.0])


def test_converts_three_channels() -> None:
    """3ch以上でもchannel平均でmono化する。"""
    data = struct.pack("<3f", 0.3, 0.6, 0.9)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Float, channels=3))
    )

    assert samples.tolist() == pytest.approx([0.6])


def test_silence_stays_silent() -> None:
    """無音はそのまま0になる。"""
    data = struct.pack("<4h", 0, 0, 0, 0)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Int16))
    )

    assert samples.tolist() == [0.0, 0.0]


def test_clipping_input_is_clamped_to_the_unit_range() -> None:
    """範囲外のfloat PCMは-1～1へclampする。"""
    data = struct.pack("<4f", 3.0, 3.0, -4.0, -4.0)

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Float))
    )

    assert samples.tolist() == [1.0, -1.0]


def test_nan_and_inf_are_not_propagated() -> None:
    """壊れたfloat PCMの非有限値を残さない。"""
    data = struct.pack("<4f", float("nan"), float("nan"), float("inf"), float("inf"))

    samples, _ = audio_buffer_to_mono(
        buffer_of(data, audio_format(QAudioFormat.SampleFormat.Float))
    )

    assert np.all(np.isfinite(samples))
    assert samples.tolist() == [0.0, 1.0]


# -- 無効 buffer ------------------------------------------------------------


def test_default_constructed_buffer_is_rejected() -> None:
    """再生終端で届く空bufferは明示的に失敗させる（P5-A probeで実測）。

    format未設定・sampleRate 0・constData None がすべて成立する。
    """
    empty = QAudioBuffer()
    assert empty.format().sampleRate() == 0
    assert empty.constData() is None

    with pytest.raises(ValueError, match="sample rate"):
        audio_buffer_to_mono(empty)


def test_zero_sample_rate_is_rejected() -> None:
    """sample rateが不正なbufferは受け付けない。"""
    invalid = audio_format(QAudioFormat.SampleFormat.Int16, sample_rate=0)

    with pytest.raises(ValueError, match="sample rate"):
        audio_buffer_to_mono(buffer_of(struct.pack("<2h", 0, 0), invalid))


def test_zero_channel_count_is_rejected() -> None:
    """channel countが不正なbufferは受け付けない。"""
    invalid = audio_format(QAudioFormat.SampleFormat.Int16, channels=0)

    with pytest.raises(ValueError, match="channel count"):
        audio_buffer_to_mono(stub_buffer(struct.pack("<2h", 0, 0), invalid))


def test_unknown_sample_format_is_rejected() -> None:
    """未対応formatは既定値へ丸めず明示的に失敗させる（silent fallbackを作らない）。"""
    unknown = audio_format(QAudioFormat.SampleFormat.Unknown)

    with pytest.raises(ValueError, match="未対応"):
        audio_buffer_to_mono(stub_buffer(struct.pack("<2h", 0, 0), unknown))


def test_none_const_data_is_rejected() -> None:
    """``constData()`` が None を返す実測ケース（P0-C）を明示的に失敗させる。"""
    with pytest.raises(ValueError, match="None"):
        audio_buffer_to_mono(stub_buffer(None, audio_format(QAudioFormat.SampleFormat.Int16)))


def test_non_bytes_const_data_is_rejected() -> None:
    """bytes化できないPCMは失敗させる。"""
    with pytest.raises(ValueError, match="bytes"):
        audio_buffer_to_mono(stub_buffer(object(), audio_format(QAudioFormat.SampleFormat.Int16)))


def test_byte_count_mismatch_is_rejected() -> None:
    """byteCountとPCM長が食い違うbufferは信用しない。"""
    data = struct.pack("<4h", 0, 0, 0, 0)

    with pytest.raises(ValueError, match="byteCount"):
        audio_buffer_to_mono(
            stub_buffer(data, audio_format(QAudioFormat.SampleFormat.Int16), byte_count=99)
        )


def test_partial_frame_is_rejected() -> None:
    """frame境界で終わらないPCMは安全に失敗させる。"""
    # Int16 stereo は1frame 4bytes。6bytesは1.5frame。
    with pytest.raises(ValueError, match="frame境界"):
        audio_buffer_to_mono(
            stub_buffer(struct.pack("<3h", 1, 2, 3), audio_format(QAudioFormat.SampleFormat.Int16))
        )


def test_empty_pcm_of_a_valid_format_returns_an_empty_array() -> None:
    """format妥当でPCMが空なら空配列を返す（失敗にしない）。"""
    samples, sample_rate = audio_buffer_to_mono(
        stub_buffer(b"", audio_format(QAudioFormat.SampleFormat.Int16))
    )

    assert samples.shape == (0,)
    assert sample_rate == 48_000


# -- buffer を保持しない ----------------------------------------------------


def test_result_does_not_share_memory_with_the_buffer() -> None:
    """PCMは新しいfloat32配列へコピーされ、QAudioBufferのviewを保持しない。"""
    data = struct.pack("<4h", 16384, 16384, -16384, -16384)
    buffer = buffer_of(data, audio_format(QAudioFormat.SampleFormat.Int16))

    samples, _ = audio_buffer_to_mono(buffer)
    del buffer

    # buffer破棄後も値が読める（viewではなくコピーである証拠）。
    assert samples.tolist() == pytest.approx([0.5, -0.5])
    assert samples.base is None or samples.flags.owndata


def test_conversion_does_not_return_qt_types() -> None:
    """返り値はNumPy配列とintだけで、Qtの型を漏らさない。"""
    samples, sample_rate = audio_buffer_to_mono(
        buffer_of(struct.pack("<2h", 0, 0), audio_format(QAudioFormat.SampleFormat.Int16))
    )

    assert isinstance(samples, np.ndarray)
    assert isinstance(sample_rate, int)

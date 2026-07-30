"""QAudioBuffer から mono／L／R の float32 PCM を取り出すQt境界。

正規化そのものは Qt 非依存の :func:`sdp.core.analysis.waveform.pcm_bytes_to_channels`
が担う。ここは「QAudioBuffer の妥当性確認」と「bytes 化」だけを引き受け、
波形解析（オフラインの QAudioDecoder）と PCM タップ（再生中の
QAudioBufferOutput）の双方から再利用する（重複実装を避ける）。

**QAudioBuffer の bytes 化は 1 回だけ行う。** mono とL／Rは同じ bytes から派生させ、
別経路で再び bytes 化しない（P5-B）。

QAudioBuffer とその内部 memory view をこのモジュールの外へ持ち出さない。
必要な PCM だけを新しい float32 配列へコピーして返す。
"""

from dataclasses import dataclass
from typing import cast

import numpy as np
from numpy.typing import NDArray
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat

from sdp.core.analysis.waveform import PcmSampleFormat, pcm_bytes_to_channels

_SAMPLE_FORMAT_MAP: dict[QAudioFormat.SampleFormat, PcmSampleFormat] = {
    QAudioFormat.SampleFormat.UInt8: PcmSampleFormat.UINT8,
    QAudioFormat.SampleFormat.Int16: PcmSampleFormat.INT16,
    QAudioFormat.SampleFormat.Int32: PcmSampleFormat.INT32,
    QAudioFormat.SampleFormat.Float: PcmSampleFormat.FLOAT,
}
"""Qt の sample format からアプリ内 format への写像。

``Unknown`` は写像へ含めない。再生終端で format 未設定の空 buffer が届くことを
P5-A の probe で実測しており、既定値へ丸めず明示的に失敗させる。
"""


@dataclass(frozen=True, slots=True)
class PcmChunk:
    """1 QAudioBuffer 分の mono／左／右 PCM と format 情報。

    契約:

    - 3 配列はいずれも 1 次元 float32、同じ長さ、read-only、有限、-1〜1。
    - 呼び出し元の QAudioBuffer とメモリを共有しない（buffer 破棄後も読める）。
    - ``channel_count`` は 1 以上。mono 入力では ``left`` と ``right`` が
      ``mono`` と同値になる。2ch 以上では ``left`` が channel 0、``right`` が
      channel 1（3ch 以上の残りは ``mono`` の平均にだけ含まれる）。
    - QAudioBuffer、QAudioFormat、memoryview、時刻オブジェクトは保持しない。
    """

    mono: NDArray[np.float32]
    left: NDArray[np.float32]
    right: NDArray[np.float32]
    sample_rate: int
    channel_count: int

    def __post_init__(self) -> None:
        arrays = tuple(
            _validated_array(name, getattr(self, name)) for name in ("mono", "left", "right")
        )
        if len({array.shape for array in arrays}) != 1:
            raise ValueError("mono／left／rightのshapeが一致しません")
        if type(self.sample_rate) is not int or self.sample_rate < 1:
            raise ValueError("sample_rateは1以上の整数である必要があります")
        if type(self.channel_count) is not int or self.channel_count < 1:
            raise ValueError("channel_countは1以上の整数である必要があります")

        # 呼び出し側の配列とメモリを共有せず、frozen dataclassの要素も不変にする
        # （WaveformData・SpectrumFrameと同じ契約）。
        for name, array in zip(("mono", "left", "right"), arrays, strict=True):
            copied = array.copy()
            copied.setflags(write=False)
            object.__setattr__(self, name, copied)

    @property
    def frame_count(self) -> int:
        return int(self.mono.size)


def audio_buffer_to_pcm_chunk(buffer: QAudioBuffer) -> PcmChunk:
    """QAudioBuffer を :class:`PcmChunk` へ 1 回の bytes 化で変換する。

    無効な buffer・未対応 format・frame 境界不正はすべて :class:`ValueError`
    とする。呼び出し側（音声コールバック）はこれを捕捉して buffer を捨てる。
    """
    audio_format = buffer.format()
    channels = audio_format.channelCount()
    sample_rate = audio_format.sampleRate()
    if sample_rate < 1:
        raise ValueError("QAudioBufferのsample rateが不正です")
    if channels < 1:
        raise ValueError("QAudioBufferのchannel countが不正です")
    sample_format = _map_sample_format(audio_format.sampleFormat())
    raw_data: object = buffer.constData()
    # PySide stubはNoneを含めないが、P0-C実測では無効bufferでNoneになり得た。
    if raw_data is None:  # pyright: ignore[reportUnnecessaryComparison]
        raise ValueError("QAudioBuffer.constData()がNoneを返しました")
    try:
        raw = bytes(raw_data)
    except (TypeError, ValueError) as error:
        raise ValueError("QAudioBufferのPCMをbytesへ変換できません") from error
    if len(raw) != buffer.byteCount():
        raise ValueError("QAudioBufferのbyteCountとPCM長が一致しません")
    mono, left, right = pcm_bytes_to_channels(raw, sample_format, channels)
    return PcmChunk(
        mono=mono,
        left=left,
        right=right,
        sample_rate=sample_rate,
        channel_count=channels,
    )


def audio_buffer_to_mono(buffer: QAudioBuffer) -> tuple[NDArray[np.float32], int]:
    """QAudioBuffer を ``(mono float32, sample rate)`` へ変換する。

    mono だけを使う波形解析側のための互換 wrapper。
    """
    chunk = audio_buffer_to_pcm_chunk(buffer)
    return chunk.mono, chunk.sample_rate


def _validated_array(name: str, value: object) -> NDArray[np.float32]:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はNumPy配列である必要があります")
    # ndarrayのdtypeは直後に検証するため、以降の型だけをfloat32へ絞る。
    array = cast("NDArray[np.float32]", value)
    if array.dtype != np.dtype(np.float32):
        raise TypeError(f"{name}のdtypeはfloat32である必要があります")
    if array.ndim != 1:
        raise ValueError(f"{name}は1次元である必要があります")
    if not bool(np.all(np.isfinite(array))):
        raise ValueError(f"{name}にNaNまたはinfが含まれています")
    if bool(np.any(array < -1.0)) or bool(np.any(array > 1.0)):
        raise ValueError(f"{name}が-1.0～1.0の範囲外です")
    return array


def _map_sample_format(value: QAudioFormat.SampleFormat) -> PcmSampleFormat:
    try:
        return _SAMPLE_FORMAT_MAP[value]
    except KeyError as error:
        raise ValueError(f"未対応のQAudioFormat.SampleFormatです: {value.name}") from error

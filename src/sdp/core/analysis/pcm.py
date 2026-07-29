"""QAudioBuffer から mono float32 PCM を取り出すQt境界。

正規化そのものは Qt 非依存の :func:`sdp.core.analysis.waveform.pcm_bytes_to_mono`
が担う。ここは「QAudioBuffer の妥当性確認」と「bytes 化」だけを引き受け、
波形解析（オフラインの QAudioDecoder）と PCM タップ（再生中の
QAudioBufferOutput）の双方から再利用する（重複実装を避ける）。

QAudioBuffer とその内部 memory view をこのモジュールの外へ持ち出さない。
必要な PCM だけを新しい float32 配列へコピーして返す。
"""

import numpy as np
from numpy.typing import NDArray
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat

from sdp.core.analysis.waveform import PcmSampleFormat, pcm_bytes_to_mono

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


def audio_buffer_to_mono(buffer: QAudioBuffer) -> tuple[NDArray[np.float32], int]:
    """QAudioBuffer を ``(mono float32, sample rate)`` へ変換する。

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
    return pcm_bytes_to_mono(raw, sample_format, channels), sample_rate


def _map_sample_format(value: QAudioFormat.SampleFormat) -> PcmSampleFormat:
    try:
        return _SAMPLE_FORMAT_MAP[value]
    except KeyError as error:
        raise ValueError(f"未対応のQAudioFormat.SampleFormatです: {value.name}") from error

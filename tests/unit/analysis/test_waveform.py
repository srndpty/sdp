"""PCM正規化と増分波形縮約を検証する。"""

import math

import numpy as np
import pytest

from sdp.core.analysis.waveform import (
    PcmSampleFormat,
    UnsupportedSampleFormatError,
    WaveformData,
    WaveformReducer,
    pcm_bytes_to_mono,
)


def waveform(
    minimum: np.ndarray,
    maximum: np.ndarray,
    *,
    bucket: float = 20.0,
    duration: int = 20,
    complete: bool = True,
) -> WaveformData:
    return WaveformData(
        minimum=minimum.astype(np.float32),
        maximum=maximum.astype(np.float32),
        bucket_duration_ms=bucket,
        duration_ms=duration,
        complete=complete,
    )


def test_waveform_data_copies_and_makes_arrays_read_only() -> None:
    """公開配列は入力と共有せず、要素を書き換えられない。"""
    source = np.array([-0.5], dtype=np.float32)
    data = waveform(source, -source)
    source[0] = -1.0
    assert data.minimum[0] == pytest.approx(-0.5)
    assert not data.minimum.flags.writeable
    assert not data.maximum.flags.writeable
    with pytest.raises(ValueError):
        data.minimum[0] = 0.0


@pytest.mark.parametrize(
    ("minimum", "maximum", "error"),
    [
        (np.zeros(1, dtype=np.float32), np.zeros(2, dtype=np.float32), ValueError),
        (np.zeros(1, dtype=np.float64), np.zeros(1, dtype=np.float32), TypeError),
        (np.array([math.nan], dtype=np.float32), np.zeros(1, dtype=np.float32), ValueError),
        (np.array([-math.inf], dtype=np.float32), np.zeros(1, dtype=np.float32), ValueError),
        (np.array([0.5], dtype=np.float32), np.array([0.4], dtype=np.float32), ValueError),
        (np.array([-1.1], dtype=np.float32), np.zeros(1, dtype=np.float32), ValueError),
    ],
)
def test_waveform_data_rejects_invalid_arrays(
    minimum: np.ndarray, maximum: np.ndarray, error: type[Exception]
) -> None:
    """shape、dtype、有限性、順序、範囲を厳密に検証する。"""
    with pytest.raises(error):
        WaveformData(minimum, maximum, 20.0, 20, True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("bucket", "duration", "complete", "error"),
    [
        (0.0, 0, True, ValueError),
        (math.inf, 0, True, ValueError),
        (20.0, -1, True, ValueError),
        (20.0, 0, 1, TypeError),
    ],
)
def test_waveform_data_rejects_invalid_metadata(
    bucket: float, duration: int, complete: object, error: type[Exception]
) -> None:
    """bucket、duration、completeの契約違反を拒否する。"""
    with pytest.raises(error):
        WaveformData(
            np.empty(0, dtype=np.float32),
            np.empty(0, dtype=np.float32),
            bucket,
            duration,
            complete,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("sample_format", "values", "dtype", "expected"),
    [
        (PcmSampleFormat.UINT8, [0, 128, 255], np.uint8, [-1.0, 0.0, 127 / 128]),
        (PcmSampleFormat.INT16, [-32768, 0, 32767], np.int16, [-1.0, 0.0, 32767 / 32768]),
        (
            PcmSampleFormat.INT32,
            [-2147483648, 0, 2147483647],
            np.int32,
            [-1.0, 0.0, 2147483647 / 2147483648],
        ),
        (PcmSampleFormat.FLOAT, [-1.0, 0.0, 1.0], np.float32, [-1.0, 0.0, 1.0]),
    ],
)
def test_pcm_sample_formats_are_normalized(
    sample_format: PcmSampleFormat,
    values: list[float],
    dtype: type[np.generic],
    expected: list[float],
) -> None:
    """UInt8／Int16／Int32／Floatをmono float32へ正規化する。"""
    data = np.asarray(values, dtype=dtype).tobytes()
    actual = pcm_bytes_to_mono(data, sample_format, 1)
    np.testing.assert_allclose(actual, expected, atol=1e-7)
    assert actual.dtype == np.float32


def test_stereo_is_averaged_to_mono_and_clipped() -> None:
    """channelごとのframeを平均し、非有限値と範囲外値を残さない。"""
    frames = np.array([[1.0, -1.0], [2.0, 2.0], [math.nan, math.inf]], dtype=np.float32)
    actual = pcm_bytes_to_mono(frames.tobytes(), PcmSampleFormat.FLOAT, 2)
    np.testing.assert_array_equal(actual, np.array([0.0, 1.0, 0.0], dtype=np.float32))


def test_pcm_rejects_partial_frame_and_unsupported_format() -> None:
    """frame途中のbytesと未知sample formatを明示的に拒否する。"""
    with pytest.raises(ValueError, match="frame"):
        pcm_bytes_to_mono(b"\0", PcmSampleFormat.INT16, 2)
    with pytest.raises(UnsupportedSampleFormatError):
        pcm_bytes_to_mono(b"", object(), 1)  # type: ignore[arg-type]


def test_reducer_handles_silence_sine_clipping_and_remainder() -> None:
    """完成bucketと最後の端数bucketをmin/maxへ縮約する。"""
    reducer = WaveformReducer(sample_rate=1000, bucket_duration_ms=20)
    first = np.zeros(20, dtype=np.float32)
    phase = np.linspace(0, 2 * np.pi, 20, endpoint=False, dtype=np.float32)
    sine = np.sin(phase).astype(np.float32)
    remainder = np.array([-2.0, 0.5, 2.0], dtype=np.float32)
    reducer.append(np.concatenate((first, sine, remainder)))

    partial = reducer.snapshot(complete=False)
    complete = reducer.snapshot(complete=True)
    np.testing.assert_allclose(partial.minimum, [0.0, -1.0], atol=1e-6)
    np.testing.assert_allclose(partial.maximum, [0.0, 1.0], atol=1e-6)
    np.testing.assert_array_equal(complete.minimum, [0.0, -1.0, -1.0])
    np.testing.assert_array_equal(complete.maximum, [0.0, 1.0, 1.0])
    assert complete.duration_ms == 43


def test_chunk_boundaries_and_single_sample_appends_match_one_chunk() -> None:
    """bucketを跨ぐchunkと1sampleずつの追加でも同じ結果になる。"""
    samples = np.linspace(-1.0, 1.0, 53, dtype=np.float32)
    once = WaveformReducer(1000)
    once.append(samples)
    split = WaveformReducer(1000)
    for sample in samples:
        split.append(np.array([sample], dtype=np.float32))

    once_data = once.snapshot(complete=True)
    split_data = split.snapshot(complete=True)
    np.testing.assert_array_equal(split_data.minimum, once_data.minimum)
    np.testing.assert_array_equal(split_data.maximum, once_data.maximum)
    assert split.pending_frame_count < split.frames_per_bucket


def test_empty_reducer_returns_valid_empty_data() -> None:
    """空入力でも完了済みの有効な空波形を返せる。"""
    data = WaveformReducer(44_100).snapshot(complete=True)
    assert data.minimum.size == data.maximum.size == 0
    assert data.duration_ms == 0
    assert data.complete


def test_old_snapshot_does_not_change_after_append() -> None:
    """snapshot後の内部更新は過去の公開配列へ影響しない。"""
    reducer = WaveformReducer(1000)
    reducer.append(np.zeros(20, dtype=np.float32))
    old = reducer.snapshot(complete=False)
    reducer.append(np.ones(20, dtype=np.float32))
    np.testing.assert_array_equal(old.minimum, [0.0])
    np.testing.assert_array_equal(old.maximum, [0.0])


def test_sixty_minute_stream_has_expected_bucket_count_without_full_pcm_history() -> None:
    """60分相当でも保持するPCMは1bucket未満で、約18万bucketになる。"""
    reducer = WaveformReducer(sample_rate=1000)
    minute = np.zeros(60_000, dtype=np.float32)
    for _ in range(60):
        reducer.append(minute)
    data = reducer.snapshot(complete=True)
    assert data.minimum.size == 180_000
    assert data.minimum.nbytes + data.maximum.nbytes == 1_440_000
    assert reducer.pending_frame_count == 0

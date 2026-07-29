"""PcmRingBufferの容量固定・wrap・snapshot契約・thread安全性を検証する。"""

import inspect
import threading

import numpy as np
import pytest
from numpy.typing import NDArray

from sdp.core.analysis import ring_buffer as ring_buffer_module
from sdp.core.analysis.ring_buffer import (
    DEFAULT_PCM_SAMPLE_RATE,
    PCM_RING_BUFFER_SECONDS,
    PcmRingBuffer,
    pcm_ring_capacity,
)


def ramp(start: int, count: int) -> NDArray[np.float32]:
    """1サンプルずつ識別できる連番データ（-1～1へ収める）。"""
    return (np.arange(start, start + count, dtype=np.float32) % 1000) / 1000.0


# -- 容量 -------------------------------------------------------------------


@pytest.mark.parametrize("capacity", [0, -1, -100])
def test_rejects_non_positive_capacity(capacity: int) -> None:
    """容量は1以上の整数だけを受け付ける。"""
    with pytest.raises(ValueError, match="capacity"):
        PcmRingBuffer(capacity)


@pytest.mark.parametrize("capacity", [1.0, True, "8"])
def test_rejects_non_integer_capacity(capacity: object) -> None:
    """暗黙のキャストで呼び出し側のバグを隠さない。"""
    with pytest.raises(ValueError, match="capacity"):
        PcmRingBuffer(capacity)  # pyright: ignore[reportArgumentType]


def test_capacity_helper_uses_two_seconds() -> None:
    """標準契約は48kHzで2秒＝96,000サンプル。"""
    assert PCM_RING_BUFFER_SECONDS == 2.0
    assert pcm_ring_capacity(DEFAULT_PCM_SAMPLE_RATE) == 96_000
    assert pcm_ring_capacity(44_100) == 88_200


def test_capacity_helper_respects_the_minimum() -> None:
    """低sample rateでもFFT長を下回らない。"""
    assert pcm_ring_capacity(1_000, 4_096) == 4_096
    assert pcm_ring_capacity(48_000, 4_096) == 96_000


@pytest.mark.parametrize(("sample_rate", "minimum"), [(0, 0), (-1, 0), (48_000, -1)])
def test_capacity_helper_validates_arguments(sample_rate: int, minimum: int) -> None:
    """不正なsample rate・下限は例外にする。"""
    with pytest.raises(ValueError):
        pcm_ring_capacity(sample_rate, minimum)


# -- snapshot ---------------------------------------------------------------


def test_empty_snapshot_is_zero_padded() -> None:
    """1サンプルも無い状態でも要求長のゼロ配列を返す（無効frameにしない）。"""
    buffer = PcmRingBuffer(16)

    snapshot = buffer.snapshot(8)

    assert snapshot.shape == (8,)
    assert snapshot.dtype == np.dtype(np.float32)
    assert np.all(snapshot == 0.0)
    assert buffer.available == 0


def test_snapshot_after_append_returns_latest_samples() -> None:
    """append後は最新サンプルが右詰めで並ぶ。"""
    buffer = PcmRingBuffer(16)
    buffer.append(ramp(0, 4))

    snapshot = buffer.snapshot(4)

    assert np.array_equal(snapshot, ramp(0, 4))


def test_snapshot_left_pads_when_data_is_insufficient() -> None:
    """保持数が要求長より少ない場合は左側を0で埋める。"""
    buffer = PcmRingBuffer(16)
    buffer.append(ramp(0, 3))

    snapshot = buffer.snapshot(6)

    assert np.array_equal(snapshot[:3], np.zeros(3, dtype=np.float32))
    assert np.array_equal(snapshot[3:], ramp(0, 3))


def test_snapshot_of_zero_returns_empty_array() -> None:
    """N=0は空配列（例外にしない）。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))

    assert buffer.snapshot(0).shape == (0,)


@pytest.mark.parametrize("frame_count", [-1, 1.0, True])
def test_snapshot_rejects_invalid_frame_count(frame_count: object) -> None:
    """負値・非整数のsnapshot長は例外にする。"""
    buffer = PcmRingBuffer(8)

    with pytest.raises(ValueError, match="frame_count"):
        buffer.snapshot(frame_count)  # pyright: ignore[reportArgumentType]


def test_snapshot_longer_than_capacity_is_left_padded() -> None:
    """要求長が容量を超えても、保持分を右詰めした要求長の配列を返す。"""
    buffer = PcmRingBuffer(4)
    buffer.append(ramp(0, 4))

    snapshot = buffer.snapshot(10)

    assert snapshot.shape == (10,)
    assert np.all(snapshot[:6] == 0.0)
    assert np.array_equal(snapshot[6:], ramp(0, 4))


def test_snapshot_is_read_only_and_not_shared_with_the_buffer() -> None:
    """snapshotは独立したread-only配列で、内部bufferとメモリを共有しない。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))

    snapshot = buffer.snapshot(8)

    assert not snapshot.flags.writeable
    with pytest.raises(ValueError):
        snapshot[0] = 1.0
    assert snapshot.base is None or not np.shares_memory(snapshot, buffer.snapshot(8))


def test_previous_snapshot_is_unchanged_by_later_appends() -> None:
    """append後も先に取得したsnapshotは変化しない。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))
    first = buffer.snapshot(8).copy()

    buffer.append(ramp(100, 8))

    assert np.array_equal(buffer.snapshot(8).copy(), ramp(100, 8))
    assert np.array_equal(first, ramp(0, 8))


# -- append と wrap ---------------------------------------------------------


def test_append_exactly_to_capacity() -> None:
    """容量ちょうどのappendはすべて保持される。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))

    assert buffer.available == 8
    assert np.array_equal(buffer.snapshot(8), ramp(0, 8))


def test_append_below_capacity_keeps_available_count() -> None:
    """容量未満では保持数がappend済み数と一致する。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 3))
    buffer.append(ramp(3, 2))

    assert buffer.available == 5
    assert np.array_equal(buffer.snapshot(5), ramp(0, 5))


def test_snapshot_before_wrap_keeps_insertion_order() -> None:
    """wrap前は挿入順のまま読み出せる。"""
    buffer = PcmRingBuffer(8)
    for index in range(0, 6, 2):
        buffer.append(ramp(index, 2))

    assert np.array_equal(buffer.snapshot(6), ramp(0, 6))


def test_snapshot_after_wrap_returns_latest_samples_in_order() -> None:
    """wrap後も最新サンプルが順序どおり読み出せる。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 6))
    buffer.append(ramp(6, 5))

    assert buffer.available == 8
    assert np.array_equal(buffer.snapshot(8), ramp(3, 8))


def test_multiple_wraps_keep_only_the_newest_window() -> None:
    """複数回wrapしても容量分の最新窓だけが残る。"""
    buffer = PcmRingBuffer(8)
    for index in range(0, 40, 5):
        buffer.append(ramp(index, 5))

    assert buffer.available == 8
    assert np.array_equal(buffer.snapshot(8), ramp(32, 8))


def test_single_append_larger_than_capacity_keeps_the_tail() -> None:
    """1回のappendが容量を超える場合は末尾capacity分だけを保持する。"""
    buffer = PcmRingBuffer(4)
    buffer.append(ramp(0, 10))

    assert buffer.available == 4
    assert np.array_equal(buffer.snapshot(4), ramp(6, 4))


def test_many_appends_keep_the_capacity_fixed() -> None:
    """大量appendでも容量とメモリ使用量は増えない。"""
    buffer = PcmRingBuffer(1_024)
    for index in range(500):
        buffer.append(ramp(index * 512, 512))

    assert buffer.capacity == 1_024
    assert buffer.available == 1_024
    assert buffer.snapshot(1_024).nbytes == 4_096


def test_empty_append_is_a_no_op() -> None:
    """空chunkは何も変えない。"""
    buffer = PcmRingBuffer(8)
    buffer.append(np.empty(0, dtype=np.float32))

    assert buffer.available == 0


# -- 入力検証 ---------------------------------------------------------------


def test_append_rejects_non_float32() -> None:
    """dtypeはfloat32に限る（暗黙のキャストをしない）。"""
    buffer = PcmRingBuffer(8)

    with pytest.raises(TypeError, match="float32"):
        buffer.append(np.zeros(4, dtype=np.float64))  # pyright: ignore[reportArgumentType]


def test_append_rejects_non_array() -> None:
    """NumPy配列以外は受け付けない。"""
    buffer = PcmRingBuffer(8)

    with pytest.raises(TypeError, match="NumPy"):
        buffer.append([0.0, 1.0])  # pyright: ignore[reportArgumentType]


def test_append_rejects_multi_dimensional_input() -> None:
    """mono 1次元だけを受け付ける（stereoのmono化は呼び出し側の責務）。"""
    buffer = PcmRingBuffer(8)

    with pytest.raises(ValueError, match="1次元"):
        buffer.append(np.zeros((2, 4), dtype=np.float32))


def test_append_does_not_keep_nan_or_inf() -> None:
    """壊れたfloat PCMの非有限値をバッファへ残さない。"""
    buffer = PcmRingBuffer(4)
    buffer.append(np.array([np.nan, np.inf, -np.inf, 0.5], dtype=np.float32))

    snapshot = buffer.snapshot(4)

    assert np.all(np.isfinite(snapshot))
    assert snapshot.tolist() == [0.0, 1.0, -1.0, 0.5]


def test_append_does_not_modify_the_input_array() -> None:
    """呼び出し側の配列を書き換えない。"""
    buffer = PcmRingBuffer(8)
    samples = np.array([np.nan, 0.25], dtype=np.float32)

    buffer.append(samples)

    assert bool(np.isnan(samples[0]))


# -- clear と再構成 ---------------------------------------------------------


def test_clear_discards_held_samples_but_keeps_capacity() -> None:
    """source変更時のclearで保持データだけを捨てる。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))

    buffer.clear()

    assert buffer.capacity == 8
    assert buffer.available == 0
    assert np.all(buffer.snapshot(8) == 0.0)


def test_set_capacity_rebuilds_and_clears() -> None:
    """sample rate変更時は容量を作り直し、旧formatのサンプルを混ぜない。"""
    buffer = PcmRingBuffer(8)
    buffer.append(ramp(0, 8))

    buffer.set_capacity(16)

    assert buffer.capacity == 16
    assert buffer.available == 0
    assert np.all(buffer.snapshot(16) == 0.0)

    buffer.append(ramp(100, 4))
    assert np.array_equal(buffer.snapshot(4), ramp(100, 4))


def test_set_capacity_validates_the_argument() -> None:
    """不正な容量では作り直さない。"""
    buffer = PcmRingBuffer(8)

    with pytest.raises(ValueError, match="capacity"):
        buffer.set_capacity(0)
    assert buffer.capacity == 8


# -- thread 競合 ------------------------------------------------------------


def test_concurrent_append_and_snapshot_stay_consistent() -> None:
    """writerとreaderが別threadでも、途中状態や例外が漏れない。

    固定sleepでタイミングを作らず、writerの完了イベントで終端する。
    """
    buffer = PcmRingBuffer(1_024)
    chunk = np.full(256, 0.5, dtype=np.float32)
    # 固定sleepではなくbarrierで両threadの開始を揃える。
    start = threading.Barrier(2, timeout=30.0)
    errors: list[BaseException] = []
    snapshots: list[int] = []

    def write() -> None:
        try:
            start.wait()
            for _ in range(2_000):
                buffer.append(chunk)
        except BaseException as error:
            errors.append(error)

    def read() -> None:
        try:
            start.wait()
            for _ in range(2_000):
                snapshot = buffer.snapshot(512)
                assert snapshot.shape == (512,)
                # 書き込まれる値は0.5か未書き込みの0だけ。中途半端な値は現れない。
                assert set(np.unique(snapshot).tolist()) <= {0.0, 0.5}
                snapshots.append(1)
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=write, name="pcm-writer")
    reader = threading.Thread(target=read, name="pcm-reader")
    writer.start()
    reader.start()
    writer.join(timeout=60.0)
    reader.join(timeout=60.0)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert len(snapshots) == 2_000
    assert buffer.capacity == 1_024
    assert buffer.available == 1_024


def test_concurrent_clear_does_not_corrupt_appends() -> None:
    """clearとappendが競合しても容量と有限性を保つ。"""
    buffer = PcmRingBuffer(512)
    chunk = np.full(128, 0.25, dtype=np.float32)
    start = threading.Barrier(2, timeout=30.0)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            start.wait()
            for _ in range(1_000):
                buffer.append(chunk)
        except BaseException as error:
            errors.append(error)

    def clear() -> None:
        try:
            start.wait()
            for _ in range(1_000):
                buffer.clear()
                assert buffer.available <= 512
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=write, name="pcm-writer")
    clearer = threading.Thread(target=clear, name="pcm-clearer")
    writer.start()
    clearer.start()
    writer.join(timeout=60.0)
    clearer.join(timeout=60.0)

    assert not writer.is_alive()
    assert not clearer.is_alive()
    assert errors == []
    assert buffer.capacity == 512
    assert np.all(np.isfinite(buffer.snapshot(512)))


# -- 実装構造 ---------------------------------------------------------------


def test_implementation_avoids_python_sample_loops_and_growth() -> None:
    """sample単位のPythonループやappendごとの配列成長を使わない。"""
    source = inspect.getsource(ring_buffer_module)

    assert "np.concatenate" not in source
    assert "np.append" not in source
    assert "for " not in source

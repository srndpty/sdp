"""波形キャッシュのkey、npz検証、atomic保存、LRUを検証する。"""

import os
from pathlib import Path

import numpy as np
import pytest

from sdp.core.analysis.waveform import WaveformData
from sdp.core.analysis.waveform_cache import (
    WaveformCache,
    WaveformCacheKey,
)


def make_source(tmp_path: Path, name: str = "日本語 音源.wav") -> Path:
    path = tmp_path / name
    path.write_bytes(b"audio")
    return path


def make_key(path: Path, **changes: int) -> WaveformCacheKey:
    base = WaveformCacheKey.from_path(path)
    values = {
        "path": base.path,
        "size": base.size,
        "mtime_ns": base.mtime_ns,
        "analysis_version": base.analysis_version,
        "bucket_ms": base.bucket_ms,
        "format_version": base.format_version,
    }
    values.update(changes)
    return WaveformCacheKey(**values)  # type: ignore[arg-type]


def make_data(*, complete: bool = True) -> WaveformData:
    return WaveformData(
        minimum=np.array([-1.0, -0.25], dtype=np.float32),
        maximum=np.array([0.5, 1.0], dtype=np.float32),
        bucket_duration_ms=20.0,
        duration_ms=40,
        complete=complete,
    )


def test_cache_key_is_stable_and_contains_no_source_name(tmp_path: Path) -> None:
    """同じ属性は同じhashとなり、生ファイル名をcache filenameへ含めない。"""
    source = make_source(tmp_path)
    first = WaveformCacheKey.from_path(source)
    second = WaveformCacheKey.from_path(source)
    assert first.digest == second.digest
    assert first.filename == f"{first.digest}.npz"
    assert source.name not in first.filename
    assert len(first.digest) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"size": 6},
        {"mtime_ns": 1},
        {"analysis_version": 2},
        {"bucket_ms": 10},
        {"format_version": 2},
    ],
)
def test_cache_key_changes_with_each_input(tmp_path: Path, change: dict[str, int]) -> None:
    """size、mtime、解析version、bucket、mono formatがkeyを無効化する。"""
    source = make_source(tmp_path)
    assert make_key(source, **change).digest != make_key(source).digest


def test_cache_key_changes_with_path_and_requires_absolute_path(tmp_path: Path) -> None:
    """pathもkeyへ含め、直接構築の相対pathは拒否する。"""
    first = make_source(tmp_path, "一.wav")
    second = make_source(tmp_path, "二.wav")
    assert WaveformCacheKey.from_path(first).digest != WaveformCacheKey.from_path(second).digest
    with pytest.raises(ValueError, match="絶対"):
        WaveformCacheKey(Path("relative.wav"), 1, 1)


def test_cache_round_trip_preserves_float32_and_read_only(tmp_path: Path) -> None:
    """正常なnpz往復でdtypeと不変性を維持し、hit時刻を更新する。"""
    source = make_source(tmp_path)
    key = WaveformCacheKey.from_path(source)
    cache = WaveformCache(tmp_path / "cache")
    saved_path = cache.save(key, make_data())
    old_time = 1_000_000_000
    os.utime(saved_path, ns=(old_time, old_time))

    loaded = cache.load(key)

    assert loaded is not None
    np.testing.assert_array_equal(loaded.minimum, [-1.0, -0.25])
    assert loaded.minimum.dtype == np.float32
    assert not loaded.minimum.flags.writeable
    assert saved_path.stat().st_mtime_ns > old_time


def test_cache_miss_does_not_create_directory(tmp_path: Path) -> None:
    """cache missは正常扱いでfilesystem副作用を起こさない。"""
    cache_dir = tmp_path / "cache"
    key = WaveformCacheKey.from_path(make_source(tmp_path))
    assert WaveformCache(cache_dir).load(key) is None
    assert not cache_dir.exists()


@pytest.mark.parametrize(
    "override",
    [
        {"analysis_version": np.int64(2)},
        {"file_size": np.int64(999)},
        {"file_mtime_ns": np.int64(1)},
        {"minimum": np.array([-1.0], dtype=np.float32)},
        {"maximum": np.array([0.5], dtype=np.float32)},
        {"minimum": np.array([-1.0, 0.0], dtype=np.float64)},
        {"minimum": np.array([np.nan, 0.0], dtype=np.float32)},
        {"minimum": np.array([0.75, 0.0], dtype=np.float32)},
        {"bucket_duration_ms": np.float64(0.0)},
        {"duration_ms": np.int64(-1)},
        {"complete": np.bool_(False)},
    ],
)
def test_invalid_npz_is_deleted_and_treated_as_miss(
    tmp_path: Path, override: dict[str, np.ndarray | np.generic]
) -> None:
    """field不整合、dtype、非有限値、min>max、部分結果を破損扱いにする。"""
    source = make_source(tmp_path)
    key = WaveformCacheKey.from_path(source)
    cache = WaveformCache(tmp_path / "cache")
    path = cache.save(key, make_data())
    fields: dict[str, np.ndarray | np.generic] = {
        "minimum": np.array([-1.0, -0.25], dtype=np.float32),
        "maximum": np.array([0.5, 1.0], dtype=np.float32),
        "bucket_duration_ms": np.float64(20.0),
        "duration_ms": np.int64(40),
        "analysis_version": np.int64(key.analysis_version),
        "format_version": np.int64(key.format_version),
        "file_size": np.int64(key.size),
        "file_mtime_ns": np.int64(key.mtime_ns),
        "complete": np.bool_(True),
    }
    fields.update(override)
    # NumPy stubは任意の名前付き配列をallow_pickle引数候補と誤認する。
    np.savez(path, **fields)  # pyright: ignore[reportArgumentType]

    assert cache.load(key) is None
    assert not path.exists()


def test_broken_zip_and_missing_field_can_be_replaced(tmp_path: Path) -> None:
    """zip破損や必須field欠落後も再解析結果を保存できる。"""
    key = WaveformCacheKey.from_path(make_source(tmp_path))
    cache = WaveformCache(tmp_path / "cache")
    path = cache.save(key, make_data())
    path.write_bytes(b"broken")
    assert cache.load(key) is None
    np.savez(path, minimum=np.empty(0, dtype=np.float32))
    assert cache.load(key) is None
    assert cache.save(key, make_data()).is_file()


def test_save_rejects_partial_before_filesystem_side_effect(tmp_path: Path) -> None:
    """部分結果の保存拒否ではcache directoryも作らない。"""
    cache_dir = tmp_path / "new-cache"
    key = WaveformCacheKey.from_path(make_source(tmp_path))
    with pytest.raises(ValueError, match="部分"):
        WaveformCache(cache_dir).save(key, make_data(complete=False))
    assert not cache_dir.exists()


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_atomic_save_failure_preserves_existing_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """fsync／replace失敗で既存cacheを壊さずtempを回収する。"""
    key = WaveformCacheKey.from_path(make_source(tmp_path))
    cache = WaveformCache(tmp_path / "cache")
    path = cache.save(key, make_data())
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(f"{failure}失敗")

    monkeypatch.setattr(f"sdp.core.analysis.waveform_cache.os.{failure}", fail)
    with pytest.raises(OSError):
        cache.save(key, make_data())
    assert path.read_bytes() == original
    assert list(cache.directory.glob("*.tmp")) == []


def test_npz_write_failure_preserves_existing_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npz書込失敗でも既存cacheを壊さずtempを回収する。"""
    key = WaveformCacheKey.from_path(make_source(tmp_path))
    cache = WaveformCache(tmp_path / "cache")
    path = cache.save(key, make_data())
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("npz write失敗")

    monkeypatch.setattr("sdp.core.analysis.waveform_cache.np.savez_compressed", fail)
    with pytest.raises(OSError):
        cache.save(key, make_data())
    assert path.read_bytes() == original
    assert list(cache.directory.glob("*.tmp")) == []


def test_lru_removes_oldest_deterministically_and_ignores_other_files(tmp_path: Path) -> None:
    """同mtimeは名前順で削除し、npz以外とtempは対象にしない。"""
    directory = tmp_path / "cache"
    directory.mkdir()
    first = directory / "a.npz"
    second = directory / "b.npz"
    newest = directory / "c.npz"
    other = directory / "keep.txt"
    temporary = directory / "cache.tmp"
    for path in (first, second, newest, other, temporary):
        path.write_bytes(b"1234")
    os.utime(first, ns=(1, 1))
    os.utime(second, ns=(1, 1))
    os.utime(newest, ns=(2, 2))

    WaveformCache(directory, max_bytes=8).prune()

    assert not first.exists()
    assert second.exists() and newest.exists()
    assert other.exists() and temporary.exists()


def test_lru_below_limit_deletes_nothing(tmp_path: Path) -> None:
    """合計が上限以下ならcacheを削除しない。"""
    directory = tmp_path / "cache"
    directory.mkdir()
    path = directory / "one.npz"
    path.write_bytes(b"1234")
    WaveformCache(directory, max_bytes=4).prune()
    assert path.exists()


def test_lru_continues_after_individual_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1件の削除失敗をログ対象に留め、後続cacheの削除を続ける。"""
    directory = tmp_path / "cache"
    directory.mkdir()
    paths = [directory / f"{name}.npz" for name in ("a", "b", "c")]
    for index, path in enumerate(paths):
        path.write_bytes(b"1234")
        os.utime(path, ns=(index + 1, index + 1))
    original_unlink = Path.unlink

    def selective_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == paths[0]:
            raise OSError("使用中")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", selective_unlink)
    WaveformCache(directory, max_bytes=4).prune()
    assert paths[0].exists()
    assert not paths[1].exists()
    assert not paths[2].exists()

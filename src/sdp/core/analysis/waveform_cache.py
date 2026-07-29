"""波形解析結果の検証済みnpzキャッシュ。"""

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from sdp.core.analysis.waveform import WAVEFORM_BUCKET_MS, WaveformData

_logger = logging.getLogger(__name__)

WAVEFORM_ANALYSIS_VERSION = 1
WAVEFORM_FORMAT_VERSION = 1
MAX_WAVEFORM_CACHE_BYTES = 500 * 1024 * 1024


class WaveformCacheError(Exception):
    """キャッシュが現在の契約どおり解釈できない。"""


@dataclass(frozen=True, slots=True)
class WaveformCacheKey:
    """音源の同一性と解析アルゴリズムを表すキャッシュキー。"""

    path: Path
    size: int
    mtime_ns: int
    analysis_version: int = WAVEFORM_ANALYSIS_VERSION
    bucket_ms: int = WAVEFORM_BUCKET_MS
    format_version: int = WAVEFORM_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("キャッシュキーのpathは絶対パスである必要があります")
        if self.size < 0 or self.mtime_ns < 0:
            raise ValueError("sizeとmtime_nsは0以上である必要があります")
        if self.analysis_version < 1 or self.bucket_ms < 1 or self.format_version < 1:
            raise ValueError("versionとbucket_msは1以上である必要があります")

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        analysis_version: int = WAVEFORM_ANALYSIS_VERSION,
        bucket_ms: int = WAVEFORM_BUCKET_MS,
        format_version: int = WAVEFORM_FORMAT_VERSION,
    ) -> "WaveformCacheKey":
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
        if not resolved.is_file():
            raise OSError(f"波形解析対象がファイルではありません: {resolved}")
        return cls(
            path=resolved,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            analysis_version=analysis_version,
            bucket_ms=bucket_ms,
            format_version=format_version,
        )

    @property
    def digest(self) -> str:
        document = {
            "analysis_version": self.analysis_version,
            "bucket_ms": self.bucket_ms,
            "format_version": self.format_version,
            "mtime_ns": self.mtime_ns,
            "path": str(self.path),
            "size": self.size,
        }
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def filename(self) -> str:
        return f"{self.digest}.npz"


class WaveformCache:
    """完了波形だけを保存し、利用時刻ベースで容量を制限する。"""

    def __init__(
        self,
        directory: Path,
        *,
        max_bytes: int = MAX_WAVEFORM_CACHE_BYTES,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytesは0以上である必要があります")
        self.directory = directory
        self.max_bytes = max_bytes

    def path_for(self, key: WaveformCacheKey) -> Path:
        return self.directory / key.filename

    def load(self, key: WaveformCacheKey) -> WaveformData | None:
        """正常なhitだけを返す。破損は削除してmissとして扱う。"""
        cache_path = self.path_for(key)
        if not cache_path.is_file():
            return None
        try:
            with np.load(cache_path, allow_pickle=False) as archive:
                data = _read_archive(archive, key)
        except (OSError, ValueError, TypeError, KeyError, WaveformCacheError):
            _logger.exception("波形キャッシュが破損しています。再解析します: %s", cache_path)
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                _logger.exception("破損した波形キャッシュを削除できません: %s", cache_path)
            return None
        else:
            try:
                os.utime(cache_path, None)
            except OSError:
                # 利用時刻の更新失敗はデータ破損ではない。hitはそのまま利用する。
                _logger.exception("波形キャッシュの利用時刻を更新できません: %s", cache_path)
            return data

    def save(self, key: WaveformCacheKey, data: WaveformData) -> Path:
        """完了結果をアトミック保存し、保存後にLRU上限を適用する。"""
        if not data.complete:
            raise ValueError("部分波形はキャッシュへ保存できません")
        # 公開後にwriteable flagを戻して変更された場合も、filesystem副作用前に再検証する。
        validated = WaveformData(
            minimum=data.minimum,
            maximum=data.maximum,
            bucket_duration_ms=data.bucket_duration_ms,
            duration_ms=data.duration_ms,
            complete=data.complete,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(key)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.directory, prefix=f"{key.digest}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w+b") as stream:
                np.savez_compressed(
                    stream,
                    minimum=validated.minimum,
                    maximum=validated.maximum,
                    bucket_duration_ms=np.float64(validated.bucket_duration_ms),
                    duration_ms=np.int64(validated.duration_ms),
                    analysis_version=np.int64(key.analysis_version),
                    format_version=np.int64(key.format_version),
                    file_size=np.int64(key.size),
                    file_mtime_ns=np.int64(key.mtime_ns),
                    complete=np.bool_(validated.complete),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        self.prune(protected=target)
        return target

    def prune(self, *, protected: Path | None = None) -> None:
        """`.npz`だけを古い利用時刻順に削除する。個別失敗は継続する。"""
        if not self.directory.is_dir():
            return
        entries: list[tuple[int, str, Path, int]] = []
        for path in self.directory.glob("*.npz"):
            try:
                stat = path.stat()
            except OSError:
                _logger.exception("波形キャッシュの情報を取得できません: %s", path)
                continue
            entries.append((stat.st_mtime_ns, path.name, path, stat.st_size))
        total = sum(entry[3] for entry in entries)
        for _mtime, _name, path, size in sorted(entries):
            if total <= self.max_bytes:
                break
            if protected is not None and path == protected:
                continue
            try:
                path.unlink()
            except OSError:
                _logger.exception("古い波形キャッシュを削除できません: %s", path)
                continue
            total -= size


def _read_archive(archive: np.lib.npyio.NpzFile, key: WaveformCacheKey) -> WaveformData:
    required = {
        "minimum",
        "maximum",
        "bucket_duration_ms",
        "duration_ms",
        "analysis_version",
        "format_version",
        "file_size",
        "file_mtime_ns",
        "complete",
    }
    if not required.issubset(archive.files):
        raise WaveformCacheError("必須fieldがありません")
    if _scalar_int(archive["analysis_version"]) != key.analysis_version:
        raise WaveformCacheError("analysis versionが一致しません")
    if _scalar_int(archive["format_version"]) != key.format_version:
        raise WaveformCacheError("format versionが一致しません")
    if _scalar_int(archive["file_size"]) != key.size:
        raise WaveformCacheError("file sizeが一致しません")
    if _scalar_int(archive["file_mtime_ns"]) != key.mtime_ns:
        raise WaveformCacheError("mtime_nsが一致しません")
    complete = archive["complete"]
    if complete.shape != () or complete.dtype != np.dtype(np.bool_) or not bool(complete.item()):
        raise WaveformCacheError("completeな結果ではありません")
    minimum = archive["minimum"]
    maximum = archive["maximum"]
    if minimum.dtype != np.dtype(np.float32) or maximum.dtype != np.dtype(np.float32):
        raise WaveformCacheError("波形dtypeがfloat32ではありません")
    bucket = _scalar_float(archive["bucket_duration_ms"])
    duration = _scalar_int(archive["duration_ms"])
    return WaveformData(
        minimum=minimum,
        maximum=maximum,
        bucket_duration_ms=bucket,
        duration_ms=duration,
        complete=True,
    )


def _scalar_int(value: np.ndarray) -> int:
    if value.shape != () or value.dtype.kind not in "iu":
        raise WaveformCacheError("整数scalar fieldが不正です")
    return int(value.item())


def _scalar_float(value: np.ndarray) -> float:
    if value.shape != () or value.dtype.kind != "f":
        raise WaveformCacheError("浮動小数scalar fieldが不正です")
    return float(value.item())

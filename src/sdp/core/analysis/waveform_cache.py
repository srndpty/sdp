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
WAVEFORM_FORMAT_VERSION = 2
"""キャッシュファイルの構造version。

- version 1: path・size・mtime_ns で同一性を判定
- version 2: 先頭・中央・末尾の内容 fingerprint を追加

version 1 のファイルは version 不一致で無効化され、再解析される。
"""

FINGERPRINT_CHUNK_BYTES = 64 * 1024
"""fingerprintで読む1区間のbyte数（先頭・中央・末尾の3か所）。"""
MAX_WAVEFORM_CACHE_BYTES = 500 * 1024 * 1024


class WaveformCacheError(Exception):
    """キャッシュが現在の契約どおり解釈できない。"""


def content_fingerprint(path: Path, size: int) -> str:
    """ファイル内容の軽量な指紋（先頭・中央・末尾の各64KiBとサイズ）。

    size と mtime だけでは音源を同定できない。バックアップ復元、同期ソフト、
    一部のタグ編集ツールはタイムスタンプを維持したまま内容を差し替えるため、
    古い波形をそのまま返してしまう。全体のSHA-256は長時間音源で重すぎるので、
    3か所の抜き取りで実用上の取り違えを防ぐ。

    先頭はヘッダーとタグ、末尾は末尾タグと切り詰め、中央は本体の差し替えを
    捉える。**暗号学的な同一性の保証ではない**（悪意ある衝突は想定しない）。
    """
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    if size <= 0:
        return digest.hexdigest()
    offsets = _fingerprint_offsets(size)
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            digest.update(offset.to_bytes(8, "big"))
            digest.update(stream.read(FINGERPRINT_CHUNK_BYTES))
    return digest.hexdigest()


def _fingerprint_offsets(size: int) -> tuple[int, ...]:
    """読み取り開始位置を重複なく昇順で返す。"""
    candidates = (
        0,
        max(0, size // 2 - FINGERPRINT_CHUNK_BYTES // 2),
        max(0, size - FINGERPRINT_CHUNK_BYTES),
    )
    offsets: list[int] = []
    for offset in sorted(candidates):
        if offset not in offsets:
            offsets.append(offset)
    return tuple(offsets)


@dataclass(frozen=True, slots=True)
class WaveformCacheKey:
    """音源の同一性と解析アルゴリズムを表すキャッシュキー。"""

    path: Path
    size: int
    mtime_ns: int
    content_fingerprint: str = ""
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
        if not self.content_fingerprint:
            raise ValueError("content_fingerprintは空にできません")

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
            content_fingerprint=content_fingerprint(resolved, stat.st_size),
            analysis_version=analysis_version,
            bucket_ms=bucket_ms,
            format_version=format_version,
        )

    @property
    def digest(self) -> str:
        document = {
            "analysis_version": self.analysis_version,
            "bucket_ms": self.bucket_ms,
            "content_fingerprint": self.content_fingerprint,
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
                    content_fingerprint=np.str_(key.content_fingerprint),
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
        "content_fingerprint",
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
    if _scalar_str(archive["content_fingerprint"]) != key.content_fingerprint:
        # size・mtimeを保ったまま内容が差し替えられた場合はここで弾く。
        raise WaveformCacheError("content fingerprintが一致しません")
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


def _scalar_str(value: np.ndarray) -> str:
    if value.shape != () or value.dtype.kind not in "US":
        raise WaveformCacheError("文字列scalar fieldが不正です")
    return str(value.item())


def _scalar_int(value: np.ndarray) -> int:
    if value.shape != () or value.dtype.kind not in "iu":
        raise WaveformCacheError("整数scalar fieldが不正です")
    return int(value.item())


def _scalar_float(value: np.ndarray) -> float:
    if value.shape != () or value.dtype.kind != "f":
        raise WaveformCacheError("浮動小数scalar fieldが不正です")
    return float(value.item())

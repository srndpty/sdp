"""メタデータの Qt 非依存な型。

読み込み状態（:class:`MetadataStatus`）と値（:class:`TrackMetadata`）を分ける。
値の側にはエラー文字列も entry_id も持たせない。
"""

from dataclasses import dataclass
from enum import Enum, auto


class MetadataStatus(Enum):
    """メタデータの読み込み状態。

    ファイルの欠損は :class:`~sdp.core.playlist.entry.FileStatus` が表すため、
    ここへ ``MISSING`` は入れない。
    """

    NOT_REQUESTED = auto()
    """まだ読み取りを要求していない（欠損中、または欠損から復活した直後を含む）。"""

    LOADING = auto()
    """最新の読み取り要求が進行中。"""

    LOADED = auto()
    """読み取りが正常に終わった。タグが 1 件も無い場合もここに入る。"""

    FAILED = auto()
    """ファイルはあったが、未対応形式・破損・権限などで読み取れなかった。"""


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """ファイルから読み取った静的な情報。

    どれも取得できないことがあるため、すべて省略可能。
    :attr:`duration_ms` は再生中の ``PlaybackController.duration_ms`` とは別物で、
    両者を同期させない。
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration_ms: int | None = None
    file_size_bytes: int | None = None
    bitrate_bps: int | None = None


def format_duration_ms(milliseconds: int) -> str:
    """ミリ秒を表示用の文字列へ変換する（純粋関数）。

    1 時間未満は ``m:ss``、1 時間以上は ``h:mm:ss``。

    負値は 0 として扱う。Qt は読み込み直後や停止直後に一時的な負の位置を返しうるため、
    表示の境界では例外にせず 0 表示にする方が実用的（値の検証は Controller の責務）。
    """
    total_seconds = max(milliseconds, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_file_size(size_bytes: int) -> str:
    """バイト数を読みやすい2進単位へ整形する。"""
    size = max(size_bytes, 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units[:-1]:
        if value < 1024.0:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} {units[-1]}"


def format_bitrate(bitrate_bps: int) -> str:
    """bit/sを一般的なkbps表記へ整形する。"""
    return f"{max(bitrate_bps, 0) / 1000:.0f} kbps"

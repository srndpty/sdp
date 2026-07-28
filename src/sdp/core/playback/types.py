"""再生に関する Qt 非依存の型。

UI と PlaybackController はこの層の型だけを扱う。
Qt の enum（``QMediaPlayer.PlaybackState`` など）や ``QUrl`` を
この層より上へ公開してはならない。変換は各 Backend の実装が担う。
"""

from dataclasses import dataclass
from enum import Enum, auto


class PlaybackState(Enum):
    """再生状態。

    Qt の ``QMediaPlayer.PlaybackState``（Stopped / Playing / Paused）に
    「音源が未設定」を表す :attr:`NO_MEDIA` を加えたもの。

    ``NO_MEDIA`` は source が設定されていない場合に限る。source が設定された後は、
    読み込み中・読み込み完了後の未再生・再生終了・読み込み失敗のいずれも、
    再生中でも一時停止中でもなければ ``STOPPED`` とする。

    読み込み中・読み込み失敗は独立した状態としてここへ入れない。
    進行状況は :class:`MediaStatus`、失敗は :class:`PlaybackError` で別に扱う。
    Qt 自身も両者を別のプロパティとして持っており、状態を増やすと
    「読み込み中に一時停止していたか」のような情報が失われるため。
    """

    NO_MEDIA = auto()
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()


class MediaStatus(Enum):
    """音源の読み込み状況。

    ``QMediaPlayer.MediaStatus`` の 8 値と 1 対 1 に対応させてある。
    値を間引くと Backend が未知値を既定値へ丸める必要が生じ、
    silent fallback になってしまうため、写像を完全にしている。
    """

    NO_MEDIA = auto()
    LOADING = auto()
    LOADED = auto()
    STALLED = auto()
    BUFFERING = auto()
    BUFFERED = auto()
    END_OF_MEDIA = auto()
    INVALID_MEDIA = auto()


class PlaybackErrorCode(Enum):
    """アプリ内の再生エラー分類。

    - 先頭 2 つは PlaybackController が読み込み前の検査で生成する。
    - 残りは Backend が自身のエラー（``QMediaPlayer.Error`` 等）から写す。
    """

    SOURCE_NOT_FOUND = auto()
    SOURCE_NOT_A_FILE = auto()
    RESOURCE_ERROR = auto()
    FORMAT_ERROR = auto()
    NETWORK_ERROR = auto()
    ACCESS_DENIED = auto()
    UNKNOWN_ERROR = auto()


@dataclass(frozen=True, slots=True)
class PlaybackError:
    """UI へ通知する再生エラー。

    例外オブジェクトそのものを UI へ渡さないための境界。
    :attr:`detail` はログ用であり、そのままユーザーへ表示しない。
    """

    code: PlaybackErrorCode
    """アプリ内エラーコード。UI が分岐に使う。"""

    message: str
    """ユーザー向けの日本語メッセージ。技術詳細を含めない。"""

    detail: str
    """ログ向けの技術詳細（Qt のエラー文字列、パスなど）。"""

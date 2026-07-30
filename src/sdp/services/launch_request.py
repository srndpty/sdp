"""コマンドラインとIPCで共有する、Qt非依存の起動要求。"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    """プレイリストへ追加するパスと、無視した引数の不変snapshot。

    ``paths`` は絶対パスで、順序と重複を維持する。欠損パスと未知拡張子も、
    既存の :class:`PlaylistModel` と同じく拒否しない。
    """

    paths: tuple[Path, ...] = ()
    ignored_arguments: tuple[str, ...] = ()
    activate_window: bool = True

    def __post_init__(self) -> None:
        if any(not path.is_absolute() for path in self.paths):
            raise ValueError("LaunchRequestのpathは絶対パスである必要があります")
        if type(self.activate_window) is not bool:
            raise TypeError("activate_windowはboolである必要があります")


def parse_launch_request(arguments: Sequence[str], current_directory: Path) -> LaunchRequest:
    """OSが分割済みの引数を起動時current directory基準で絶対化する。

    引用符の再解釈、拡張子判定、ファイルシステム照会は行わない。
    Pathとして解釈不能な引数だけを除外し、順序と重複を維持する。
    ディレクトリの拒否は既存のPlaylistModel追加経路に任せる。
    """
    base = Path(os.path.abspath(current_directory))
    paths: list[Path] = []
    ignored: list[str] = []
    for argument in arguments:
        if "\0" in argument:
            ignored.append(argument)
            continue
        try:
            candidate = Path(argument)
            if not candidate.is_absolute():
                candidate = base / candidate
            candidate = Path(os.path.abspath(candidate))
        except (OSError, ValueError):
            ignored.append(argument)
            continue
        paths.append(candidate)
    return LaunchRequest(tuple(paths), tuple(ignored))

"""ウィンドウ状態など日常利用で自然に変わるUI状態のファイル形式と補正ロジック。

設定画面から明示的に変更する :mod:`sdp.services.settings` の ``AppSettings`` とは
**別ファイル・別責務**として扱う（``ui-state.json``）。ユーザーが意識して選ぶ設定と、
使っているうちに勝手に変わる位置・サイズを同じファイルへ混ぜない。

このモジュールは Qt 非依存とする。MainWindow・QMainWindow・QSplitter・
QFileDialog・PlaybackController・PlaylistModel・AppSettingsController は参照しない。
Qt の ``saveGeometry()`` / ``QByteArray`` を base64 で保存する方式も採らず、
手編集・デバッグ・画面外補正がしやすい**意味の明確な整数値**で保存する。
"""

import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, cast

_logger = logging.getLogger(__name__)

UI_STATE_SCHEMA_VERSION = 1
RESTORE_FAILED_MESSAGE = "ウィンドウ状態の復元に失敗しました。既定の位置で起動します。"

MINIMUM_VISIBLE_WIDTH = 80
"""画面内に残すべきウィンドウ上端の最小幅（px）。"""

TITLE_BAR_BAND_HEIGHT = 24
"""タイトルバー相当とみなす上端の高さ（px）。ここが画面内にあれば掴んで動かせる。"""

MINIMUM_SPLITTER_SIZE = 60
"""Splitterの片側が完全に潰れないための最小サイズ（px）。"""


class UiStateFileError(Exception):
    """ui-state.jsonが壊れている、または契約どおり解釈できない。"""


@dataclass(frozen=True, slots=True)
class ScreenRect:
    """画面の利用可能領域（Qt非依存の整数矩形）。

    マルチモニターでは負の座標が正当な値になるため、負のx／yを拒否しない。
    """

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            _validated_int(name, getattr(self, name))
        for name in ("width", "height"):
            if _validated_int(name, getattr(self, name)) < 1:
                raise ValueError(f"{name}は正の整数である必要があります")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True, slots=True)
class WindowState:
    """ウィンドウのnormal geometryと最大化状態。

    **最大化中でもnormal geometryを保存する**（最大化された画面全体のサイズを
    normal sizeとして保存しない）。最小化状態は保存しない。
    """

    x: int
    y: int
    width: int
    height: int
    maximized: bool

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            _validated_int(name, getattr(self, name))
        for name in ("width", "height"):
            if _validated_int(name, getattr(self, name)) < 1:
                raise ValueError(f"{name}は正の整数である必要があります")
        if type(self.maximized) is not bool:
            raise TypeError(f"maximizedはboolである必要があります: {self.maximized!r}")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True, slots=True)
class SplitterState:
    """メインSplitterの上下サイズ。復元時は比率として使う。"""

    player_size: int
    playlist_size: int

    def __post_init__(self) -> None:
        for name in ("player_size", "playlist_size"):
            if _validated_int(name, getattr(self, name)) < 0:
                raise ValueError(f"{name}は0以上の整数である必要があります")
        if self.total < 1:
            raise ValueError("Splitterの合計サイズは正である必要があります")

    @property
    def total(self) -> int:
        return self.player_size + self.playlist_size

    @property
    def player_ratio(self) -> float:
        return self.player_size / self.total


@dataclass(frozen=True, slots=True)
class UiState:
    """保存するUI状態一式。未保存の項目は ``None``。"""

    window: WindowState | None = None
    main_splitter: SplitterState | None = None
    last_open_directory: Path | None = None

    def __post_init__(self) -> None:
        # 型注釈はあるが、呼び出し側の実行時の誤りも表面化させる。
        for name, expected in (("window", WindowState), ("main_splitter", SplitterState)):
            value: object = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise TypeError(f"{name}は{expected.__name__}である必要があります: {value!r}")
        directory: object = self.last_open_directory
        if directory is not None:
            if not isinstance(directory, Path):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise TypeError(f"last_open_directoryはPathである必要があります: {directory!r}")
            if not _is_absolute_path(directory):
                raise ValueError(f"last_open_directoryは絶対パスである必要があります: {directory}")


def load_ui_state(file_path: Path) -> UiState:
    """UI状態を読み込む。未作成なら既定（すべて未保存）を返す。

    未知のキーは無視し、既知キーの欠落は「未保存」として扱う。既知キーの値が
    不正な場合だけ :class:`UiStateFileError` とする。
    ``last_open_directory`` の**存在確認はここでは行わない**（外付けドライブや
    ネットワークドライブが後で戻ることがあるため）。利用時にfallbackする。
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UiState()
    except UnicodeDecodeError as error:
        raise UiStateFileError(f"UI状態ファイルがUTF-8として不正です: {file_path}") from error
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise UiStateFileError(f"UI状態ファイルがJSONとして不正です: {file_path}") from error
    if not isinstance(parsed, dict):
        raise UiStateFileError(f"UI状態ファイルのルートがオブジェクトではありません: {file_path}")
    document = cast("dict[str, object]", parsed)

    version = document.get("schema_version")
    if type(version) is not int or version != UI_STATE_SCHEMA_VERSION:
        raise UiStateFileError(
            f"未対応のUI状態schema_versionです（期待 {UI_STATE_SCHEMA_VERSION}、実際 {version!r}）"
        )
    return UiState(
        window=_window_from_json(document.get("window")),
        main_splitter=_splitter_from_json(document.get("main_splitter")),
        last_open_directory=_directory_from_json(document.get("last_open_directory")),
    )


def save_ui_state(file_path: Path, state: UiState) -> None:
    """UI状態を同一ディレクトリの一時ファイル経由でアトミック保存する。"""
    document: dict[str, Any] = {"schema_version": UI_STATE_SCHEMA_VERSION}
    window = state.window
    if window is not None:
        document["window"] = {
            "x": window.x,
            "y": window.y,
            "width": window.width,
            "height": window.height,
            "maximized": window.maximized,
        }
    splitter = state.main_splitter
    if splitter is not None:
        document["main_splitter"] = {
            "player": splitter.player_size,
            "playlist": splitter.playlist_size,
        }
    if state.last_open_directory is not None:
        document["last_open_directory"] = str(state.last_open_directory)

    # 検証に成功するまでディレクトリも一時ファイルも作らない。
    file_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=file_path.parent, prefix=f"{file_path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, file_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def fit_window_state(
    state: WindowState,
    screens: Sequence[ScreenRect],
    *,
    minimum_size: tuple[int, int] = (1, 1),
) -> WindowState:
    """保存済みgeometryを現在の画面構成に合わせて補正する。

    契約:

    - **完全に画面外なら**primary screen（``screens[0]``）の中央へ移動する。
    - タイトルバー相当の帯が最も重なる画面、次にウィンドウ矩形が最も重なる画面を
      復元先とする。マルチモニターの負座標は正当な値として扱う。
    - サイズは復元先screenのavailable sizeを超えないようclampし、
      ``minimum_size`` を下回らないようにする。
    - 画面情報が取れない場合（``screens`` が空）は、サイズの下限だけを保証して
      位置には触れない（誤った補正で画面外へ動かさない）。
    """
    minimum_width, minimum_height = (max(1, value) for value in minimum_size)
    width = max(state.width, minimum_width)
    height = max(state.height, minimum_height)
    if not screens:
        return WindowState(state.x, state.y, width, height, state.maximized)

    primary = screens[0]
    target = _best_matching_screen(state, screens)
    if target is None:
        width = min(width, primary.width)
        height = min(height, primary.height)
        # どの画面とも重ならない（モニターを外した等）。primary中央へ戻す。
        return WindowState(
            x=primary.x + (primary.width - width) // 2,
            y=primary.y + (primary.height - height) // 2,
            width=width,
            height=height,
            maximized=state.maximized,
        )

    target_width = min(width, target.width)
    target_height = min(height, target.height)
    size_was_clamped = target_width != width or target_height != height
    width = target_width
    height = target_height
    candidate = WindowState(state.x, state.y, width, height, state.maximized)
    if size_was_clamped:
        return _fit_position_inside_screen(candidate, target)
    if _title_band_is_visible(candidate, target):
        return candidate

    return _fit_position_to_keep_title_band_visible(candidate, target)


def _best_matching_screen(state: WindowState, screens: Sequence[ScreenRect]) -> ScreenRect | None:
    """保存矩形が属する画面を、タイトル帯、全体矩形の順で選ぶ。"""
    title_overlaps = [(_title_band_overlap_area(state, screen), screen) for screen in screens]
    title_area, title_screen = max(title_overlaps, key=lambda item: item[0])
    if title_area > 0:
        return title_screen

    window_overlaps = [(_window_overlap_area(state, screen), screen) for screen in screens]
    window_area, window_screen = max(window_overlaps, key=lambda item: item[0])
    return window_screen if window_area > 0 else None


def _fit_position_to_keep_title_band_visible(state: WindowState, screen: ScreenRect) -> WindowState:
    """選んだ画面でタイトル帯を掴める最小範囲だけ位置を補正する。"""
    visible_width = min(MINIMUM_VISIBLE_WIDTH, state.width)
    band_height = min(TITLE_BAR_BAND_HEIGHT, state.height)
    x = max(screen.x - state.width + visible_width, state.x)
    x = min(screen.right - visible_width, x)
    y = max(screen.y, state.y)
    y = min(screen.bottom - band_height, y)
    return WindowState(
        x=x,
        y=y,
        width=state.width,
        height=state.height,
        maximized=state.maximized,
    )


def _fit_position_inside_screen(state: WindowState, screen: ScreenRect) -> WindowState:
    """サイズ補正後の矩形全体を、選んだ画面のavailable geometryへ収める。"""
    x = max(screen.x, min(screen.right - state.width, state.x))
    y = max(screen.y, min(screen.bottom - state.height, state.y))
    return WindowState(x, y, state.width, state.height, state.maximized)


def distribute_splitter_sizes(
    state: SplitterState,
    total: int,
    *,
    minimum: int = MINIMUM_SPLITTER_SIZE,
) -> tuple[int, int]:
    """保存済みサイズを**比率**として現在の利用可能高さへ再配分する。

    絶対値をそのまま適用すると、前回と違うウィンドウ高さでプレイリストが極端に
    狭くなったり、可視化OFFで余白が生まれたりするため、比率を契約とする。
    片側が完全に潰れないよう、``minimum``（総量が小さい場合は総量の半分）を下限にする。
    """
    if _validated_int("total", total) < 1:
        return (state.player_size, state.playlist_size)
    limit = min(minimum, total // 2)
    player = round(total * state.player_ratio)
    player = max(limit, min(total - limit, player))
    return (player, total - player)


def _title_band_is_visible(state: WindowState, screen: ScreenRect) -> bool:
    """ウィンドウ上端の帯が画面と十分に重なっているか。"""
    overlap_width = min(state.right, screen.right) - max(state.x, screen.x)
    band_bottom = state.y + min(TITLE_BAR_BAND_HEIGHT, state.height)
    overlap_height = min(band_bottom, screen.bottom) - max(state.y, screen.y)
    return overlap_width >= min(MINIMUM_VISIBLE_WIDTH, state.width) and overlap_height >= min(
        TITLE_BAR_BAND_HEIGHT, state.height
    )


def _title_band_overlap_area(state: WindowState, screen: ScreenRect) -> int:
    """ウィンドウ上端のタイトル帯と画面の交差面積。"""
    band_bottom = state.y + min(TITLE_BAR_BAND_HEIGHT, state.height)
    return _intersection_area(
        state.x,
        state.y,
        state.right,
        band_bottom,
        screen.x,
        screen.y,
        screen.right,
        screen.bottom,
    )


def _window_overlap_area(state: WindowState, screen: ScreenRect) -> int:
    """ウィンドウ矩形と画面の交差面積。"""
    return _intersection_area(
        state.x,
        state.y,
        state.right,
        state.bottom,
        screen.x,
        screen.y,
        screen.right,
        screen.bottom,
    )


def _intersection_area(
    left_a: int,
    top_a: int,
    right_a: int,
    bottom_a: int,
    left_b: int,
    top_b: int,
    right_b: int,
    bottom_b: int,
) -> int:
    width = max(0, min(right_a, right_b) - max(left_a, left_b))
    height = max(0, min(bottom_a, bottom_b) - max(top_a, top_b))
    return width * height


def _window_from_json(value: object) -> WindowState | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UiStateFileError(f"windowがオブジェクトではありません: {value!r}")
    document = cast("dict[str, object]", value)
    try:
        return WindowState(
            x=_int_from_json("window.x", document.get("x")),
            y=_int_from_json("window.y", document.get("y")),
            width=_int_from_json("window.width", document.get("width")),
            height=_int_from_json("window.height", document.get("height")),
            maximized=_bool_from_json("window.maximized", document.get("maximized", False)),
        )
    except (TypeError, ValueError) as error:
        raise UiStateFileError(f"windowの値が不正です: {error}") from error


def _splitter_from_json(value: object) -> SplitterState | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UiStateFileError(f"main_splitterがオブジェクトではありません: {value!r}")
    document = cast("dict[str, object]", value)
    try:
        return SplitterState(
            player_size=_int_from_json("main_splitter.player", document.get("player")),
            playlist_size=_int_from_json("main_splitter.playlist", document.get("playlist")),
        )
    except (TypeError, ValueError) as error:
        raise UiStateFileError(f"main_splitterの値が不正です: {error}") from error


def _directory_from_json(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UiStateFileError(f"last_open_directoryが文字列ではありません: {value!r}")
    if not value:
        return None
    directory = Path(value)
    if not _is_absolute_path(directory):
        raise UiStateFileError(f"last_open_directoryが絶対パスではありません: {value!r}")
    return directory


def _is_absolute_path(path: Path) -> bool:
    """Windowsの絶対パスも、実行環境に依存せず絶対と判定する。

    テストと実運用のどちらでも同じ判定にするため、POSIX上でも
    ``C:\\Music`` のような保存値を絶対パスとして扱う。
    """
    return path.is_absolute() or PureWindowsPath(path).is_absolute()


def _int_from_json(name: str, value: object) -> int:
    if value is None:
        raise ValueError(f"{name}がありません")
    # boolはintのサブクラスだが、数値欄としては受理しない。
    if type(value) is not int:
        raise TypeError(f"{name}が整数ではありません: {value!r}")
    return value


def _bool_from_json(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name}がboolではありません: {value!r}")
    return value


def _validated_int(name: str, value: object) -> int:
    # boolはintのサブクラスだが、座標・サイズとしては受理しない。
    if type(value) is not int:
        raise TypeError(f"{name}は整数である必要があります: {value!r}")
    return value

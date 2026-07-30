"""ui-state.jsonのQt非依存なschema・検証・アトミック保存・画面補正を検証する。

画面矩形は純粋な整数矩形として注入するため、実際のモニター構成に依存しない。
"""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sdp.services.ui_state import (
    MINIMUM_SPLITTER_SIZE,
    UI_STATE_SCHEMA_VERSION,
    ScreenRect,
    SplitterState,
    UiState,
    UiStateFileError,
    WindowState,
    distribute_splitter_sizes,
    fit_window_state,
    load_ui_state,
    save_ui_state,
)

FULL_HD = ScreenRect(x=0, y=0, width=1920, height=1040)
LEFT_MONITOR = ScreenRect(x=-1920, y=0, width=1920, height=1040)
TOP_MONITOR = ScreenRect(x=0, y=-1080, width=1920, height=1040)
WINDOW = WindowState(x=120, y=80, width=960, height=760, maximized=False)
SPLITTER = SplitterState(player_size=400, playlist_size=300)


def write_document(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def document_of(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "window": {"x": 120, "y": 80, "width": 960, "height": 760, "maximized": False},
        "main_splitter": {"player": 400, "playlist": 300},
        "last_open_directory": "C:\\Music",
    }
    document.update(overrides)
    return document


# -- 値オブジェクト ---------------------------------------------------------


def test_default_ui_state_is_empty() -> None:
    """既定はすべて未保存（初回起動）。"""
    state = UiState()

    assert state.window is None
    assert state.main_splitter is None
    assert state.last_open_directory is None


def test_ui_state_is_immutable() -> None:
    """frozen dataclassのため書き換えられない。"""
    with pytest.raises(FrozenInstanceError):
        UiState().window = WINDOW  # type: ignore[misc]


@pytest.mark.parametrize("value", [True, False])
def test_window_rejects_bool_as_coordinate(value: bool) -> None:
    """boolを座標・サイズとして受理しない。"""
    with pytest.raises(TypeError):
        WindowState(x=value, y=0, width=100, height=100, maximized=False)  # type: ignore[arg-type]


@pytest.mark.parametrize(("width", "height"), [(0, 100), (100, 0), (-1, 100), (100, -1)])
def test_window_requires_positive_size(width: int, height: int) -> None:
    """width／heightは正でなければならない。"""
    with pytest.raises(ValueError, match="正の整数"):
        WindowState(x=0, y=0, width=width, height=height, maximized=False)


def test_window_accepts_negative_position() -> None:
    """マルチモニターの負座標は正当な値として受け入れる。"""
    state = WindowState(x=-1800, y=-200, width=800, height=600, maximized=False)

    assert state.x == -1800
    assert state.y == -200


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_window_requires_exact_bool_for_maximized(value: object) -> None:
    """maximizedは厳密なboolだけを受理する。"""
    with pytest.raises(TypeError):
        WindowState(x=0, y=0, width=10, height=10, maximized=value)  # type: ignore[arg-type]


def test_splitter_allows_zero_on_one_side() -> None:
    """片側0は許容する（合計が正であればよい）。"""
    state = SplitterState(player_size=0, playlist_size=500)

    assert state.total == 500
    assert state.player_ratio == 0.0


@pytest.mark.parametrize(("player", "playlist"), [(0, 0), (-1, 10)])
def test_splitter_rejects_empty_or_negative(player: int, playlist: int) -> None:
    """合計0や負値は拒否する。"""
    with pytest.raises(ValueError):
        SplitterState(player_size=player, playlist_size=playlist)


def test_last_open_directory_must_be_absolute() -> None:
    """相対パスは保存対象にしない。"""
    with pytest.raises(ValueError, match="絶対パス"):
        UiState(last_open_directory=Path("音楽"))


def test_last_open_directory_accepts_windows_and_unicode_paths() -> None:
    """日本語・空白を含むWindows絶対パスを保持できる。"""
    state = UiState(last_open_directory=Path("C:\\音 楽\\テスト"))

    assert state.last_open_directory == Path("C:\\音 楽\\テスト")


# -- 読み書き ---------------------------------------------------------------


def test_missing_file_returns_the_default_state(tmp_path: Path) -> None:
    """未作成は初回起動として既定状態を返す。"""
    assert load_ui_state(tmp_path / "ui-state.json") == UiState()


def test_round_trip_keeps_every_field(tmp_path: Path) -> None:
    """window・splitter・前回フォルダーが往復する。"""
    path = tmp_path / "日本語" / "ui-state.json"
    expected = UiState(
        window=WindowState(x=-1800, y=-200, width=800, height=600, maximized=True),
        main_splitter=SPLITTER,
        last_open_directory=Path("C:\\音 楽"),
    )

    save_ui_state(path, expected)

    assert load_ui_state(path) == expected
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == UI_STATE_SCHEMA_VERSION == 2
    assert set(document) == {"schema_version", "window", "main_splitter", "last_open_directory"}


def test_empty_state_saves_only_the_schema_version(tmp_path: Path) -> None:
    """未保存の項目はキーごと書かない。"""
    path = tmp_path / "ui-state.json"

    save_ui_state(path, UiState())

    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 2}
    assert load_ui_state(path) == UiState()


@pytest.mark.parametrize("name", ["window", "main_splitter", "last_open_directory"])
def test_missing_sections_are_treated_as_unsaved(tmp_path: Path, name: str) -> None:
    """既知キーの欠落は失敗ではなく「未保存」とする。"""
    path = tmp_path / "ui-state.json"
    document = document_of()
    del document[name]
    write_document(path, document)

    assert getattr(load_ui_state(path), name) is None


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """将来の未知キーは既知schemaの解釈を妨げない。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(future={"未知": True}, window_extra=1))

    state = load_ui_state(path)

    assert state.window == WINDOW
    assert state.main_splitter == SPLITTER


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 0, 3, 99])
def test_schema_version_requires_a_supported_integer(tmp_path: Path, version: object) -> None:
    """versionはbool・float・文字列・欠落・未知値を拒否する（1と2だけ許可）。"""
    path = tmp_path / "ui-state.json"
    document = document_of()
    if version is None:
        del document["schema_version"]
    else:
        document["schema_version"] = version
    write_document(path, document)

    with pytest.raises(UiStateFileError, match="schema_version"):
        load_ui_state(path)


@pytest.mark.parametrize(
    "window",
    [
        {"x": 0, "y": 0, "width": 0, "height": 100, "maximized": False},
        {"x": 0, "y": 0, "width": 100, "height": 0, "maximized": False},
        {"x": 0, "y": 0, "width": 100, "height": 100, "maximized": 1},
        {"x": True, "y": 0, "width": 100, "height": 100, "maximized": False},
        {"x": 0.5, "y": 0, "width": 100, "height": 100, "maximized": False},
        {"x": "0", "y": 0, "width": 100, "height": 100, "maximized": False},
        {"y": 0, "width": 100, "height": 100, "maximized": False},
        [],
    ],
)
def test_invalid_window_is_rejected(tmp_path: Path, window: object) -> None:
    """windowの不正な既知値は復元失敗にする（既定値へ丸めない）。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(window=window))

    with pytest.raises(UiStateFileError, match="window"):
        load_ui_state(path)


@pytest.mark.parametrize(
    "splitter",
    [
        {"player": 0, "playlist": 0},
        {"player": -1, "playlist": 10},
        {"player": True, "playlist": 10},
        {"player": 1.5, "playlist": 10},
        {"playlist": 10},
        "400/300",
    ],
)
def test_invalid_splitter_is_rejected(tmp_path: Path, splitter: object) -> None:
    """main_splitterの不正な既知値は復元失敗にする。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(main_splitter=splitter))

    with pytest.raises(UiStateFileError, match="main_splitter"):
        load_ui_state(path)


@pytest.mark.parametrize("directory", [123, True, ["C:\\Music"], "音楽", "..\\music"])
def test_invalid_last_open_directory_is_rejected(tmp_path: Path, directory: object) -> None:
    """型不正と相対パスは復元失敗にする。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(last_open_directory=directory))

    with pytest.raises(UiStateFileError, match="last_open_directory"):
        load_ui_state(path)


def test_empty_last_open_directory_is_treated_as_unsaved(tmp_path: Path) -> None:
    """空文字は「未保存」として扱う。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(last_open_directory=""))

    assert load_ui_state(path).last_open_directory is None


def test_missing_directory_is_kept_without_failing(tmp_path: Path) -> None:
    """存在しない前回フォルダーでも読み込み自体は失敗させない。

    外付け・ネットワークドライブが後で戻ることがあるため、存在確認は利用時に行う。
    """
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(last_open_directory="C:\\存在しない場所"))

    assert load_ui_state(path).last_open_directory == Path("C:\\存在しない場所")


@pytest.mark.parametrize("content", ["{壊れた", "[]", '"text"', "null"])
def test_malformed_or_unsupported_root_is_rejected(tmp_path: Path, content: str) -> None:
    """不正JSONと非objectルートを拒否する。"""
    path = tmp_path / "ui-state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(UiStateFileError):
        load_ui_state(path)


def test_non_utf8_is_rejected(tmp_path: Path) -> None:
    """UTF-8でないUI状態を拒否する。"""
    path = tmp_path / "ui-state.json"
    path.write_bytes(b"\x80\x81\xff")

    with pytest.raises(UiStateFileError, match="UTF-8"):
        load_ui_state(path)


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_atomic_save_keeps_existing_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """write/fsync/replace失敗でも既存ファイルを維持し一時ファイルを残さない。"""
    path = tmp_path / "ui-state.json"
    save_ui_state(path, UiState(window=WINDOW))
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(f"{failure}失敗")

    monkeypatch.setattr(f"sdp.services.ui_state.os.{failure}", fail)
    with pytest.raises(OSError):
        save_ui_state(path, UiState(main_splitter=SPLITTER))

    assert path.read_bytes() == original
    assert list(tmp_path.glob("ui-state.json.*.tmp")) == []


def test_failed_json_write_keeps_existing_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON書き込み失敗でも既存ファイルを維持し、一時ファイルを回収する。"""
    path = tmp_path / "ui-state.json"
    save_ui_state(path, UiState(window=WINDOW))
    original = path.read_bytes()

    def fail_dump(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("write失敗")

    monkeypatch.setattr("sdp.services.ui_state.json.dump", fail_dump)
    with pytest.raises(OSError):
        save_ui_state(path, UiState(main_splitter=SPLITTER))

    assert path.read_bytes() == original
    assert list(tmp_path.glob("ui-state.json.*.tmp")) == []


# -- 画面外補正 -------------------------------------------------------------


def test_window_inside_a_single_screen_is_kept() -> None:
    """画面内のgeometryはそのまま使う。"""
    assert fit_window_state(WINDOW, [FULL_HD]) == WINDOW


def test_window_on_a_left_monitor_keeps_negative_x() -> None:
    """左側モニターの負xを補正しない。"""
    state = WindowState(x=-1500, y=100, width=800, height=600, maximized=False)

    assert fit_window_state(state, [FULL_HD, LEFT_MONITOR]) == state


def test_window_on_a_top_monitor_keeps_negative_y() -> None:
    """上側モニターの負yを補正しない。"""
    state = WindowState(x=200, y=-900, width=800, height=600, maximized=False)

    assert fit_window_state(state, [FULL_HD, TOP_MONITOR]) == state


def test_partially_visible_window_is_kept() -> None:
    """タイトルバーが十分見えていれば、はみ出していても使う。"""
    state = WindowState(x=1800, y=0, width=800, height=600, maximized=False)

    assert fit_window_state(state, [FULL_HD]) == state


def test_completely_offscreen_window_moves_to_the_primary_center() -> None:
    """モニターを外した等で完全に画面外なら、primary screen中央へ戻す。"""
    state = WindowState(x=-4000, y=-3000, width=800, height=600, maximized=False)

    fitted = fit_window_state(state, [FULL_HD])

    assert fitted.width == 800
    assert fitted.height == 600
    assert fitted.x == (1920 - 800) // 2
    assert fitted.y == (1040 - 600) // 2


def test_window_below_the_screen_is_moved_back() -> None:
    """下端の外にあるウィンドウも掴めなくならないよう戻す。"""
    state = WindowState(x=100, y=2000, width=800, height=600, maximized=False)

    fitted = fit_window_state(state, [FULL_HD])

    assert fitted.y == (1040 - 600) // 2


def test_window_larger_than_the_screen_is_clamped() -> None:
    """画面より大きいサイズは所属screenのavailable size以内へ収める。"""
    state = WindowState(x=0, y=0, width=5000, height=4000, maximized=False)

    fitted = fit_window_state(state, [FULL_HD])

    assert fitted.width == FULL_HD.width
    assert fitted.height == FULL_HD.height


def test_oversized_window_on_smaller_secondary_is_clamped_to_secondary() -> None:
    """小さいsecondary上の過大Windowをprimaryではなくsecondary基準で縮める。"""
    primary = ScreenRect(x=0, y=0, width=3840, height=2160)
    secondary = ScreenRect(x=3840, y=0, width=1280, height=720)
    state = WindowState(x=4000, y=100, width=1800, height=1000, maximized=False)

    fitted = fit_window_state(state, [primary, secondary])

    assert (fitted.x, fitted.y) == (secondary.x, secondary.y)
    assert (fitted.width, fitted.height) == (secondary.width, secondary.height)
    assert fitted.right <= secondary.right
    assert fitted.bottom <= secondary.bottom


def test_large_window_on_larger_secondary_is_not_clamped_to_primary() -> None:
    """大きいsecondaryで有効なサイズを、小さいprimary基準で縮めない。"""
    primary = ScreenRect(x=0, y=0, width=1280, height=720)
    secondary = ScreenRect(x=1280, y=0, width=2560, height=1440)
    state = WindowState(x=1400, y=100, width=1800, height=1000, maximized=False)

    assert fit_window_state(state, [primary, secondary]) == state


@pytest.mark.parametrize(
    ("secondary", "state"),
    [
        (
            ScreenRect(x=-1280, y=0, width=1280, height=720),
            WindowState(x=-1280, y=0, width=1800, height=900, maximized=False),
        ),
        (
            ScreenRect(x=0, y=-720, width=1280, height=720),
            WindowState(x=0, y=-720, width=1800, height=900, maximized=False),
        ),
    ],
)
def test_oversized_window_uses_negative_coordinate_secondary(
    secondary: ScreenRect, state: WindowState
) -> None:
    """左側・上側の負座標monitorも所属画面として選択する。"""
    primary = ScreenRect(x=0, y=0, width=1920, height=1080)

    fitted = fit_window_state(state, [primary, secondary])

    assert (fitted.x, fitted.y) == (secondary.x, secondary.y)
    assert (fitted.width, fitted.height) == (secondary.width, secondary.height)


def test_screen_with_largest_title_overlap_is_selected() -> None:
    """複数画面に跨る場合はタイトル帯の重なりが最大の画面を選ぶ。"""
    primary = ScreenRect(x=0, y=0, width=1000, height=800)
    secondary = ScreenRect(x=1000, y=0, width=2000, height=1200)
    state = WindowState(x=200, y=100, width=1900, height=900, maximized=False)

    fitted = fit_window_state(state, [primary, secondary])

    assert (fitted.width, fitted.height) == (state.width, state.height)


def test_window_body_overlap_selects_screen_when_title_is_above_screens() -> None:
    """タイトル帯が外でも本体が重なる画面を選び、掴める位置へ補正する。"""
    state = WindowState(x=200, y=-100, width=800, height=600, maximized=False)

    fitted = fit_window_state(state, [FULL_HD])

    assert fitted.x == state.x
    assert fitted.y == FULL_HD.y


def test_minimum_size_is_respected() -> None:
    """最小サイズを下回らない。"""
    state = WindowState(x=10, y=10, width=10, height=10, maximized=False)

    fitted = fit_window_state(state, [FULL_HD], minimum_size=(400, 300))

    assert fitted.width == 400
    assert fitted.height == 300


def test_maximized_flag_is_preserved_by_the_fit() -> None:
    """補正しても最大化フラグは保つ（normal geometryだけを直す）。"""
    state = WindowState(x=-9000, y=-9000, width=800, height=600, maximized=True)

    assert fit_window_state(state, [FULL_HD]).maximized is True


def test_no_screen_information_keeps_the_position() -> None:
    """画面情報が取れない場合は位置へ触れず、最小サイズだけ保証する。"""
    state = WindowState(x=-500, y=-500, width=100, height=100, maximized=False)

    fitted = fit_window_state(state, [], minimum_size=(300, 200))

    assert (fitted.x, fitted.y) == (-500, -500)
    assert (fitted.width, fitted.height) == (300, 200)


def test_extreme_coordinates_are_moved_into_the_primary_screen() -> None:
    """極端な座標でも画面内へ戻す。"""
    state = WindowState(x=2_000_000, y=2_000_000, width=800, height=600, maximized=False)

    fitted = fit_window_state(state, [FULL_HD, LEFT_MONITOR])

    assert FULL_HD.x <= fitted.x <= FULL_HD.right
    assert FULL_HD.y <= fitted.y <= FULL_HD.bottom


def test_secondary_monitor_removal_falls_back_to_primary() -> None:
    """secondary monitorを外した構成では、そこにあったウィンドウを戻す。"""
    state = WindowState(x=-1700, y=200, width=800, height=600, maximized=False)

    on_both = fit_window_state(state, [FULL_HD, LEFT_MONITOR])
    on_primary_only = fit_window_state(state, [FULL_HD])

    assert on_both == state
    assert on_primary_only.x >= FULL_HD.x


# -- Splitter比率 -----------------------------------------------------------


def test_splitter_sizes_are_redistributed_by_ratio() -> None:
    """保存値は比率として現在の高さへ再配分する。"""
    state = SplitterState(player_size=400, playlist_size=600)

    assert distribute_splitter_sizes(state, 500) == (200, 300)


def test_splitter_redistribution_keeps_the_total() -> None:
    """再配分後の合計は現在の利用可能高さに一致する。"""
    player, playlist = distribute_splitter_sizes(SPLITTER, 777)

    assert player + playlist == 777


def test_splitter_never_collapses_one_side() -> None:
    """片側が完全に潰れないよう最低値を確保する。"""
    state = SplitterState(player_size=1000, playlist_size=1)

    player, playlist = distribute_splitter_sizes(state, 1000)

    assert playlist >= MINIMUM_SPLITTER_SIZE
    assert player + playlist == 1000


def test_splitter_minimum_shrinks_with_a_small_total() -> None:
    """総量が小さい場合は最低値を総量の半分までに抑える。"""
    state = SplitterState(player_size=100, playlist_size=1)

    player, playlist = distribute_splitter_sizes(state, 40)

    assert player + playlist == 40
    assert playlist >= 20


def test_splitter_without_a_total_keeps_the_saved_sizes() -> None:
    """レイアウト未確定（総量0）では保存値をそのまま返す。"""
    assert distribute_splitter_sizes(SPLITTER, 0) == (400, 300)


# -- schema version 2（現在曲のentry_id）-------------------------------------


def test_version_two_round_trips_the_current_entry(tmp_path: Path) -> None:
    """現在曲のentry_idを往復する（行番号でもパスでもない）。"""
    path = tmp_path / "ui-state.json"
    expected = UiState(window=WINDOW, current_playlist_entry_id="entry-1234")

    save_ui_state(path, expected)

    assert load_ui_state(path) == expected
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["current_playlist_entry_id"] == "entry-1234"


def test_version_one_has_no_current_entry(tmp_path: Path) -> None:
    """v1には現在曲が無いためNoneで補う。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of())

    assert load_ui_state(path).current_playlist_entry_id is None


def test_version_one_ignores_the_version_two_key(tmp_path: Path) -> None:
    """v1にv2のキーが混入していても未知キーとして無視する。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(current_playlist_entry_id="entry-1234"))

    assert load_ui_state(path).current_playlist_entry_id is None


def test_missing_current_entry_is_none(tmp_path: Path) -> None:
    """v2でキーが無ければ「未保存」として扱う。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(schema_version=2))

    assert load_ui_state(path).current_playlist_entry_id is None


def test_empty_current_entry_is_normalized_to_none(tmp_path: Path) -> None:
    """空文字は None へ統一する（空IDで照合しない）。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(schema_version=2, current_playlist_entry_id=""))

    assert load_ui_state(path).current_playlist_entry_id is None


@pytest.mark.parametrize("value", [123, True, ["a"], {"id": "a"}, 1.5])
def test_non_string_current_entry_is_rejected(tmp_path: Path, value: object) -> None:
    """文字列以外のentry_idは復元失敗にする。"""
    path = tmp_path / "ui-state.json"
    write_document(path, document_of(schema_version=2, current_playlist_entry_id=value))

    with pytest.raises(UiStateFileError, match="current_playlist_entry_id"):
        load_ui_state(path)


def test_unicode_current_entry_round_trips(tmp_path: Path) -> None:
    """日本語・記号を含むentry_idも保持できる。"""
    path = tmp_path / "ui-state.json"
    expected = UiState(current_playlist_entry_id="曲 001-テスト")

    save_ui_state(path, expected)

    assert load_ui_state(path).current_playlist_entry_id == "曲 001-テスト"


def test_current_entry_must_not_be_empty_in_the_value_object() -> None:
    """値オブジェクトでも空文字を受け付けない。"""
    with pytest.raises(ValueError, match="current_playlist_entry_id"):
        UiState(current_playlist_entry_id="")


def test_playback_position_is_never_saved(tmp_path: Path) -> None:
    """再生位置・再生状態・選択行はui-state.jsonへ保存しない。

    数秒の曲やSEでは復元価値が低く、突然の再開は予測しにくい。まずは現在曲の
    選択復元だけでUXを評価する（[architecture.md](../../../docs/architecture.md) §9.6）。
    """
    path = tmp_path / "ui-state.json"

    save_ui_state(
        path,
        UiState(
            window=WINDOW,
            main_splitter=SPLITTER,
            last_open_directory=Path("C:\\Music"),
            current_playlist_entry_id="entry-1",
        ),
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "window",
        "main_splitter",
        "last_open_directory",
        "current_playlist_entry_id",
    }
    for forbidden in ("position_ms", "position", "playing", "state", "selected_row", "volume"):
        assert forbidden not in document

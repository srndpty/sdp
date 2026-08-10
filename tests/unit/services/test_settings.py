"""settings.jsonのQt非依存なschema・検証・移行・アトミック保存を検証する。"""

import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from sdp.core.playlist.types import RepeatMode
from sdp.services.settings import (
    SETTINGS_SCHEMA_VERSION,
    AppSettings,
    RepeatModeSetting,
    SettingsFileError,
    load_settings,
    save_settings,
    validate_settings,
)

DEFAULTS = AppSettings(playback_rate=1.0, pitch_compensation=True)


def write_document(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_app_settings_is_immutable_and_holds_playback_and_visibility() -> None:
    """保存対象は再生設定と可視化の表示ON/OFFだけの不変値。"""
    with pytest.raises(FrozenInstanceError):
        DEFAULTS.playback_rate = 1.5  # type: ignore[misc]
    assert AppSettings.__match_args__ == (
        "playback_rate",
        "pitch_compensation",
        "waveform_visible",
        "spectrum_visible",
        "level_meter_visible",
        "oscilloscope_visible",
        "vectorscope_visible",
        "correlation_meter_visible",
        "spectrogram_visible",
        "chromagram_visible",
        "volume",
        "muted",
        "repeat_mode",
        "shuffle_enabled",
    )


def test_visualization_defaults_to_visible() -> None:
    """可視化の既定はすべて表示ON（旧versionからの補完値と一致する）。"""
    settings = AppSettings(playback_rate=1.0, pitch_compensation=True)

    assert settings.waveform_visible is True
    assert settings.spectrum_visible is True
    assert settings.level_meter_visible is True
    assert settings.oscilloscope_visible is True
    assert settings.vectorscope_visible is True
    assert settings.correlation_meter_visible is True
    assert settings.spectrogram_visible is True
    assert settings.chromagram_visible is True


def test_playback_state_defaults() -> None:
    """音量1.0・ミュートOFF・Repeat OFF・ShuffleOFFが既定（旧versionの補完値）。"""
    settings = AppSettings(playback_rate=1.0, pitch_compensation=True)

    assert settings.volume == pytest.approx(1.0)
    assert settings.muted is False
    assert settings.repeat_mode is RepeatModeSetting.OFF
    assert settings.shuffle_enabled is False


def test_missing_file_returns_controller_defaults(tmp_path: Path) -> None:
    """未作成は初回起動としてController由来の既定値を返す。"""
    assert load_settings(tmp_path / "settings.json", DEFAULTS) is DEFAULTS


def test_round_trip_uses_utf8_and_only_settings_fields(tmp_path: Path) -> None:
    """正常往復し、playlist等の情報を混入させない。"""
    path = tmp_path / "日本語" / "settings.json"
    expected = AppSettings(playback_rate=1.25, pitch_compensation=False)

    save_settings(path, expected)

    assert load_settings(path, DEFAULTS) == expected
    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "playback_rate",
        "pitch_compensation",
        "waveform_visible",
        "spectrum_visible",
        "level_meter_visible",
        "oscilloscope_visible",
        "vectorscope_visible",
        "correlation_meter_visible",
        "spectrogram_visible",
        "chromagram_visible",
        "volume",
        "muted",
        "repeat_mode",
        "shuffle_enabled",
    }
    assert document["schema_version"] == SETTINGS_SCHEMA_VERSION == 4


def test_integer_rate_is_restored_as_float(tmp_path: Path) -> None:
    """JSON整数の有効速度も公開型ではfloatへ揃える。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": 2, "pitch_compensation": True},
    )
    assert load_settings(path, DEFAULTS).playback_rate == 2.0
    assert isinstance(load_settings(path, DEFAULTS).playback_rate, float)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """将来の未知キーは既知schemaの解釈を妨げない。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {
            "schema_version": 1,
            "playback_rate": 1.5,
            "pitch_compensation": False,
            "future": {"未知": True},
        },
    )
    assert load_settings(path, DEFAULTS) == AppSettings(1.5, False)


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"schema_version": 1, "pitch_compensation": False}, AppSettings(1.0, False)),
        ({"schema_version": 1, "playback_rate": 1.25}, AppSettings(1.25, True)),
    ],
)
def test_missing_known_keys_use_defaults(
    tmp_path: Path, document: dict[str, object], expected: AppSettings
) -> None:
    """既知キーの欠落は起動時Controller値から補完する。"""
    path = tmp_path / "settings.json"
    write_document(path, document)
    assert load_settings(path, DEFAULTS) == expected


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 0, 5, 99])
def test_schema_version_requires_a_supported_integer(tmp_path: Path, version: object) -> None:
    """versionはbool・float・文字列・欠落・未知値を拒否する（1〜4だけ許可）。"""
    path = tmp_path / "settings.json"
    document: dict[str, object] = {
        "playback_rate": 1.0,
        "pitch_compensation": True,
    }
    if version is not None:
        document["schema_version"] = version
    write_document(path, document)
    with pytest.raises(SettingsFileError, match="schema_version"):
        load_settings(path, DEFAULTS)


# -- schema version 1 → 2 の移行 --------------------------------------------


def test_version_one_fills_visualization_defaults(tmp_path: Path) -> None:
    """可視化設定を持たないversion 1は、表示ONで補って読み込める。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": 1.25, "pitch_compensation": False},
    )

    settings = load_settings(path, DEFAULTS)

    assert settings == AppSettings(
        playback_rate=1.25,
        pitch_compensation=False,
        waveform_visible=True,
        spectrum_visible=True,
        level_meter_visible=True,
    )


@pytest.mark.parametrize("value", [False, 1])
def test_version_one_ignores_version_two_visibility_keys(tmp_path: Path, value: object) -> None:
    """v1ではv2キーを未知キーとして無視し、値の型にも影響されない。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {
            "schema_version": 1,
            "playback_rate": 1.0,
            "pitch_compensation": True,
            "waveform_visible": value,
        },
    )

    assert load_settings(path, DEFAULTS).waveform_visible is True


def test_loading_version_one_does_not_rewrite_the_file(tmp_path: Path) -> None:
    """読み込みだけではversion 1のファイルを書き換えない。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": 1.5, "pitch_compensation": True},
    )
    original = path.read_bytes()

    load_settings(path, DEFAULTS)
    load_settings(path, DEFAULTS)

    assert path.read_bytes() == original


def test_saving_after_a_version_one_load_writes_the_current_version(tmp_path: Path) -> None:
    """変更後の保存は現在のversionになり、可視化設定も含まれる。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": 1.5, "pitch_compensation": True},
    )
    restored = load_settings(path, DEFAULTS)

    save_settings(path, AppSettings(restored.playback_rate, restored.pitch_compensation, False))

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == SETTINGS_SCHEMA_VERSION
    assert document["waveform_visible"] is False
    assert document["spectrum_visible"] is True
    assert load_settings(path, DEFAULTS).waveform_visible is False


def test_version_two_round_trip_keeps_every_visualization_flag(tmp_path: Path) -> None:
    """version 2は3つの表示設定を独立に往復できる。"""
    path = tmp_path / "settings.json"
    expected = AppSettings(
        playback_rate=0.75,
        pitch_compensation=False,
        waveform_visible=False,
        spectrum_visible=True,
        level_meter_visible=False,
    )

    save_settings(path, expected)

    assert load_settings(path, DEFAULTS) == expected


@pytest.mark.parametrize("name", ["waveform_visible", "spectrum_visible", "level_meter_visible"])
@pytest.mark.parametrize("value", [0, 1, "true", None, [], 1.0])
def test_visualization_flags_require_exact_bool(tmp_path: Path, name: str, value: object) -> None:
    """表示設定も0／1／文字列を受理せず、厳密なboolだけを許可する。"""
    path = tmp_path / "settings.json"
    document: dict[str, object] = {
        "schema_version": 2,
        "playback_rate": 1.0,
        "pitch_compensation": True,
        name: value,
    }
    write_document(path, document)

    with pytest.raises(SettingsFileError, match=name):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize("name", ["waveform_visible", "spectrum_visible", "level_meter_visible"])
def test_missing_visualization_flags_use_defaults(tmp_path: Path, name: str) -> None:
    """version 2で個別キーが欠落しても、既定値で補う（失敗にしない）。"""
    path = tmp_path / "settings.json"
    document: dict[str, object] = {
        "schema_version": 2,
        "playback_rate": 1.0,
        "pitch_compensation": True,
        "waveform_visible": False,
        "spectrum_visible": False,
        "level_meter_visible": False,
    }
    del document[name]
    write_document(path, document)

    settings = load_settings(path, DEFAULTS)

    assert getattr(settings, name) is True


def test_validate_settings_rejects_non_bool_visibility() -> None:
    """適用前検証でも表示設定のboolを厳密に扱う。"""
    validate_settings(DEFAULTS)

    with pytest.raises(ValueError, match="spectrum_visible"):
        validate_settings(AppSettings(1.0, True, True, 1, True))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rate",
    [True, "1.0", None, math.nan, math.inf, -math.inf, 0.49, 2.01],
)
def test_invalid_playback_rate_is_rejected(tmp_path: Path, rate: object) -> None:
    """型・有限性・UI復元範囲を満たさない速度をclampせず拒否する。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": rate, "pitch_compensation": True},
    )
    with pytest.raises(SettingsFileError, match="playback_rate"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize("pitch", [0, 1, "true", None])
def test_pitch_requires_exact_bool(tmp_path: Path, pitch: object) -> None:
    """pitchは真偽相当値でなく厳密なboolだけを受理する。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": 1, "playback_rate": 1.0, "pitch_compensation": pitch},
    )
    with pytest.raises(SettingsFileError, match="pitch_compensation"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize("content", ["{壊れた", "[]", '"text"', "null"])
def test_malformed_or_unsupported_root_is_rejected(tmp_path: Path, content: str) -> None:
    """不正JSONと非objectルートを拒否する。"""
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SettingsFileError):
        load_settings(path, DEFAULTS)


def test_non_utf8_is_rejected(tmp_path: Path) -> None:
    """UTF-8でない設定を拒否する。"""
    path = tmp_path / "settings.json"
    path.write_bytes(b"\x80\x81\xff")
    with pytest.raises(SettingsFileError, match="UTF-8"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize(
    "settings",
    [
        AppSettings(math.nan, True),
        AppSettings(math.inf, True),
        AppSettings(0.49, True),
        AppSettings(2.01, True),
        AppSettings(1.0, 1),  # type: ignore[arg-type]
    ],
)
def test_invalid_save_has_no_filesystem_side_effect(tmp_path: Path, settings: AppSettings) -> None:
    """保存前検証失敗では親ディレクトリも一時ファイルも作らない。"""
    path = tmp_path / "new" / "settings.json"
    with pytest.raises(ValueError):
        save_settings(path, settings)
    assert not path.parent.exists()


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failed_atomic_save_keeps_existing_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """write/fsync/replace失敗でも既存ファイルを維持し一時ファイルを残さない。"""
    path = tmp_path / "settings.json"
    save_settings(path, DEFAULTS)
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(f"{failure}失敗")

    monkeypatch.setattr(f"sdp.services.settings.os.{failure}", fail)
    with pytest.raises(OSError):
        save_settings(path, AppSettings(1.5, False))

    assert path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.*.tmp")) == []


def test_failed_json_write_keeps_existing_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON書き込み失敗でも既存ファイルを維持し、一時ファイルを回収する。"""
    path = tmp_path / "settings.json"
    save_settings(path, DEFAULTS)
    original = path.read_bytes()

    def fail_dump(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("write失敗")

    monkeypatch.setattr("sdp.services.settings.json.dump", fail_dump)
    with pytest.raises(OSError):
        save_settings(path, AppSettings(1.5, False))

    assert path.read_bytes() == original
    assert list(tmp_path.glob("settings.json.*.tmp")) == []


# -- schema version 3（音量・ミュート・Repeat・Shuffle）----------------------


def v3_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 3,
        "playback_rate": 1.25,
        "pitch_compensation": False,
        "waveform_visible": False,
        "spectrum_visible": True,
        "level_meter_visible": False,
        "volume": 0.4,
        "muted": True,
        "repeat_mode": "all",
        "shuffle_enabled": True,
    }
    document.update(overrides)
    return document


def test_version_three_round_trip(tmp_path: Path) -> None:
    """v3は再生設定と可視化設定をすべて往復する。"""
    path = tmp_path / "settings.json"
    expected = AppSettings(
        playback_rate=0.75,
        pitch_compensation=False,
        waveform_visible=False,
        spectrum_visible=True,
        level_meter_visible=False,
        volume=0.25,
        muted=True,
        repeat_mode=RepeatModeSetting.ONE,
        shuffle_enabled=True,
    )

    save_settings(path, expected)

    assert load_settings(path, DEFAULTS) == expected


def test_version_three_is_read_from_the_document(tmp_path: Path) -> None:
    """v3の各キーが読み込まれる。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document())

    settings = load_settings(path, DEFAULTS)

    assert settings.volume == pytest.approx(0.4)
    assert settings.muted is True
    assert settings.repeat_mode is RepeatModeSetting.ALL
    assert settings.shuffle_enabled is True


@pytest.mark.parametrize("version", [1, 2])
def test_older_versions_use_playback_state_defaults(tmp_path: Path, version: int) -> None:
    """v1／v2には再生設定が無いため既定値で補う。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {"schema_version": version, "playback_rate": 1.5, "pitch_compensation": True},
    )

    settings = load_settings(path, DEFAULTS)

    assert settings.volume == pytest.approx(DEFAULTS.volume)
    assert settings.muted is DEFAULTS.muted
    assert settings.repeat_mode is DEFAULTS.repeat_mode
    assert settings.shuffle_enabled is DEFAULTS.shuffle_enabled


@pytest.mark.parametrize("version", [1, 2])
def test_older_versions_ignore_version_three_keys(tmp_path: Path, version: int) -> None:
    """v1／v2にv3のキーが混入していても未知キーとして無視する。"""
    path = tmp_path / "settings.json"
    write_document(
        path,
        {
            "schema_version": version,
            "playback_rate": 1.0,
            "pitch_compensation": True,
            "volume": 0.1,
            "muted": True,
            "repeat_mode": "one",
            "shuffle_enabled": True,
        },
    )

    settings = load_settings(path, DEFAULTS)

    assert settings.volume == pytest.approx(DEFAULTS.volume)
    assert settings.muted is False
    assert settings.repeat_mode is RepeatModeSetting.OFF
    assert settings.shuffle_enabled is False


@pytest.mark.parametrize("name", ["volume", "muted", "repeat_mode", "shuffle_enabled"])
def test_missing_version_three_keys_use_defaults(tmp_path: Path, name: str) -> None:
    """v3で個別キーが欠落しても既定値で補う（失敗にしない）。"""
    path = tmp_path / "settings.json"
    document = v3_document()
    del document[name]
    write_document(path, document)

    settings = load_settings(path, DEFAULTS)

    assert getattr(settings, name) == getattr(DEFAULTS, name)


@pytest.mark.parametrize("volume", [0.0, 0.5, 1.0])
def test_volume_boundaries_are_accepted(tmp_path: Path, volume: float) -> None:
    """0.0〜1.0はそのまま復元する。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(volume=volume))

    assert load_settings(path, DEFAULTS).volume == pytest.approx(volume)


@pytest.mark.parametrize(
    "volume", [True, False, "0.5", None, math.nan, math.inf, -math.inf, -0.01, 1.01, 2]
)
def test_invalid_volume_is_rejected(tmp_path: Path, volume: object) -> None:
    """bool・文字列・非有限・範囲外の音量をclampせず拒否する。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(volume=volume))

    with pytest.raises(SettingsFileError, match="volume"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize("muted", [0, 1, "true", None])
def test_muted_requires_exact_bool(tmp_path: Path, muted: object) -> None:
    """mutedは0／1／文字列を受理しない。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(muted=muted))

    with pytest.raises(SettingsFileError, match="muted"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize("shuffle", [0, 1, "true", None])
def test_shuffle_requires_exact_bool(tmp_path: Path, shuffle: object) -> None:
    """shuffle_enabledも厳密なboolだけを受理する。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(shuffle_enabled=shuffle))

    with pytest.raises(SettingsFileError, match="shuffle_enabled"):
        load_settings(path, DEFAULTS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("off", RepeatModeSetting.OFF),
        ("all", RepeatModeSetting.ALL),
        ("one", RepeatModeSetting.ONE),
    ],
)
def test_every_repeat_mode_round_trips(
    tmp_path: Path, text: str, expected: RepeatModeSetting
) -> None:
    """Repeatの3値すべてを安定した文字列で往復する。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(repeat_mode=text))

    settings = load_settings(path, DEFAULTS)
    assert settings.repeat_mode is expected

    save_settings(path, settings)
    assert json.loads(path.read_text(encoding="utf-8"))["repeat_mode"] == text


@pytest.mark.parametrize("mode", ["OFF", "loop", "", None, 0, True, ["off"]])
def test_unknown_repeat_mode_is_rejected(tmp_path: Path, mode: object) -> None:
    """未知のrepeat_modeは既定値へ丸めず復元失敗にする。"""
    path = tmp_path / "settings.json"
    write_document(path, v3_document(repeat_mode=mode))

    with pytest.raises(SettingsFileError, match="repeat_mode"):
        load_settings(path, DEFAULTS)


def test_repeat_mode_setting_maps_to_the_core_enum() -> None:
    """core enumと保存表現が1対1で対応する（暗黙の既定値へ落とさない）。"""
    for mode in RepeatMode:
        setting = RepeatModeSetting.from_repeat_mode(mode)
        assert setting.to_repeat_mode() is mode
    assert {setting.value for setting in RepeatModeSetting} == {"off", "all", "one"}


def test_validate_rejects_invalid_playback_state() -> None:
    """適用前検証でも音量・bool・Repeatの型を拒否する。"""
    with pytest.raises(ValueError, match="volume"):
        validate_settings(replace(DEFAULTS, volume=1.5))
    with pytest.raises(ValueError, match="muted"):
        validate_settings(replace(DEFAULTS, muted=1))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="shuffle_enabled"):
        validate_settings(replace(DEFAULTS, shuffle_enabled="true"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="repeat_mode"):
        validate_settings(replace(DEFAULTS, repeat_mode="off"))  # type: ignore[arg-type]


def test_position_is_never_saved(tmp_path: Path) -> None:
    """再生位置・再生中かどうかはsettings.jsonへ保存しない。"""
    path = tmp_path / "settings.json"

    save_settings(path, DEFAULTS)

    document = json.loads(path.read_text(encoding="utf-8"))
    for forbidden in ("position_ms", "position", "playing", "state", "current_entry_id"):
        assert forbidden not in document

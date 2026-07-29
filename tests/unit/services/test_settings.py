"""settings.jsonのQt非依存なschema・検証・アトミック保存を検証する。"""

import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sdp.services.settings import (
    SETTINGS_SCHEMA_VERSION,
    AppSettings,
    SettingsFileError,
    load_settings,
    save_settings,
)

DEFAULTS = AppSettings(playback_rate=1.0, pitch_compensation=True)


def write_document(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_app_settings_is_immutable_and_has_only_two_fields() -> None:
    """保存対象は速度とピッチ補正だけの不変値。"""
    with pytest.raises(FrozenInstanceError):
        DEFAULTS.playback_rate = 1.5  # type: ignore[misc]
    assert AppSettings.__match_args__ == ("playback_rate", "pitch_compensation")


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
    assert set(document) == {"schema_version", "playback_rate", "pitch_compensation"}
    assert document["schema_version"] == SETTINGS_SCHEMA_VERSION


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


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_schema_version_requires_exact_integer_one(tmp_path: Path, version: object) -> None:
    """versionはbool・float・文字列・欠落・未知値を拒否する。"""
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

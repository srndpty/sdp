"""AppSettingsControllerの適用調停と、SettingsSessionの復元・デバウンス・障害分離を検証する。"""

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.services.settings import (
    RESTORE_FAILED_MESSAGE,
    AppSettings,
    AppSettingsController,
    SettingsSession,
    load_settings,
    save_settings,
)

DEBOUNCE_MS = 20


@pytest.fixture
def backend(qtbot: QtBot) -> Iterator[FakePlaybackBackend]:
    del qtbot
    yield FakePlaybackBackend(playback_rate=1.0, pitch_compensation=True)


@pytest.fixture
def controller(backend: FakePlaybackBackend) -> Iterator[PlaybackController]:
    yield PlaybackController(backend)


def make_session(path: Path, controller: PlaybackController) -> SettingsSession:
    """調停サービスごと組み立てる（sessionが調停サービスの寿命を保持する）。"""
    return SettingsSession(
        path,
        AppSettingsController(controller),
        debounce_ms=DEBOUNCE_MS,
        retry_ms=DEBOUNCE_MS,
    )


def make_app_settings(controller: PlaybackController) -> AppSettingsController:
    return AppSettingsController(controller)


def recording_save(saved: list[AppSettings]) -> Callable[[Path, AppSettings], None]:
    """保存内容だけを記録する型付きテストダブルを作る。"""

    def record(path: Path, settings: AppSettings) -> None:
        del path
        saved.append(settings)

    return record


def test_load_applies_settings_before_ui_construction(
    tmp_path: Path, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """loadだけでControllerへ適用でき、監視timerは開始しない。"""
    path = tmp_path / "settings.json"
    save_settings(path, AppSettings(1.25, False))
    session = make_session(path, controller)

    assert session.load() is None

    assert controller.playback_rate == 1.25
    assert controller.pitch_compensation is False
    assert backend.call_args("set_playback_rate") == [(1.25,)]
    assert backend.call_args("set_pitch_compensation") == [(False,)]
    assert not session.is_running


def test_missing_file_keeps_backend_defaults_and_saving_enabled(
    tmp_path: Path, controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """未作成時は既定値を維持し、エラーなく保存可能。"""
    session = make_session(tmp_path / "settings.json", controller)
    assert session.load() is None
    assert backend.call_names() == []
    assert session.is_save_enabled


def test_start_is_idempotent_and_continuous_changes_save_once(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """連続するrate/pitch変更はtimerを再開始し最後のsnapshotだけ1回保存する。"""
    saved: list[AppSettings] = []
    monkeypatch.setattr(
        "sdp.services.settings.save_settings",
        recording_save(saved),
    )
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    session.start()
    session.start()

    controller.set_playback_rate(1.05)
    controller.set_playback_rate(1.10)
    controller.set_playback_rate(1.15)
    controller.set_pitch_compensation(False)

    qtbot.waitUntil(lambda: len(saved) == 1, timeout=2_000)
    assert saved == [AppSettings(1.15, False)]
    session.stop()


def test_flush_saves_immediately_stops_timer_and_skips_same_snapshot(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flushは待機中変更を即時保存し、同じsnapshotを再保存しない。"""
    saved: list[AppSettings] = []
    monkeypatch.setattr(
        "sdp.services.settings.save_settings",
        recording_save(saved),
    )
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    session.start()
    controller.set_playback_rate(1.5)
    assert session._timer.isActive()  # pyright: ignore[reportPrivateUsage]

    assert session.flush() is True
    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]
    assert session.flush() is False
    assert saved == [AppSettings(1.5, True)]
    session.stop()


def test_stop_is_idempotent_and_prevents_new_saves(
    tmp_path: Path, controller: PlaybackController
) -> None:
    """stop後は変更Signalを監視せず、複数回呼んでも問題ない。"""
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    session.start()
    session.stop()
    session.stop()
    controller.set_playback_rate(1.25)
    assert not session.is_running
    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("content", ["{壊れた", "[]"])
def test_load_failure_disables_saving_and_keeps_original(
    tmp_path: Path,
    controller: PlaybackController,
    content: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """復元失敗後は既定値で動き、既存ファイルを自動・flush保存で上書きしない。"""
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    original = path.read_bytes()
    session = make_session(path, controller)

    with caplog.at_level(logging.ERROR):
        message = session.load()
    session.start()
    controller.set_playback_rate(1.25)

    assert message == RESTORE_FAILED_MESSAGE
    assert not session.is_save_enabled
    assert session.flush() is False
    assert path.read_bytes() == original
    assert "復元に失敗" in caplog.text
    session.stop()


def test_save_failure_is_logged_and_can_be_retried(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """保存失敗を外へ投げずログへ残し、snapshotを保存済み扱いしない。"""
    calls: list[AppSettings] = []

    def failing_save(path: Path, settings: AppSettings) -> None:
        del path
        calls.append(settings)
        raise OSError("保存失敗")

    monkeypatch.setattr("sdp.services.settings.save_settings", failing_save)
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    controller.set_playback_rate(1.25)

    with caplog.at_level(logging.ERROR):
        assert session.flush() is False
        assert session.flush() is False

    assert calls == [AppSettings(1.25, True), AppSettings(1.25, True)]
    assert "保存に失敗" in caplog.text


def test_debounce_save_failure_retries_automatically_once(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一時的なデバウンス保存失敗は長めのタイマーで1回自動再試行する。"""
    calls: list[AppSettings] = []
    saved: list[AppSettings] = []

    def flaky_save(path: Path, settings: AppSettings) -> None:
        del path
        calls.append(settings)
        if len(calls) == 1:
            raise OSError("一時的な共有違反")
        saved.append(settings)

    monkeypatch.setattr("sdp.services.settings.save_settings", flaky_save)
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    session.start()

    with caplog.at_level(logging.INFO):
        controller.set_playback_rate(1.25)
        qtbot.waitUntil(lambda: len(saved) == 1, timeout=2_000)

    assert calls == [AppSettings(1.25, True), AppSettings(1.25, True)]
    assert saved == [AppSettings(1.25, True)]
    assert "再試行" in caplog.text
    session.stop()


def test_real_debounce_writes_round_trip(
    tmp_path: Path, controller: PlaybackController, qtbot: QtBot
) -> None:
    """実ファイルにも最後の値がデバウンス後に往復する。"""
    path = tmp_path / "settings.json"
    session = make_session(path, controller)
    session.load()
    session.start()
    controller.set_playback_rate(1.35)
    controller.set_pitch_compensation(False)

    qtbot.waitUntil(path.is_file, timeout=2_000)

    assert load_settings(path, AppSettings(1.0, True)) == AppSettings(1.35, False)
    session.stop()


def test_deleted_session_cancels_pending_timer(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    """QObject削除で待機timerも無効化され、遅延保存しない。"""
    saved: list[AppSettings] = []
    monkeypatch.setattr(
        "sdp.services.settings.save_settings",
        recording_save(saved),
    )
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    session.start()
    controller.set_playback_rate(1.25)
    timer = session._timer  # pyright: ignore[reportPrivateUsage]
    session.deleteLater()
    qtbot.waitUntil(lambda: not isValid(session))

    assert not isValid(timer)
    assert saved == []


# -- AppSettingsController（適用の調停）-------------------------------------


def test_initial_snapshot_comes_from_the_controller(controller: PlaybackController) -> None:
    """初期snapshotはControllerの現在値と、可視化の既定（すべて表示）。"""
    app_settings = make_app_settings(controller)

    assert app_settings.settings == AppSettings(
        playback_rate=controller.playback_rate,
        pitch_compensation=controller.pitch_compensation,
        waveform_visible=True,
        spectrum_visible=True,
        level_meter_visible=True,
    )


def test_apply_updates_the_controller_and_notifies_once(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """applyは差分のある項目だけControllerへ適用し、1回だけ通知する。"""
    app_settings = make_app_settings(controller)
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    app_settings.apply(AppSettings(1.25, False, waveform_visible=False))

    assert controller.playback_rate == pytest.approx(1.25)
    assert controller.pitch_compensation is False
    assert backend.call_args("set_playback_rate") == [(1.25,)]
    assert notified == [app_settings.settings]
    assert app_settings.settings.waveform_visible is False


def test_apply_of_the_same_settings_does_not_notify(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """同値のapplyでは通知もController操作も行わない。"""
    app_settings = make_app_settings(controller)
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    app_settings.apply(app_settings.settings)

    assert notified == []
    assert backend.call_names() == []


def test_visualization_only_change_does_not_touch_the_controller(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """表示ON/OFFだけの変更でControllerのsetterを呼ばない。"""
    app_settings = make_app_settings(controller)

    app_settings.apply(
        AppSettings(
            controller.playback_rate,
            controller.pitch_compensation,
            spectrum_visible=False,
            level_meter_visible=False,
        )
    )

    assert backend.call_names() == []
    assert app_settings.settings.spectrum_visible is False
    assert app_settings.settings.level_meter_visible is False


def test_controller_changes_are_mirrored_into_the_snapshot(
    controller: PlaybackController,
) -> None:
    """SpeedPanelやショートカット経由の変更もsnapshotへ追従する。"""
    app_settings = make_app_settings(controller)
    app_settings.apply(AppSettings(1.0, True, waveform_visible=False))
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    controller.set_playback_rate(1.5)
    controller.set_pitch_compensation(False)

    assert app_settings.settings.playback_rate == pytest.approx(1.5)
    assert app_settings.settings.pitch_compensation is False
    # 可視化設定は再生操作で失われない。
    assert app_settings.settings.waveform_visible is False
    assert len(notified) == 2


def test_invalid_settings_are_rejected_without_applying(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """不正値は適用せず、既存設定を保持する。"""
    app_settings = make_app_settings(controller)
    before = app_settings.settings

    with pytest.raises(ValueError):
        app_settings.apply(AppSettings(3.0, True))
    with pytest.raises(ValueError):
        app_settings.apply(AppSettings(1.0, True, spectrum_visible=1))  # type: ignore[arg-type]

    assert app_settings.settings == before
    assert backend.call_names() == []


def test_shutdown_stops_mirroring(controller: PlaybackController) -> None:
    """shutdown後はController変更を取り込まない（冪等）。"""
    app_settings = make_app_settings(controller)

    app_settings.shutdown()
    app_settings.shutdown()
    controller.set_playback_rate(1.75)

    assert app_settings.settings.playback_rate == pytest.approx(1.0)


def test_session_saves_visualization_changes(
    tmp_path: Path, controller: PlaybackController, qtbot: QtBot
) -> None:
    """可視化設定の変更もデバウンス保存の対象になる。"""
    path = tmp_path / "settings.json"
    session = make_session(path, controller)
    session.load()
    session.start()
    app_settings = session._app_settings  # pyright: ignore[reportPrivateUsage]

    app_settings.apply(AppSettings(1.0, True, spectrum_visible=False))

    qtbot.waitUntil(path.is_file, timeout=2_000)
    restored = load_settings(path, AppSettings(1.0, True))
    assert restored.spectrum_visible is False
    assert restored.waveform_visible is True
    session.stop()


def test_load_applies_visualization_settings_before_start(
    tmp_path: Path, controller: PlaybackController
) -> None:
    """復元した表示設定はstart前にsnapshotへ反映され、保存契機にはならない。"""
    path = tmp_path / "settings.json"
    save_settings(path, AppSettings(1.0, True, waveform_visible=False, level_meter_visible=False))
    session = make_session(path, controller)

    assert session.load() is None

    app_settings = session._app_settings  # pyright: ignore[reportPrivateUsage]
    assert app_settings.settings.waveform_visible is False
    assert app_settings.settings.level_meter_visible is False
    assert app_settings.settings.spectrum_visible is True
    assert not session.is_running
    assert not session._timer.isActive()  # pyright: ignore[reportPrivateUsage]


def test_version_one_file_is_not_rewritten_on_startup(
    tmp_path: Path, controller: PlaybackController
) -> None:
    """version 1の設定で起動しても、読み込みだけではファイルを書き換えない。"""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"schema_version": 1, "playback_rate": 1.5, "pitch_compensation": false}\n',
        encoding="utf-8",
    )
    original = path.read_bytes()
    session = make_session(path, controller)

    assert session.load() is None
    session.start()

    assert path.read_bytes() == original
    assert controller.playback_rate == pytest.approx(1.5)
    session.stop()


def test_version_one_is_upgraded_on_the_next_change(
    tmp_path: Path, controller: PlaybackController, qtbot: QtBot
) -> None:
    """次の変更で初めてversion 2として保存される。"""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"schema_version": 1, "playback_rate": 1.5, "pitch_compensation": false}\n',
        encoding="utf-8",
    )
    session = make_session(path, controller)
    session.load()
    session.start()

    controller.set_playback_rate(1.25)

    qtbot.waitUntil(lambda: json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["playback_rate"] == pytest.approx(1.25)
    assert document["waveform_visible"] is True
    session.stop()

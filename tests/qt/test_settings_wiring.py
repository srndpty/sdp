"""AppSettingsControllerの適用調停と、SettingsSessionの復元・デバウンス・障害分離を検証する。"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from shiboken6 import isValid

from fakes.fake_playback_backend import FakePlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode
from sdp.services.settings import (
    RESTORE_FAILED_MESSAGE,
    SETTINGS_SCHEMA_VERSION,
    AppSettings,
    AppSettingsController,
    RepeatModeSetting,
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


def test_settings_changed_observes_the_already_applied_controller_values(
    controller: PlaybackController,
) -> None:
    """settings_changed受信時にはControllerとsnapshotが同じ実効値になっている。"""
    app_settings = make_app_settings(controller)
    observed: list[tuple[AppSettings, float, bool, AppSettings]] = []

    def record(value: object) -> None:
        assert isinstance(value, AppSettings)
        observed.append(
            (
                value,
                controller.playback_rate,
                controller.pitch_compensation,
                app_settings.settings,
            )
        )

    app_settings.settings_changed.connect(record)

    app_settings.apply(AppSettings(1.25, False, waveform_visible=False))

    assert observed == [(app_settings.settings, 1.25, False, app_settings.settings)]


def test_apply_uses_the_controller_effective_readback(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """Backendが補正した実効値を、要求値ではなく最終snapshotへ採用する。"""
    app_settings = make_app_settings(controller)
    backend.effective_playback_rate = 1.4
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    app_settings.apply(AppSettings(1.5, True, spectrum_visible=False))

    assert controller.playback_rate == pytest.approx(1.4)
    assert app_settings.settings.playback_rate == pytest.approx(1.4)
    assert app_settings.settings.spectrum_visible is False
    assert notified == [app_settings.settings]


def test_second_setter_failure_does_not_publish_or_keep_a_partial_apply(
    controller: PlaybackController, backend: FakePlaybackBackend
) -> None:
    """pitch適用失敗時はrateを戻し、未適用snapshotを保存通知しない。"""
    app_settings = make_app_settings(controller)
    before = app_settings.settings
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)
    backend.setter_errors["set_pitch_compensation"] = RuntimeError("故障注入")

    with pytest.raises(RuntimeError, match="故障注入"):
        app_settings.apply(AppSettings(1.25, False, waveform_visible=False))

    assert app_settings.settings == before
    assert notified == []
    assert controller.playback_rate == pytest.approx(1.0)
    assert controller.pitch_compensation is True
    assert backend.call_args("set_playback_rate") == [(1.25,), (1.0,)]


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


def test_shutdown_rejects_apply(controller: PlaybackController) -> None:
    """shutdown後はapplyも設定とControllerを変更できない。"""
    app_settings = make_app_settings(controller)
    before = app_settings.settings
    app_settings.shutdown()

    with pytest.raises(RuntimeError, match="shutdown後"):
        app_settings.apply(AppSettings(1.5, False, waveform_visible=False))

    assert app_settings.settings == before
    assert controller.playback_rate == pytest.approx(1.0)
    assert controller.pitch_compensation is True


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
    """次の変更で初めて現在のversionとして保存される。"""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"schema_version": 1, "playback_rate": 1.5, "pitch_compensation": false}\n',
        encoding="utf-8",
    )
    session = make_session(path, controller)
    session.load()
    session.start()

    controller.set_playback_rate(1.25)

    qtbot.waitUntil(
        lambda: (
            json.loads(path.read_text(encoding="utf-8"))["schema_version"]
            == SETTINGS_SCHEMA_VERSION
        )
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["playback_rate"] == pytest.approx(1.25)
    assert document["waveform_visible"] is True
    session.stop()


# -- 2つのControllerの調停（P6-C）-------------------------------------------


@pytest.fixture
def playlist_model() -> PlaylistModel:
    return PlaylistModel()


@pytest.fixture
def playlist_playback(
    controller: PlaybackController, playlist_model: PlaylistModel
) -> PlaylistPlaybackController:
    return PlaylistPlaybackController(controller, playlist_model)


def make_full_app_settings(
    controller: PlaybackController, playlist_playback: PlaylistPlaybackController
) -> AppSettingsController:
    return AppSettingsController(controller, playlist_playback)


def test_initial_snapshot_includes_playback_state(
    controller: PlaybackController, playlist_playback: PlaylistPlaybackController
) -> None:
    """音量・ミュート・Repeat・Shuffleの実効値もsnapshotへ入る。"""
    controller.set_volume(0.3)
    playlist_playback.set_repeat_mode(RepeatMode.ONE)

    app_settings = make_full_app_settings(controller, playlist_playback)

    assert app_settings.settings.volume == pytest.approx(0.3)
    assert app_settings.settings.muted is False
    assert app_settings.settings.repeat_mode is RepeatModeSetting.ONE
    assert app_settings.settings.shuffle_enabled is False


def test_apply_reaches_both_controllers_with_one_notification(
    controller: PlaybackController,
    playlist_playback: PlaylistPlaybackController,
    backend: FakePlaybackBackend,
) -> None:
    """6項目以上を一括適用しても通知は1回で、両Controllerの実効値と一致する。"""
    app_settings = make_full_app_settings(controller, playlist_playback)
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    app_settings.apply(
        AppSettings(
            playback_rate=1.25,
            pitch_compensation=False,
            waveform_visible=False,
            spectrum_visible=False,
            level_meter_visible=False,
            volume=0.2,
            muted=True,
            repeat_mode=RepeatModeSetting.ALL,
            shuffle_enabled=True,
        )
    )

    assert len(notified) == 1
    assert controller.playback_rate == pytest.approx(1.25)
    assert controller.volume == pytest.approx(0.2)
    assert controller.muted is True
    assert backend.muted is True
    assert playlist_playback.repeat_mode is RepeatMode.ALL
    assert playlist_playback.shuffle_enabled is True
    # 通知時点でsnapshotと実効値が一致している。
    assert app_settings.settings.volume == pytest.approx(controller.volume)
    assert app_settings.settings.repeat_mode is RepeatModeSetting.from_repeat_mode(
        playlist_playback.repeat_mode
    )


def test_player_controls_changes_are_mirrored(
    controller: PlaybackController, playlist_playback: PlaylistPlaybackController
) -> None:
    """PlayerControlsやショートカット経由の変更もsnapshotへ取り込む。"""
    app_settings = make_full_app_settings(controller, playlist_playback)
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    controller.set_volume(0.6)
    controller.set_muted(True)
    playlist_playback.set_repeat_mode(RepeatMode.ALL)
    playlist_playback.set_shuffle_enabled(True)

    assert app_settings.settings.volume == pytest.approx(0.6)
    assert app_settings.settings.muted is True
    assert app_settings.settings.repeat_mode is RepeatModeSetting.ALL
    assert app_settings.settings.shuffle_enabled is True
    assert len(notified) == 4


def test_failed_apply_rolls_back_every_changed_controller(
    controller: PlaybackController,
    playlist_playback: PlaylistPlaybackController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2つ目以降のsetterが失敗したら、部分適用を残さず元へ戻す。"""
    app_settings = make_full_app_settings(controller, playlist_playback)
    before = app_settings.settings
    notified: list[object] = []
    app_settings.settings_changed.connect(notified.append)

    def explode(enabled: bool) -> None:
        del enabled
        raise RuntimeError("シャッフルを設定できません")

    monkeypatch.setattr(playlist_playback, "set_shuffle_enabled", explode)

    with pytest.raises(RuntimeError):
        app_settings.apply(
            replace(
                before,
                playback_rate=1.5,
                volume=0.1,
                muted=True,
                repeat_mode=RepeatModeSetting.ONE,
                shuffle_enabled=True,
            )
        )

    assert controller.playback_rate == pytest.approx(before.playback_rate)
    assert controller.volume == pytest.approx(before.volume)
    assert controller.muted is before.muted
    assert playlist_playback.repeat_mode is before.repeat_mode.to_repeat_mode()
    # 未適用のsnapshotは公開しない（保存も予約されない）。
    assert app_settings.settings == before
    assert notified == []


def test_apply_after_shutdown_is_rejected(
    controller: PlaybackController, playlist_playback: PlaylistPlaybackController
) -> None:
    """shutdown後の適用は拒否し、Controllerへ触れない。"""
    app_settings = make_full_app_settings(controller, playlist_playback)
    app_settings.shutdown()

    with pytest.raises(RuntimeError, match="shutdown後"):
        app_settings.apply(replace(app_settings.settings, volume=0.1))

    assert controller.volume == pytest.approx(1.0)


def test_shutdown_stops_mirroring_both_controllers(
    controller: PlaybackController, playlist_playback: PlaylistPlaybackController
) -> None:
    """shutdown後は両Controllerの変更を取り込まない。"""
    app_settings = make_full_app_settings(controller, playlist_playback)

    app_settings.shutdown()
    controller.set_volume(0.1)
    playlist_playback.set_shuffle_enabled(True)

    assert app_settings.settings.volume == pytest.approx(1.0)
    assert app_settings.settings.shuffle_enabled is False


def test_session_saves_playback_state(
    tmp_path: Path,
    controller: PlaybackController,
    playlist_playback: PlaylistPlaybackController,
    qtbot: QtBot,
) -> None:
    """音量・Repeatの変更もデバウンス保存され、次回のloadで戻る。"""
    path = tmp_path / "settings.json"
    app_settings = make_full_app_settings(controller, playlist_playback)
    session = SettingsSession(path, app_settings, debounce_ms=DEBOUNCE_MS, retry_ms=DEBOUNCE_MS)
    session.load()
    session.start()

    controller.set_volume(0.35)
    playlist_playback.set_repeat_mode(RepeatMode.ALL)

    qtbot.waitUntil(path.is_file, timeout=2_000)
    qtbot.waitUntil(
        lambda: load_settings(path, AppSettings(1.0, True)).repeat_mode is RepeatModeSetting.ALL,
        timeout=2_000,
    )
    restored = load_settings(path, AppSettings(1.0, True))
    assert restored.volume == pytest.approx(0.35)
    assert restored.repeat_mode is RepeatModeSetting.ALL
    session.stop()


def test_load_applies_playback_state_to_both_controllers(
    tmp_path: Path,
    controller: PlaybackController,
    playlist_playback: PlaylistPlaybackController,
) -> None:
    """復元でPlaybackControllerとPlaylistPlaybackControllerの両方へ適用する。"""
    path = tmp_path / "settings.json"
    save_settings(
        path,
        AppSettings(
            playback_rate=1.0,
            pitch_compensation=True,
            volume=0.15,
            muted=True,
            repeat_mode=RepeatModeSetting.ONE,
            shuffle_enabled=True,
        ),
    )
    app_settings = make_full_app_settings(controller, playlist_playback)
    session = SettingsSession(path, app_settings, debounce_ms=DEBOUNCE_MS, retry_ms=DEBOUNCE_MS)

    assert session.load() is None

    assert controller.volume == pytest.approx(0.15)
    assert controller.muted is True
    assert playlist_playback.repeat_mode is RepeatMode.ONE
    assert playlist_playback.shuffle_enabled is True


def test_save_failure_and_recovery_are_reported_once(
    tmp_path: Path,
    controller: PlaybackController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存失敗・復旧は状態が変わったときだけ通知する。"""
    session = make_session(tmp_path / "settings.json", controller)
    session.load()
    failures: list[int] = []
    recoveries: list[int] = []
    session.save_failed.connect(lambda: failures.append(1))
    session.save_recovered.connect(lambda: recoveries.append(1))

    calls: list[int] = []

    def flaky_save(path: Path, settings: AppSettings) -> None:
        del path, settings
        calls.append(1)
        if len(calls) <= 2:
            raise OSError("保存失敗")

    monkeypatch.setattr("sdp.services.settings.save_settings", flaky_save)
    controller.set_playback_rate(1.25)
    assert session.flush() is False
    controller.set_playback_rate(1.3)
    assert session.flush() is False
    controller.set_playback_rate(1.35)
    assert session.flush() is True

    assert failures == [1]
    assert recoveries == [1]

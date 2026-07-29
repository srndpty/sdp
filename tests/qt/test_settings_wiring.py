"""SettingsSessionのController適用・デバウンス・障害分離を検証する。"""

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
    return SettingsSession(path, controller, debounce_ms=DEBOUNCE_MS)


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

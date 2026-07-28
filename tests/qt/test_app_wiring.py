"""app.py の組み立てを検証する。

イベントループは起動しない（無期限に待つテストを作らない）。
本番配線の確認に音声再生は不要。
"""

from collections.abc import Iterator

import pytest
from pytestqt.qtbot import QtBot

from sdp import app as app_module
from sdp.core.playback.backend import PlaybackBackend
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.ui.main_window import MainWindow
from sdp.ui.player_controls import PlayerControls


@pytest.fixture
def composition(qtbot: QtBot) -> Iterator[app_module.PlayerComposition]:
    built = app_module.build_player()
    qtbot.addWidget(built.window)
    yield built


def test_build_player_creates_the_three_layers(
    composition: app_module.PlayerComposition,
) -> None:
    """Backend → Controller → MainWindow が生成される。"""
    assert isinstance(composition.backend, QtMultimediaBackend)
    assert isinstance(composition.backend, PlaybackBackend)
    assert isinstance(composition.controller, PlaybackController)
    assert isinstance(composition.window, MainWindow)


def test_window_uses_the_wired_controller(
    composition: app_module.PlayerComposition,
) -> None:
    """MainWindow の PlayerControls が同じ Controller の通知を受け取っている。"""
    controls = composition.window.findChild(PlayerControls)
    assert controls is not None
    # Controller が Backend の音量変更を UI へ中継できる配線になっている。
    composition.controller.set_volume(0.5)
    assert composition.backend.volume == pytest.approx(0.5, abs=1e-6)


def test_composition_does_not_let_the_window_own_the_backend(
    composition: app_module.PlayerComposition,
) -> None:
    """MainWindow は Backend を所有も参照もしない。"""
    assert composition.backend.parent() is None
    assert composition.backend not in composition.window.findChildren(QtMultimediaBackend)
    exposed = [name for name in dir(composition.window) if not name.startswith("_")]
    for name in exposed:
        assert not isinstance(getattr(composition.window, name), PlaybackBackend), name


def test_create_application_sets_metadata(qtbot: QtBot) -> None:
    """QApplication のメタ情報が設定される（既存インスタンスへ適用される）。"""
    del qtbot
    app = app_module.create_application([])

    assert app.applicationName() == "sdp"
    assert app.applicationDisplayName() == "sdp"
    assert app.organizationName() == "sdp"


def test_entry_point_delegates_to_app_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m sdp` は app.run を呼ぶだけで、組み立てを重複させない。"""
    from sdp import __main__ as main_module

    calls: list[str] = []
    monkeypatch.setattr(app_module, "run", lambda: calls.append("run") or 0)

    assert main_module.main() == 0
    assert calls == ["run"]

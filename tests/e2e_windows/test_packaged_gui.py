# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""build済みWindows配布版をUI Automationで操作するsmoke test。"""

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

if sys.platform == "win32":
    from pywinauto import Application, Desktop
else:
    Application = None  # type: ignore[assignment,misc]
    Desktop = None  # type: ignore[assignment,misc]

pytestmark = [
    pytest.mark.packaged_gui,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows配布版専用"),
]

_PACKAGE_ENV = "SDP_PACKAGE_DIRECTORY"
_WAIT_SECONDS = 15.0


def _wait_until(predicate: Callable[[], bool], message: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def _visible_texts(window: Any) -> set[str]:
    texts: set[str] = set()
    for control in window.descendants():
        text = control.window_text().strip()
        if text:
            texts.add(text)
    return texts


def _find_process_control(desktop: Any, *, process_id: int, title: str, control_type: str) -> Any:
    """process配下のpopupを含め、指定したUI Automation要素を待つ。"""
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        for top_level in desktop.windows(process=process_id):
            controls = [top_level, *top_level.descendants()]
            for control in controls:
                if (
                    control.element_info.control_type == control_type
                    and control.window_text() == title
                ):
                    return control
        time.sleep(0.1)
    raise AssertionError(f"UI Automation要素が見つかりません: {control_type} {title}")


@pytest.fixture
def packaged_window(
    tmp_path: Path, test_audio_dir: Path
) -> Iterator[tuple[subprocess.Popen[bytes], Any]]:
    """隔離profileで配布版を起動し、終了時にprocess残留を許さない。"""
    configured = os.environ.get(_PACKAGE_ENV)
    if not configured:
        pytest.skip(f"{_PACKAGE_ENV}が未指定")
    executable = Path(configured).resolve() / "sdp.exe"
    if not executable.is_file():
        pytest.fail(f"配布版sdp.exeがありません: {executable}")

    sources = [
        test_audio_dir / "日本語 ディレクトリ" / "テスト 音源 440Hz.flac",
        test_audio_dir / "sine440.mp3",
    ]
    for source in sources:
        assert source.is_file(), source

    profile_directory = tmp_path / "profile"
    runtime_directory = tmp_path / "runtime"
    local_app_data_directory = tmp_path / "local-app-data"
    for directory in (
        profile_directory,
        runtime_directory,
        local_app_data_directory,
    ):
        directory.mkdir()

    # single-instance識別子とQLockFileの配置先も隔離し、通常起動中のsdpや
    # 並列test processと衝突させない。
    isolated_user = f"sdp-e2e-{tmp_path.name}"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(profile_directory),
            "USERPROFILE": str(profile_directory),
            "LOCALAPPDATA": str(local_app_data_directory),
            "TEMP": str(runtime_directory),
            "TMP": str(runtime_directory),
            "USERNAME": isolated_user,
            "USER": isolated_user,
            "LOGNAME": isolated_user,
            "LNAME": isolated_user,
            "USERDOMAIN": "SDP-E2E",
            "SESSIONNAME": isolated_user,
        }
    )
    environment.pop("QT_QPA_PLATFORM", None)
    process = subprocess.Popen(
        [str(executable), *(str(source) for source in sources)],
        cwd=tmp_path,
        env=environment,
    )
    window: Any | None = None
    try:
        assert Application is not None
        application: Any = Application(backend="uia").connect(
            process=process.pid, timeout=_WAIT_SECONDS
        )
        candidate: Any = application.window(title="sdpメインウィンドウ")
        candidate.wait("visible enabled ready", timeout=_WAIT_SECONDS)
        window = candidate.wrapper_object()
        yield process, window
    finally:
        if window is not None and process.poll() is None:
            window.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def test_packaged_gui_accepts_paths_and_exposes_core_controls(
    packaged_window: tuple[subprocess.Popen[bytes], Any],
) -> None:
    """日本語・空白入りを含む複数path、主要操作、設定dialog、正常終了を確認する。"""
    process, window = packaged_window

    expected = {
        "テスト 音源 440Hz.flac",
        "sine440.mp3",
        "再生",
        "一時停止",
        "停止",
        "ミュート",
        "プレイリスト",
    }
    _wait_until(
        lambda: expected <= _visible_texts(window),
        f"主要UIまたは追加したpathを確認できません: {_visible_texts(window)}",
    )

    assert Desktop is not None
    window.set_focus()
    tools_menu = next(
        control
        for control in window.descendants()
        if control.element_info.control_type == "MenuItem" and control.window_text() == "ツール(T)"
    )
    tools_menu.click_input()
    desktop: Any = Desktop(backend="uia")
    settings_item = _find_process_control(
        desktop,
        process_id=process.pid,
        title="設定...(S)",
        control_type="MenuItem",
    )
    settings_item.click_input()

    dialog = _find_process_control(
        desktop,
        process_id=process.pid,
        title="設定",
        control_type="Window",
    )
    settings_texts = _visible_texts(dialog)
    assert {"1.00×", "音量", "リピート", "波形を表示", "適用", "キャンセル"} <= settings_texts
    cancel_button = next(
        control
        for control in dialog.descendants()
        if control.element_info.control_type == "Button" and control.window_text() == "キャンセル"
    )
    cancel_button.click_input()
    _wait_until(lambda: not dialog.is_visible(), "設定ダイアログが閉じません")

    window.close()
    assert process.wait(timeout=5) == 0

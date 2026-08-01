# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""build済みWindows配布版をUI Automationで操作するsmoke test。"""

import json
import os
import subprocess
import sys
import time
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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


@dataclass
class PackagedSession:
    """起動中の配布版と隔離した永続化領域。"""

    process: subprocess.Popen[bytes]
    window: Any
    executable: Path
    environment: dict[str, str]
    work_directory: Path
    app_data_directory: Path
    sources: tuple[Path, ...]


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


def _find_descendant(window: Any, *, title: str, control_type: str) -> Any:
    """Window配下から名前と種類が一致するUI Automation要素を返す。"""
    return next(
        control
        for control in window.descendants()
        if control.element_info.control_type == control_type and control.window_text() == title
    )


def _launch_window(
    executable: Path,
    arguments: list[str],
    work_directory: Path,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], Any]:
    process = subprocess.Popen(
        [str(executable), *arguments],
        cwd=work_directory,
        env=environment,
    )
    assert Application is not None
    application: Any = Application(backend="uia").connect(
        process=process.pid, timeout=_WAIT_SECONDS
    )
    candidate: Any = application.window(title="sdpメインウィンドウ")
    candidate.wait("visible enabled ready", timeout=_WAIT_SECONDS)
    return process, candidate.wrapper_object()


def _cleanup_process(process: subprocess.Popen[bytes], window: Any | None) -> None:
    """失敗時cleanupではterminate、最後にkillまで行ってprocessを残さない。"""
    if window is not None and process.poll() is None:
        window.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _open_settings(session: PackagedSession) -> Any:
    assert Desktop is not None
    session.window.set_focus()
    # Popup menuのUIA tree公開はQt/Windowsのtimingで揺れるため、表示項目の
    # mnemonic（Alt+T, S）を実際に入力して設定dialogを開く。
    session.window.type_keys("%ts")
    desktop: Any = Desktop(backend="uia")
    return _find_process_control(
        desktop,
        process_id=session.process.pid,
        title="設定",
        control_type="Window",
    )


@pytest.fixture
def packaged_window(tmp_path: Path, test_audio_dir: Path) -> Iterator[PackagedSession]:
    """隔離profileで配布版を起動し、終了時にprocess残留を許さない。"""
    configured = os.environ.get(_PACKAGE_ENV)
    if not configured:
        pytest.skip(f"{_PACKAGE_ENV}が未指定")
    executable = Path(configured).resolve() / "sdp.exe"
    if not executable.is_file():
        pytest.fail(f"配布版sdp.exeがありません: {executable}")

    operation_audio = tmp_path / "操作 テスト.wav"
    with wave.open(str(operation_audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(b"\0\0" * 8_000 * 10)

    sources = [
        operation_audio,
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
    process, window = _launch_window(
        executable,
        [str(source) for source in sources],
        tmp_path,
        environment,
    )
    try:
        yield PackagedSession(
            process=process,
            window=window,
            executable=executable,
            environment=environment,
            work_directory=tmp_path,
            app_data_directory=local_app_data_directory / "sdp",
            sources=tuple(sources),
        )
    finally:
        _cleanup_process(process, window)


def test_packaged_gui_accepts_paths_and_exposes_core_controls(
    packaged_window: PackagedSession,
) -> None:
    """日本語path、再生操作、Cancelの破棄、正常終了を実操作で確認する。"""
    session = packaged_window
    process = session.process
    window = session.window

    expected = {
        "テスト 音源 440Hz.flac",
        "操作 テスト.wav",
        "3曲",
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

    settings_file = session.app_data_directory / "settings.json"
    assert not settings_file.exists()
    dialog = _open_settings(session)
    settings_texts = _visible_texts(dialog)
    assert {"1.00×", "音量", "リピート", "波形を表示", "適用", "キャンセル"} <= settings_texts
    rate = _find_descendant(dialog, title="1.00×", control_type="Spinner")
    rate.click_input()
    rate.type_keys("^a1.25")
    _wait_until(lambda: "1.25×" in _visible_texts(dialog), "再生速度を編集できません")
    _find_descendant(dialog, title="キャンセル", control_type="Button").click_input()
    _wait_until(lambda: not dialog.is_visible(), "設定ダイアログが閉じません")
    assert not settings_file.exists()

    reopened = _open_settings(session)
    assert "1.00×" in _visible_texts(reopened)
    _find_descendant(reopened, title="キャンセル", control_type="Button").click_input()
    _wait_until(lambda: not reopened.is_visible(), "設定ダイアログが閉じません")

    playlist_item = _find_descendant(
        window,
        title="操作 テスト.wav",
        control_type="DataItem",
    )
    playlist_item.click_input()
    playlist_item.type_keys("{ENTER}")
    _wait_until(
        lambda: any(
            control.element_info.control_type == "Text" and control.window_text() == "再生中"
            for control in window.descendants()
        ),
        "プレイリストから再生状態へ遷移しません",
    )

    _find_descendant(window, title="一時停止", control_type="Button").click_input()
    _wait_until(
        lambda: any(
            control.element_info.control_type == "Text" and control.window_text() == "一時停止"
            for control in window.descendants()
        ),
        "一時停止状態へ遷移しません",
    )
    _find_descendant(window, title="再生", control_type="Button").click_input()
    _wait_until(
        lambda: any(
            control.element_info.control_type == "Text" and control.window_text() == "再生中"
            for control in window.descendants()
        ),
        "一時停止から再開できません",
    )

    mute = _find_descendant(window, title="ミュート", control_type="CheckBox")
    mute.click_input()
    assert mute.get_toggle_state() == 1
    mute.click_input()
    assert mute.get_toggle_state() == 0
    _find_descendant(window, title="停止", control_type="Button").click_input()
    _wait_until(
        lambda: any(
            control.element_info.control_type == "Text" and control.window_text() == "停止"
            for control in window.descendants()
        ),
        "停止状態へ遷移しません",
    )

    window.close()
    assert process.wait(timeout=5) == 0


def test_packaged_gui_applies_and_restores_settings(packaged_window: PackagedSession) -> None:
    """再生・表示設定一式がApplyされ、同じprofileでの再起動後も復元される。"""
    session = packaged_window
    settings_file = session.app_data_directory / "settings.json"

    repeat = _find_descendant(session.window, title="リピート", control_type="Button")
    repeat.click_input()
    shuffle = _find_descendant(session.window, title="シャッフル", control_type="CheckBox")
    shuffle.click_input()
    assert shuffle.get_toggle_state() == 1
    mute = _find_descendant(session.window, title="ミュート", control_type="CheckBox")
    mute.click_input()
    assert mute.get_toggle_state() == 1

    dialog = _open_settings(session)
    rate = _find_descendant(dialog, title="1.00×", control_type="Spinner")
    rate.click_input()
    rate.type_keys("^a1.25")
    volume = _find_descendant(dialog, title="100％", control_type="Spinner")
    volume.click_input()
    volume.type_keys("^a35")

    pitch = _find_descendant(dialog, title="ピッチ補正", control_type="CheckBox")
    expected_pitch = pitch.get_toggle_state() == 0
    pitch.click_input()
    for title in ("波形を表示", "スペクトラムを表示", "レベルメーターを表示"):
        check_box = _find_descendant(dialog, title=title, control_type="CheckBox")
        assert check_box.get_toggle_state() == 1
        check_box.click_input()
    _find_descendant(dialog, title="適用", control_type="Button").click_input()

    assert dialog.is_visible()
    _wait_until(settings_file.is_file, "Apply後にsettings.jsonが保存されません")
    expected_settings = {
        "playback_rate": 1.25,
        "pitch_compensation": expected_pitch,
        "waveform_visible": False,
        "spectrum_visible": False,
        "level_meter_visible": False,
        "volume": 0.35,
        "muted": True,
        "repeat_mode": "all",
        "shuffle_enabled": True,
    }

    def settings_are_saved() -> bool:
        document = json.loads(settings_file.read_text(encoding="utf-8"))
        return all(document.get(key) == value for key, value in expected_settings.items())

    _wait_until(settings_are_saved, "適用した設定一式がsettings.jsonへ保存されません")
    _find_descendant(dialog, title="キャンセル", control_type="Button").click_input()

    session.window.close()
    assert session.process.wait(timeout=5) == 0

    restarted_process: subprocess.Popen[bytes] | None = None
    restarted_window: Any | None = None
    try:
        restarted_process, restarted_window = _launch_window(
            session.executable,
            [],
            session.work_directory,
            session.environment,
        )
        restarted = PackagedSession(
            process=restarted_process,
            window=restarted_window,
            executable=session.executable,
            environment=session.environment,
            work_directory=session.work_directory,
            app_data_directory=session.app_data_directory,
            sources=session.sources,
        )
        restored_dialog = _open_settings(restarted)
        restored_texts = _visible_texts(restored_dialog)
        assert {"1.25×", "35％"} <= restored_texts
        for title in ("ミュート", "シャッフル"):
            assert (
                _find_descendant(
                    restored_dialog, title=title, control_type="CheckBox"
                ).get_toggle_state()
                == 1
            )
        for title in ("波形を表示", "スペクトラムを表示", "レベルメーターを表示"):
            assert (
                _find_descendant(
                    restored_dialog, title=title, control_type="CheckBox"
                ).get_toggle_state()
                == 0
            )
        _find_descendant(restored_dialog, title="キャンセル", control_type="Button").click_input()
        assert (
            _find_descendant(
                restarted_window, title="シャッフル", control_type="CheckBox"
            ).get_toggle_state()
            == 1
        )
        assert (
            _find_descendant(
                restarted_window, title="ミュート", control_type="CheckBox"
            ).get_toggle_state()
            == 1
        )
        assert restarted_window is not None
        restarted_window.close()
        assert restarted_process.wait(timeout=5) == 0
    finally:
        if restarted_process is not None:
            _cleanup_process(restarted_process, restarted_window)


def test_packaged_gui_forwards_relative_path_to_running_instance(
    packaged_window: PackagedSession,
) -> None:
    """相対pathをsecondaryから転送し、primaryを復元して1processへ集約する。"""
    session = packaged_window
    forwarded_audio = session.work_directory / "二重起動 転送.wav"
    with wave.open(str(forwarded_audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8_000)
        stream.writeframes(b"\0\0" * 8_000)

    session.window.minimize()
    _wait_until(session.window.is_minimized, "primaryを最小化できません")
    secondary = subprocess.run(
        [str(session.executable), forwarded_audio.name],
        cwd=session.work_directory,
        env=session.environment,
        timeout=_WAIT_SECONDS,
        check=False,
    )

    assert secondary.returncode == 0
    assert session.process.poll() is None
    _wait_until(lambda: not session.window.is_minimized(), "転送後にprimaryが復元されません")
    _wait_until(
        lambda: {"4曲", "1曲をプレイリストへ追加しました。"} <= _visible_texts(session.window),
        "secondaryの相対pathがprimaryへ追加されません",
    )
    _wait_until(
        (session.app_data_directory / "logs" / "sdp.log").is_file,
        "配布版のログファイルが生成されません",
    )

    session.window.maximize()
    _wait_until(session.window.is_maximized, "primaryを最大化できません")
    activation_only = subprocess.run(
        [str(session.executable)],
        cwd=session.work_directory,
        env=session.environment,
        timeout=_WAIT_SECONDS,
        check=False,
    )
    assert activation_only.returncode == 0
    assert session.process.poll() is None
    _wait_until(session.window.is_maximized, "転送要求でprimaryの最大化状態が失われました")

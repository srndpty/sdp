"""QLocalServerによる単一instance IPCを実際のlocal socketで検証する。"""

import json
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QThread
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from pytestqt.qtbot import QtBot

from sdp.services import single_instance
from sdp.services.launch_request import LaunchRequest
from sdp.services.single_instance import (
    IPC_VERSION,
    MAX_MESSAGE_SIZE,
    InstanceOutcome,
    SingleInstanceService,
    _encode_frame,  # pyright: ignore[reportPrivateUsage]
)
from sdp.services.thread_shutdown import ShutdownOutcome


def unique_server_name() -> str:
    """並列テストでも衝突しない注入用server名。"""
    return f"sdp-test-{uuid.uuid4().hex}"


def connect_socket(name: str, qtbot: QtBot) -> QLocalSocket:
    socket = QLocalSocket()
    socket.connectToServer(name)
    qtbot.waitUntil(socket.isValid, timeout=2_000)
    return socket


def raw_frame(document: object) -> bytes:
    payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def forward_in_process(
    name: str,
    request: LaunchRequest,
) -> subprocess.Popen[str]:
    """実際のsecondary processから要求を転送する。"""
    code = """
import json
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from sdp.services.launch_request import LaunchRequest
from sdp.services.single_instance import SingleInstanceService

application = QCoreApplication([])
request = LaunchRequest(
    tuple(Path(value) for value in json.loads(sys.argv[2])),
    tuple(json.loads(sys.argv[3])),
    json.loads(sys.argv[4]),
)
service = SingleInstanceService(sys.argv[1], connect_timeout_ms=2_000)
print(service.start_or_forward(request).name, flush=True)
service.shutdown()
"""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            name,
            json.dumps([str(path) for path in request.paths]),
            json.dumps(request.ignored_arguments),
            json.dumps(request.activate_window),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def test_primary_and_secondary_transfer_one_request(tmp_path: Path, qtbot: QtBot) -> None:
    """最初がprimaryとなり、次のinstanceは要求転送後に常駐しない。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    request = LaunchRequest(((tmp_path / "曲.wav").resolve(),))
    process = forward_in_process(name, request)
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=5_000)
    stdout, stderr = process.communicate(timeout=1)

    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceOutcome.FORWARDED.name
    assert received == [request]
    primary.shutdown()


def test_request_is_acknowledged_and_queued_before_delivery_starts(
    tmp_path: Path, qtbot: QtBot
) -> None:
    """composition構築中でも受理ACKを返し、handler準備後に1回だけ通知する。"""
    name = unique_server_name()
    primary = SingleInstanceService(name, startup_timeout_ms=1_000)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    request = LaunchRequest(((tmp_path / "startup.wav").resolve(),))

    process = forward_in_process(name, request)
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=3_000)
    stdout, stderr = process.communicate(timeout=1)

    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceOutcome.FORWARDED.name
    assert received == []

    primary.start_delivery()
    qtbot.waitUntil(lambda: received == [request], timeout=2_000)
    primary.start_delivery()
    qtbot.wait(10)

    assert received == [request]
    primary.shutdown()


def test_multiple_paths_keep_order_duplicates_and_unicode(tmp_path: Path, qtbot: QtBot) -> None:
    """複数pathの順序・重複・UnicodeをIPC往復で維持する。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    first = (tmp_path / "日本語 曲.wav").resolve()
    second = (tmp_path / "second.mp3").resolve()
    request = LaunchRequest((first, second, first))
    process = forward_in_process(name, request)
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=5_000)
    stdout, stderr = process.communicate(timeout=1)

    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceOutcome.FORWARDED.name
    assert received == [request]
    primary.shutdown()


def test_activate_window_flag_round_trips(tmp_path: Path, qtbot: QtBot) -> None:
    """Window前面化意図をIPC往復で厳密に保つ。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    request = LaunchRequest(((tmp_path / "quiet.wav").resolve(),), (), False)

    process = forward_in_process(name, request)
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=5_000)
    stdout, stderr = process.communicate(timeout=1)

    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceOutcome.FORWARDED.name
    assert received == [request]
    primary.shutdown()


def test_partial_frame_is_not_published_until_complete(tmp_path: Path, qtbot: QtBot) -> None:
    """headerとpayloadの分割受信を蓄積し、完成前には通知しない。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    socket = connect_socket(name, qtbot)
    frame = _encode_frame(LaunchRequest(((tmp_path / "partial.wav").resolve(),)))

    socket.write(frame[:2])
    socket.flush()
    qtbot.wait(10)
    assert received == []

    socket.write(frame[2:])
    socket.flush()
    qtbot.waitUntil(lambda: len(received) == 1, timeout=2_000)
    socket.abort()
    primary.shutdown()


def test_continuous_frames_are_both_published(tmp_path: Path, qtbot: QtBot) -> None:
    """1socketへ連続した複数messageが届いても境界を失わない。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    socket = connect_socket(name, qtbot)
    requests = [
        LaunchRequest(((tmp_path / "one.wav").resolve(),)),
        LaunchRequest(((tmp_path / "two.mp3").resolve(),)),
    ]

    socket.write(b"".join(_encode_frame(request) for request in requests))
    socket.flush()
    qtbot.waitUntil(lambda: len(received) == 2, timeout=2_000)

    assert received == requests
    socket.abort()
    primary.shutdown()


def test_invalid_json_and_unknown_version_do_not_stop_primary(tmp_path: Path, qtbot: QtBot) -> None:
    """不正JSON・version・前面化意図を無視後もprimaryが有効要求を処理する。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    socket = connect_socket(name, qtbot)
    invalid_json = struct.pack(">I", 1) + b"{"
    unknown = raw_frame({"version": IPC_VERSION + 1, "paths": []})
    missing_activation = raw_frame({"version": IPC_VERSION, "paths": [], "ignored_arguments": []})
    invalid_activation = raw_frame(
        {
            "version": IPC_VERSION,
            "paths": [],
            "ignored_arguments": [],
            "activate_window": 1,
        }
    )
    valid = LaunchRequest(((tmp_path / "valid.flac").resolve(),))

    socket.write(
        invalid_json + unknown + missing_activation + invalid_activation + _encode_frame(valid)
    )
    socket.flush()
    qtbot.waitUntil(lambda: len(received) == 1, timeout=2_000)

    assert received == [valid]
    assert primary.is_primary
    socket.abort()
    primary.shutdown()


def test_oversized_message_is_rejected_and_primary_remains_available(qtbot: QtBot) -> None:
    """上限超過headerではpayloadを待たず切断し、primary自体は終了しない。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_delivery()
    socket = connect_socket(name, qtbot)

    socket.write(struct.pack(">I", MAX_MESSAGE_SIZE + 1))
    socket.flush()
    qtbot.waitUntil(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)

    assert primary.is_primary
    primary.shutdown()


def test_stale_server_endpoint_is_removed_only_after_lock_acquisition() -> None:
    """所有者のいないendpoint残骸があれば排他lock取得後に除去してprimaryになる。"""
    name = unique_server_name()
    stale = QLocalServer()
    QLocalServer.removeServer(name)
    assert stale.listen(name)
    stale.close()
    service = SingleInstanceService(name, connect_timeout_ms=20, startup_timeout_ms=50)

    assert service.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY

    service.shutdown()


def test_shutdown_releases_name_for_next_primary() -> None:
    """shutdown後はserver名とlockを残さず同名で再起動できる。"""
    name = unique_server_name()
    first = SingleInstanceService(name)
    assert first.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    first.shutdown()
    second = SingleInstanceService(name)

    assert second.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY

    second.shutdown()


def test_message_size_limit_is_enforced_before_socket_write(tmp_path: Path) -> None:
    """送信側でも無制限なmessageを生成しない。"""
    huge = "x" * MAX_MESSAGE_SIZE
    request = LaunchRequest(((tmp_path / "valid.wav").resolve(),), (huge,))

    try:
        _encode_frame(request)
    except ValueError as error:
        assert "サイズ上限" in str(error)
    else:  # pragma: no cover - 上限契約が壊れた場合だけ
        raise AssertionError("サイズ上限を超えたmessageが生成されました")


def test_each_test_can_inject_an_independent_name() -> None:
    """server名は固定globalではなく、テスト・用途ごとに注入できる。"""
    names = {unique_server_name() for _ in range(10)}

    assert len(names) == 10


def test_abandoned_server_thread_keeps_the_lock_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPC threadを停止できなかった場合、lockとserver名を解放しない。

    旧server threadが動いたままlockを手放すと、新しいsdpがprimaryとして起動でき、
    さらに旧serverが一時的に接続を受けても要求がUIへ渡らず消失しうる。
    """
    name = unique_server_name()
    service = SingleInstanceService(name, connect_timeout_ms=20, startup_timeout_ms=50)
    assert service.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    thread = service._server_thread  # pyright: ignore[reportPrivateUsage]
    assert thread is not None
    lock = service._lock  # pyright: ignore[reportPrivateUsage]
    real_remove_server = QLocalServer.removeServer

    removed: list[str] = []

    def abandon(*_args: object, **_kwargs: object) -> ShutdownOutcome:
        return ShutdownOutcome.ABANDONED

    monkeypatch.setattr(single_instance, "stop_thread", abandon)
    monkeypatch.setattr(QLocalServer, "removeServer", staticmethod(removed.append))
    try:
        service.shutdown()

        assert removed == []
        assert lock.isLocked()
    finally:
        # stop_threadを差し替えているため、放棄したthreadはテスト側で確実に止める。
        thread.quit()
        assert thread.wait(5_000)
        real_remove_server(name)
        lock.unlock()


def test_startup_abandonment_is_not_stopped_again_by_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動時に放棄したIPC threadを、直後のshutdown()でもう一度停止しない。

    二重に停止しようとすると、戻らないthreadでhard timeoutぶんの待機を二度払い、
    同じQThreadをreaperへ二重登録しうる。放棄した時点で所有権はreaper側にある。
    """
    name = unique_server_name()

    def listen_fails(_self: object) -> bool:
        return False

    monkeypatch.setattr(
        single_instance._LocalServerEndpoint,  # pyright: ignore[reportPrivateUsage]
        "listen",
        listen_fails,
    )
    stopped: list[QThread] = []

    def abandon(target: QThread, **_kwargs: object) -> ShutdownOutcome:
        stopped.append(target)
        return ShutdownOutcome.ABANDONED

    monkeypatch.setattr(single_instance, "stop_thread", abandon)
    removed: list[str] = []
    monkeypatch.setattr(QLocalServer, "removeServer", staticmethod(removed.append))

    service = SingleInstanceService(name, connect_timeout_ms=20, startup_timeout_ms=50)
    lock = service._lock  # pyright: ignore[reportPrivateUsage]
    try:
        outcome = service.start_or_forward(LaunchRequest())

        assert outcome is InstanceOutcome.FORWARD_FAILED
        assert len(stopped) == 1

        service.shutdown()

        # 放棄済みthreadを再度停止しない。
        assert len(stopped) == 1
        # 二重起動を防ぐため、endpointもlockも解放しない。
        assert removed == []
        assert lock.isLocked()
    finally:
        # stop_threadを差し替えているため、threadはテスト側で確実に止める。
        for thread in stopped:
            thread.quit()
            assert thread.wait(5_000)
        lock.unlock()


def test_shutdown_waits_even_when_the_thread_already_left_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """isRunning()がFalseでもstop_thread（=wait）を必ず通す。

    run()から戻った直後はisRunning()がFalseでも、OSスレッドの後始末が残っている。
    wait()せずに参照を捨てると、QThread破棄でheap corruptionになる。
    """
    name = unique_server_name()
    service = SingleInstanceService(name, connect_timeout_ms=20, startup_timeout_ms=50)
    assert service.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    thread = service._server_thread  # pyright: ignore[reportPrivateUsage]
    assert thread is not None

    def already_finished(_self: QThread) -> bool:
        return False

    monkeypatch.setattr(type(thread), "isRunning", already_finished)
    stopped: list[object] = []
    real_stop_thread = single_instance.stop_thread

    def recording_stop(target: QThread, **kwargs: object) -> ShutdownOutcome:
        stopped.append(target)
        return real_stop_thread(target, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(single_instance, "stop_thread", recording_stop)

    service.shutdown()

    assert stopped == [thread]

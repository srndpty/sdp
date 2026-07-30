"""QLocalServerによる単一instance IPCを実際のlocal socketで検証する。"""

import json
import struct
import subprocess
import sys
import uuid
from pathlib import Path

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from pytestqt.qtbot import QtBot

from sdp.services.launch_request import LaunchRequest
from sdp.services.single_instance import (
    IPC_VERSION,
    MAX_MESSAGE_SIZE,
    InstanceOutcome,
    SingleInstanceService,
    _encode_frame,  # pyright: ignore[reportPrivateUsage]
)


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
)
service = SingleInstanceService(sys.argv[1], connect_timeout_ms=2_000)
print(service.start_or_forward(request).name, flush=True)
service.shutdown()
"""
    return subprocess.Popen(
        [sys.executable, "-c", code, name, json.dumps([str(path) for path in request.paths])],
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
    primary.start_accepting()
    request = LaunchRequest(((tmp_path / "曲.wav").resolve(),))
    process = forward_in_process(name, request)
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=5_000)
    stdout, stderr = process.communicate(timeout=1)

    assert process.returncode == 0, stderr
    assert stdout.strip() == InstanceOutcome.FORWARDED.name
    assert received == [request]
    primary.shutdown()


def test_multiple_paths_keep_order_duplicates_and_unicode(tmp_path: Path, qtbot: QtBot) -> None:
    """複数pathの順序・重複・UnicodeをIPC往復で維持する。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_accepting()
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


def test_partial_frame_is_not_published_until_complete(tmp_path: Path, qtbot: QtBot) -> None:
    """headerとpayloadの分割受信を蓄積し、完成前には通知しない。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_accepting()
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
    primary.start_accepting()
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
    """不正JSONと未知versionを無視したあとも同じprimaryが有効要求を処理する。"""
    name = unique_server_name()
    primary = SingleInstanceService(name)
    received: list[LaunchRequest] = []
    primary.request_received.connect(received.append)
    assert primary.start_or_forward(LaunchRequest()) is InstanceOutcome.PRIMARY
    primary.start_accepting()
    socket = connect_socket(name, qtbot)
    invalid_json = struct.pack(">I", 1) + b"{"
    unknown = raw_frame({"version": IPC_VERSION + 1, "paths": []})
    valid = LaunchRequest(((tmp_path / "valid.flac").resolve(),))

    socket.write(invalid_json + unknown + _encode_frame(valid))
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
    primary.start_accepting()
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

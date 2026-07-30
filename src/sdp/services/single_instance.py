"""QLocalServerによる単一インスタンス判定と起動要求転送。"""

import getpass
import hashlib
import json
import logging
import os
import struct
import tempfile
import time
from collections import deque
from enum import Enum, auto
from pathlib import Path
from threading import Event
from typing import cast

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Qt, QThread, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from sdp.services.launch_request import LaunchRequest

_logger = logging.getLogger(__name__)

IPC_VERSION = 1
MAX_MESSAGE_SIZE = 256 * 1024
"""4-byte長を除く、IPC payloadの最大byte数。"""

_HEADER_SIZE = 4
_ACK = b"\x06"


class InstanceOutcome(Enum):
    """単一インスタンス判定の結果。"""

    PRIMARY = auto()
    FORWARDED = auto()
    FORWARD_FAILED = auto()


class _ForwardOutcome(Enum):
    NO_SERVER = auto()
    FORWARDED = auto()
    FAILED = auto()


def default_server_name() -> str:
    """ユーザー・Windows sessionごとに衝突しにくい固定server名を返す。"""
    identity = "\0".join(
        (
            getpass.getuser(),
            str(Path.home()),
            os.environ.get("USERDOMAIN", ""),
            os.environ.get("SESSIONNAME", ""),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]
    return f"sdp-{digest}"


class _LocalServerEndpoint(QObject):
    """IPC thread内でserver、socket、受理済み要求queueを所有する。"""

    request_ready = Signal(object)

    def __init__(self, server_name: str) -> None:
        super().__init__()
        self._server_name = server_name
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept_pending_connections)
        self._sockets: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._pending_requests: deque[LaunchRequest] = deque()
        self._delivery_enabled = False

    def listen(self) -> bool:
        """server名をlistenし、結果を返す。"""
        return self._server.listen(self._server_name)

    @property
    def error_string(self) -> str:
        return self._server.errorString()

    @Slot()
    def start_delivery(self) -> None:
        """GUI側のhandler準備後に、受理済み要求の通知を開始する。"""
        if self._delivery_enabled:
            return
        self._delivery_enabled = True
        self._deliver_pending_requests()

    def shutdown(self) -> None:
        """IPC thread内でsocketとserverを閉じる。"""
        for socket in tuple(self._sockets):
            self._discard_socket(socket)
        if self._server.isListening():
            self._server.close()
        self._pending_requests.clear()

    @Slot()
    def _accept_pending_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:  # pyright: ignore[reportUnnecessaryComparison]
                continue
            socket.setReadBufferSize(MAX_MESSAGE_SIZE + _HEADER_SIZE)
            self._sockets.add(socket)
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda current=socket: self._read_socket(current))
            socket.disconnected.connect(lambda current=socket: self._discard_socket(current))
            self._read_socket(socket)

    def _read_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._buffers:
            return
        buffer = self._buffers[socket]
        buffer.extend(bytes(socket.readAll().data()))
        while True:
            if len(buffer) < _HEADER_SIZE:
                return
            payload_size = struct.unpack(">I", buffer[:_HEADER_SIZE])[0]
            if payload_size == 0 or payload_size > MAX_MESSAGE_SIZE:
                _logger.warning("不正な単一instance IPC message sizeを拒否しました")
                socket.disconnectFromServer()
                return
            frame_size = _HEADER_SIZE + payload_size
            if len(buffer) < frame_size:
                return
            payload = bytes(buffer[_HEADER_SIZE:frame_size])
            del buffer[:frame_size]
            try:
                request = _decode_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                _logger.exception("不正な単一instance IPC messageを無視しました")
                continue
            # ACKはplaylist適用成功ではなく、primaryのqueueが検証済み
            # 要求を所有したことを表す。queue追加より先に返さない。
            self._pending_requests.append(request)
            self._deliver_pending_requests()
            socket.write(_ACK)
            socket.flush()

    def _deliver_pending_requests(self) -> None:
        if not self._delivery_enabled:
            return
        while self._pending_requests:
            self.request_ready.emit(self._pending_requests.popleft())

    def _discard_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._sockets:
            return
        self._sockets.discard(socket)
        self._buffers.pop(socket, None)
        socket.abort()


class _ServerThread(QThread):
    """composition構築中もIPCを受理できる専用event loop。"""

    request_ready = Signal(object)
    delivery_requested = Signal()

    def __init__(self, server_name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server_name = server_name
        self._ready = Event()
        self._listen_succeeded = False
        self._error_string = ""

    @property
    def listen_succeeded(self) -> bool:
        return self._listen_succeeded

    @property
    def error_string(self) -> str:
        return self._error_string

    def wait_until_ready(self, timeout_ms: int) -> bool:
        """listen成否が確定するまで待つ。"""
        return self._ready.wait(timeout_ms / 1_000.0)

    def run(self) -> None:
        """IPC endpointをこのthread上で生成してevent loopを回す。"""
        endpoint = _LocalServerEndpoint(self._server_name)
        endpoint.request_ready.connect(self.request_ready)
        self.delivery_requested.connect(endpoint.start_delivery)
        self._listen_succeeded = endpoint.listen()
        self._error_string = endpoint.error_string
        self._ready.set()
        if self._listen_succeeded:
            self.exec()
        endpoint.shutdown()


class SingleInstanceService(QObject):
    """primary判定、要求転送、受理queue、server寿命を所有する。"""

    request_received = Signal(object)
    """検証済み :class:`LaunchRequest` をprimaryのGUI threadへ通知する。"""

    def __init__(
        self,
        server_name: str,
        *,
        connect_timeout_ms: int = 2_000,
        startup_timeout_ms: int = 5_000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not server_name:
            raise ValueError("server_nameは空にできません")
        self._server_name = server_name
        self._connect_timeout_ms = max(1, connect_timeout_ms)
        self._startup_timeout_ms = max(self._connect_timeout_ms, startup_timeout_ms)
        self._lock = QLockFile(str(self._lock_path(server_name)))
        # 生きているprimaryを経過時間だけでstale扱いしない。PID消滅はQtが判定する。
        self._lock.setStaleLockTime(0)
        self._server_thread: _ServerThread | None = None
        self._primary = False
        self._delivery_started = False
        self._shutdown = False

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def is_primary(self) -> bool:
        return self._primary and not self._shutdown

    def start_or_forward(self, request: LaunchRequest) -> InstanceOutcome:
        """既存primaryへ転送するか、排他lockを取ってprimaryになる。"""
        if self._shutdown:
            raise RuntimeError("shutdown後のSingleInstanceServiceは再利用できません")

        forwarded = self._forward(request, self._connect_timeout_ms)
        if forwarded is _ForwardOutcome.FORWARDED:
            return InstanceOutcome.FORWARDED
        if forwarded is _ForwardOutcome.FAILED:
            return InstanceOutcome.FORWARD_FAILED

        if self._lock.tryLock(0):
            return self._listen_as_primary()

        # primaryがlock取得直後でlisten前の可能性がある。二重起動せず接続を待つ。
        deadline = time.monotonic() + self._startup_timeout_ms / 1_000.0
        while time.monotonic() < deadline:
            remaining_ms = max(1, round((deadline - time.monotonic()) * 1_000))
            forwarded = self._forward(request, min(100, remaining_ms))
            if forwarded is _ForwardOutcome.FORWARDED:
                return InstanceOutcome.FORWARDED
            if forwarded is _ForwardOutcome.FAILED:
                return InstanceOutcome.FORWARD_FAILED

        # QtがPID消滅等からstaleと確認できたlockだけを除去する。
        if self._lock.removeStaleLockFile() and self._lock.tryLock(0):
            return self._listen_as_primary()

        _logger.error("既存instanceへ接続できず、排他lockも取得できませんでした")
        return InstanceOutcome.FORWARD_FAILED

    def start_delivery(self) -> None:
        """Windowとhandlerの準備後に、受理済み要求のGUI通知を開始する。"""
        if not self.is_primary or self._delivery_started:
            return
        thread = self._server_thread
        if thread is None:
            return
        self._delivery_started = True
        thread.delivery_requested.emit()

    def shutdown(self) -> None:
        """socket、server thread、endpoint、lockを解放する（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        thread = self._server_thread
        if thread is not None and thread.isRunning():
            thread.quit()
            if not thread.wait(self._startup_timeout_ms):
                _logger.warning(
                    "単一instance IPC threadが%dms以内に終了しません。"
                    "安全なQObject破棄のため終了まで待機します。",
                    self._startup_timeout_ms,
                )
                thread.wait()
        self._server_thread = None
        if self._primary:
            QLocalServer.removeServer(self._server_name)
            self._lock.unlock()
        self._primary = False
        self._delivery_started = False

    def _listen_as_primary(self) -> InstanceOutcome:
        if not self._start_server_thread():
            # 排他lockを所有しているため、同名endpointはstaleと安全に判断できる。
            _logger.warning("staleな単一instance server endpointを除去して再試行します")
            QLocalServer.removeServer(self._server_name)
            if not self._start_server_thread():
                error = "" if self._server_thread is None else self._server_thread.error_string
                _logger.error("単一instance serverを開始できません: %s", error)
                self._lock.unlock()
                return InstanceOutcome.FORWARD_FAILED
        self._primary = True
        return InstanceOutcome.PRIMARY

    def _start_server_thread(self) -> bool:
        thread = _ServerThread(self._server_name, self)
        thread.request_ready.connect(
            self._publish_request,
            Qt.ConnectionType.QueuedConnection,
        )
        self._server_thread = thread
        thread.start()
        if not thread.wait_until_ready(self._startup_timeout_ms):
            _logger.error("単一instance IPC threadのlisten準備がtimeoutしました")
            thread.quit()
            thread.wait()
            return False
        if not thread.listen_succeeded:
            thread.wait()
            return False
        return True

    @Slot(object)
    def _publish_request(self, request: object) -> None:
        if self._shutdown or not isinstance(request, LaunchRequest):
            return
        self.request_received.emit(request)

    def _forward(self, request: LaunchRequest, timeout_ms: int) -> _ForwardOutcome:
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(timeout_ms):
            return _ForwardOutcome.NO_SERVER
        try:
            frame = _encode_frame(request)
            if socket.write(frame) != len(frame):
                _logger.error("既存instanceへの起動要求書き込みに失敗しました")
                return _ForwardOutcome.FAILED
            socket.flush()
            if (
                not socket.waitForReadyRead(self._startup_timeout_ms)
                or bytes(socket.read(1).data()) != _ACK
            ):
                _logger.error("既存instanceが起動要求をqueueへ受理できませんでした")
                return _ForwardOutcome.FAILED
        except ValueError:
            _logger.exception("起動要求がIPCのサイズ上限を超えています")
            return _ForwardOutcome.FAILED
        else:
            return _ForwardOutcome.FORWARDED
        finally:
            socket.disconnectFromServer()
            if socket.state() is not QLocalSocket.LocalSocketState.UnconnectedState:
                socket.waitForDisconnected(timeout_ms)

    @staticmethod
    def _lock_path(server_name: str) -> Path:
        temp = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
        directory = Path(temp) if temp else Path(tempfile.gettempdir())
        digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest()[:24]
        return directory / f"sdp-{digest}.lock"


def _encode_frame(request: LaunchRequest) -> bytes:
    document = {
        "version": IPC_VERSION,
        "paths": [str(path) for path in request.paths],
        "ignored_arguments": list(request.ignored_arguments),
        "activate_window": request.activate_window,
    }
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(payload) <= MAX_MESSAGE_SIZE:
        raise ValueError("IPC messageがサイズ上限を超えています")
    return struct.pack(">I", len(payload)) + payload


def _decode_payload(payload: bytes) -> LaunchRequest:
    parsed: object = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError("IPC payloadはobjectである必要があります")
    document = cast("dict[str, object]", parsed)
    version = document.get("version")
    if type(version) is not int or version != IPC_VERSION:
        raise ValueError(f"未対応のIPC versionです: {version!r}")
    raw_paths = document.get("paths")
    raw_ignored = document.get("ignored_arguments", [])
    activate_window = document.get("activate_window")
    if not isinstance(raw_paths, list) or not isinstance(raw_ignored, list):
        raise TypeError("IPCのpathsとignored_argumentsは配列である必要があります")
    if type(activate_window) is not bool:
        raise TypeError("IPCのactivate_windowはboolである必要があります")
    path_values = cast("list[object]", raw_paths)
    ignored_values = cast("list[object]", raw_ignored)
    if not all(isinstance(value, str) for value in path_values):
        raise TypeError("IPCのpathは文字列である必要があります")
    if not all(isinstance(value, str) for value in ignored_values):
        raise TypeError("IPCのignored argumentは文字列である必要があります")
    paths = tuple(Path(value) for value in cast("list[str]", path_values))
    ignored = tuple(cast("list[str]", ignored_values))
    return LaunchRequest(paths, ignored, activate_window)

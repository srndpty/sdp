"""QLocalServerによる単一インスタンス判定と起動要求転送。"""

import getpass
import hashlib
import json
import logging
import os
import struct
import tempfile
import time
from enum import Enum, auto
from pathlib import Path
from typing import cast

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Signal
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


class SingleInstanceService(QObject):
    """primary判定、要求転送、受信、server寿命を所有する。"""

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
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._lock = QLockFile(str(self._lock_path(server_name)))
        # 生きているprimaryを経過時間だけでstale扱いしない。PID消滅はQtが判定する。
        self._lock.setStaleLockTime(0)
        self._primary = False
        self._accepting = False
        self._shutdown = False
        self._sockets: set[QLocalSocket] = set()
        self._buffers: dict[QLocalSocket, bytearray] = {}

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

    def start_accepting(self) -> None:
        """Window表示後に受信通知を開始する。開始前のpending接続も回収する。"""
        if not self.is_primary or self._accepting:
            return
        self._accepting = True
        self._server.newConnection.connect(self._accept_pending_connections)
        self._accept_pending_connections()

    def shutdown(self) -> None:
        """socket、server、Signal接続、lockを解放する（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        if self._accepting:
            self._server.newConnection.disconnect(self._accept_pending_connections)
            self._accepting = False
        for socket in tuple(self._sockets):
            self._discard_socket(socket)
        if self._server.isListening():
            self._server.close()
        if self._primary:
            QLocalServer.removeServer(self._server_name)
            self._lock.unlock()
        self._primary = False

    def _listen_as_primary(self) -> InstanceOutcome:
        if not self._server.listen(self._server_name):
            # 排他lockを所有しているため、同名endpointはstaleと安全に判断できる。
            _logger.warning("staleな単一instance server endpointを除去して再試行します")
            QLocalServer.removeServer(self._server_name)
            if not self._server.listen(self._server_name):
                _logger.error("単一instance serverを開始できません: %s", self._server.errorString())
                self._lock.unlock()
                return InstanceOutcome.FORWARD_FAILED
        self._primary = True
        return InstanceOutcome.PRIMARY

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
                _logger.error("既存instanceが起動要求を受理できませんでした")
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
            self.request_received.emit(request)
            socket.write(_ACK)
            socket.flush()

    def _discard_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._sockets:
            return
        self._sockets.discard(socket)
        self._buffers.pop(socket, None)
        socket.abort()

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
    if not isinstance(raw_paths, list) or not isinstance(raw_ignored, list):
        raise TypeError("IPCのpathsとignored_argumentsは配列である必要があります")
    path_values = cast("list[object]", raw_paths)
    ignored_values = cast("list[object]", raw_ignored)
    if not all(isinstance(value, str) for value in path_values):
        raise TypeError("IPCのpathは文字列である必要があります")
    if not all(isinstance(value, str) for value in ignored_values):
        raise TypeError("IPCのignored argumentは文字列である必要があります")
    paths = tuple(Path(value) for value in cast("list[str]", path_values))
    ignored = tuple(cast("list[str]", ignored_values))
    return LaunchRequest(paths, ignored)

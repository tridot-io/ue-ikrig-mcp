import json
import uuid
import time
import socket
import logging
import threading
import errno
import os
import atexit
from typing import Optional, Any

# Protocol constants
_PROTOCOL_VERSION = 1
_PROTOCOL_MAGIC = 'ue_py'
_TYPE_PING = 'ping'
_TYPE_PONG = 'pong'
_TYPE_OPEN_CONNECTION = 'open_connection'
_TYPE_CLOSE_CONNECTION = 'close_connection'
_TYPE_COMMAND = 'command'
_TYPE_COMMAND_RESULT = 'command_result'

_NODE_PING_SECONDS = 1
_NODE_TIMEOUT_SECONDS = 5
_DEFAULT_RECEIVE_BUFFER_SIZE = 8192

def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    normalized = raw.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    return default


MULTICAST_GROUP = (os.environ.get('UE_MULTICAST_GROUP', '239.0.0.1'), _int_env('UE_MULTICAST_PORT', 6766))
MULTICAST_BIND_ADDRESS = os.environ.get('UE_MULTICAST_BIND', '0.0.0.0')
COMMAND_ENDPOINT = (os.environ.get('UE_COMMAND_HOST', '0.0.0.0'), _int_env('UE_COMMAND_PORT', 6777))
COMMAND_PORT_STRICT = _bool_env('UE_COMMAND_PORT_STRICT', False)

_MCP_RESULT_SENTINEL = '__MCP_RESULT__'

logger = logging.getLogger(__name__)


class UENotRunningError(Exception):
    """Raised when no Unreal Editor instances are discovered."""


class UEConnectionError(Exception):
    """Raised when a connection attempt to Unreal Editor fails."""


class UETimeoutError(Exception):
    """Raised when an operation times out waiting for Unreal Editor."""


def is_local_bind_error(error: BaseException) -> bool:
    """Return True only for a local TCP listen/bind port collision."""
    if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
        return True
    message = str(error).lower()
    if 'eaddrinuse' in message:
        return True
    return 'address already in use' in message and ('listen' in message or 'bind' in message)


def should_attempt_command_port_fallback(error: BaseException, strict: bool) -> bool:
    return not strict and is_local_bind_error(error)


def _callback_host_for(listen_host: str) -> str:
    if listen_host in ('', '0.0.0.0'):
        return '127.0.0.1'
    if listen_host == '::':
        return '::1'
    return listen_host


def allocate_fallback_command_endpoint(configured_endpoint: tuple[str, int]) -> tuple[str, int]:
    """Reserve and release an ephemeral port on the configured listen host."""
    host = configured_endpoint[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return host, int(sock.getsockname()[1])
    finally:
        sock.close()


class _Message:
    def __init__(self, type_: Optional[str], source: Optional[str], dest: Optional[str] = None, data: Optional[dict] = None):
        self.type_ = type_
        self.source = source
        self.dest = dest
        self.data = data

    def passes_receive_filter(self, node_id: str) -> bool:
        return self.source != node_id and (not self.dest or self.dest == node_id)

    def to_json_bytes(self) -> bytes:
        if not self.type_:
            raise ValueError('"type" cannot be empty!')
        if not self.source:
            raise ValueError('"source" cannot be empty!')
        obj: dict = {
            'version': _PROTOCOL_VERSION,
            'magic': _PROTOCOL_MAGIC,
            'type': self.type_,
            'source': self.source,
        }
        if self.dest:
            obj['dest'] = self.dest
        if self.data:
            obj['data'] = self.data
        return json.dumps(obj, ensure_ascii=False).encode('utf-8')

    def from_json_bytes(self, data: bytes) -> bool:
        try:
            obj = json.loads(data.decode('utf-8'))
            if obj['version'] != _PROTOCOL_VERSION:
                raise ValueError('Bad protocol version')
            if obj['magic'] != _PROTOCOL_MAGIC:
                raise ValueError('Bad protocol magic')
            self.type_ = obj['type']
            self.source = obj['source']
            self.dest = obj.get('dest')
            self.data = obj.get('data')
        except Exception as e:
            logger.debug('Failed to parse message: %s', e)
            return False
        return True


class _NodeSet:
    def __init__(self):
        self._nodes: dict = {}
        self._lock = threading.RLock()

    @property
    def remote_nodes(self) -> list:
        with self._lock:
            result = []
            for node_id, (data, last_pong) in self._nodes.items():
                entry = dict(data)
                entry['node_id'] = node_id
                result.append(entry)
            return result

    def update(self, node_id: str, data: dict) -> None:
        now = time.time()
        with self._lock:
            self._nodes[node_id] = (data, now)

    def timeout(self) -> None:
        now = time.time()
        with self._lock:
            for node_id in list(self._nodes):
                _, last_pong = self._nodes[node_id]
                if (last_pong + _NODE_TIMEOUT_SECONDS) < now:
                    del self._nodes[node_id]


class UEConnection:
    """
    Manages discovery and command connection to an Unreal Editor instance
    running the Python remote execution plugin.
    """

    def __init__(
        self,
        command_endpoint: Optional[tuple[str, int]] = None,
        active_command_endpoint: Optional[tuple[str, int]] = None,
        strict_command_port: Optional[bool] = None,
        fallback_used: bool = False,
        fallback_reason: Optional[str] = None,
    ):
        self._node_id = str(uuid.uuid4())
        self._nodes = _NodeSet()
        self._broadcast_socket: Optional[socket.socket] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_ping: Optional[float] = None

        self._remote_node_id: Optional[str] = None
        self._command_listen_socket: Optional[socket.socket] = None
        self._command_channel_socket: Optional[socket.socket] = None
        self._configured_command_endpoint = command_endpoint or COMMAND_ENDPOINT
        self._active_command_endpoint = active_command_endpoint or self._configured_command_endpoint
        self._strict_command_port = COMMAND_PORT_STRICT if strict_command_port is None else strict_command_port
        self._fallback_used = fallback_used
        self._fallback_reason = fallback_reason

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def start_discovery(self) -> None:
        """Open UDP broadcast socket and start the discovery listen thread."""
        self._running = True
        self._last_ping = None
        self._nodes = _NodeSet()
        self._init_broadcast_socket()
        self._listen_thread = threading.Thread(target=self._run_broadcast_listen, daemon=True)
        self._listen_thread.start()

    def _init_broadcast_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if hasattr(socket, 'SO_REUSEPORT'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((MULTICAST_BIND_ADDRESS, MULTICAST_GROUP[1]))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(MULTICAST_BIND_ADDRESS))
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(MULTICAST_GROUP[0]) + socket.inet_aton(MULTICAST_BIND_ADDRESS),
        )
        sock.settimeout(0.1)
        self._broadcast_socket = sock

    def _run_broadcast_listen(self) -> None:
        while self._running:
            while True:
                try:
                    data = b''
                    while True:
                        part = self._broadcast_socket.recv(_DEFAULT_RECEIVE_BUFFER_SIZE)
                        data += part
                        if len(part) < _DEFAULT_RECEIVE_BUFFER_SIZE:
                            break
                except socket.timeout:
                    data = None
                if data:
                    self._handle_broadcast_data(data)
                else:
                    break
            now = time.time()
            self._broadcast_ping(now)
            self._nodes.timeout()
            time.sleep(0.1)

    def _broadcast_ping(self, now: float) -> None:
        if not self._last_ping or (self._last_ping + _NODE_PING_SECONDS) < now:
            self._last_ping = now
            self._send_broadcast(_Message(_TYPE_PING, self._node_id))

    def _send_broadcast(self, msg: _Message) -> None:
        if self._broadcast_socket:
            self._broadcast_socket.sendto(msg.to_json_bytes(), MULTICAST_GROUP)

    def _handle_broadcast_data(self, data: bytes) -> None:
        msg = _Message(None, None)
        if msg.from_json_bytes(data):
            if not msg.passes_receive_filter(self._node_id):
                return
            if msg.type_ == _TYPE_PONG:
                self._nodes.update(msg.source, msg.data or {})

    def get_remote_nodes(self) -> list:
        """Return the currently discovered remote editor nodes."""
        return self._nodes.remote_nodes

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, node_id: Optional[str] = None, timeout: float = 5.0) -> None:
        """
        Connect to a discovered Unreal Editor node.

        Args:
            node_id: Specific node ID to connect to. If None, uses the first
                     discovered node (waiting up to `timeout` seconds).
            timeout: Seconds to wait for node discovery if node_id is None.

        Raises:
            UENotRunningError: No editor nodes found within timeout.
            UEConnectionError: TCP handshake with the editor failed.
        """
        if not self._running:
            self.start_discovery()

        if self.is_connected() and (node_id is None or node_id == self._remote_node_id):
            return
        if self.is_connected():
            self._cleanup_command_sockets()

        # Resolve target node
        if node_id is None:
            deadline = time.time() + timeout
            while time.time() < deadline:
                nodes = self.get_remote_nodes()
                if nodes:
                    node_id = nodes[0]['node_id']
                    break
                time.sleep(0.2)
            if node_id is None:
                raise UENotRunningError('No Unreal Editor instances discovered within timeout.')

        self._remote_node_id = node_id

        try:
            self._open_command_channel(node_id, self._active_command_endpoint)
            return
        except OSError as e:
            self._cleanup_command_sockets()
            if not should_attempt_command_port_fallback(e, self._strict_command_port):
                raise UEConnectionError(self._format_connect_error('TCP command listener failed', e)) from e

        fallback_endpoint = allocate_fallback_command_endpoint(self._configured_command_endpoint)
        self._active_command_endpoint = fallback_endpoint
        self._fallback_used = True
        self._fallback_reason = (
            f'Configured command endpoint '
            f'{self._configured_command_endpoint[0]}:{self._configured_command_endpoint[1]} '
            f'was unavailable (EADDRINUSE)'
        )
        try:
            self._open_command_channel(node_id, fallback_endpoint)
            return
        except Exception as e:
            self._cleanup_command_sockets()
            raise UEConnectionError(self._format_connect_error('TCP command fallback endpoint failed', e)) from e

    def _open_command_channel(self, node_id: str, endpoint: tuple[str, int]) -> None:
        listen_host, listen_port = endpoint
        self._cleanup_command_sockets()

        self._command_listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self._command_listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

        try:
            self._command_listen_socket.bind((listen_host, listen_port))
            self._command_listen_socket.listen(1)
            self._command_listen_socket.settimeout(5)

            callback_ip = _callback_host_for(listen_host)
            for _ in range(6):
                self._send_broadcast(_Message(_TYPE_OPEN_CONNECTION, self._node_id, node_id, {
                    'command_ip': callback_ip,
                    'command_port': listen_port,
                }))
                try:
                    self._command_channel_socket = self._command_listen_socket.accept()[0]
                    self._command_channel_socket.setblocking(True)
                    return
                except socket.timeout:
                    continue
        except Exception:
            self._cleanup_command_sockets()
            raise

        self._cleanup_command_sockets()
        raise UEConnectionError('Unreal Editor did not connect back within timeout.')

    def _format_connect_error(self, prefix: str, error: BaseException) -> str:
        status = self.get_status()
        return (
            f'{prefix} '
            f'(configured {status["configured_command_endpoint"][0]}:{status["configured_command_endpoint"][1]}, '
            f'active {status["active_command_endpoint"][0]}:{status["active_command_endpoint"][1]}, '
            f'fallback {status["fallback_used"]}). Underlying: {error}'
        )

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute(self, code: str, mode: str = 'ExecuteFile') -> dict:
        """
        Execute Python code in the connected Unreal Editor.

        Returns:
            dict with keys:
                success (bool): Whether execution succeeded.
                result  (str):  Result string from UE.
                output  (str):  Captured stdout/log output.
                parsed  (any):  Deserialized JSON after __MCP_RESULT__ sentinel,
                                or None if not found.

        Raises:
            UEConnectionError: Not connected.
        """
        if not self.is_connected():
            raise UEConnectionError('Not connected to Unreal Editor. Call connect() first.')

        msg = _Message(_TYPE_COMMAND, self._node_id, self._remote_node_id, {
            'command': code,
            'unattended': True,
            'exec_mode': mode,
        })
        try:
            self._command_channel_socket.sendall(msg.to_json_bytes())

            # Receive full response
            data = b''
            while True:
                part = self._command_channel_socket.recv(_DEFAULT_RECEIVE_BUFFER_SIZE)
                data += part
                if len(part) < _DEFAULT_RECEIVE_BUFFER_SIZE:
                    break
        except OSError as e:
            self._cleanup_command_sockets()
            raise UEConnectionError(f'Command channel socket failed: {e}') from e

        if not data:
            raise UEConnectionError('No response received from Unreal Editor.')

        response = _Message(None, None)
        if not response.from_json_bytes(data):
            raise UEConnectionError('Failed to parse response from Unreal Editor.')
        if not response.passes_receive_filter(self._node_id) or response.type_ != _TYPE_COMMAND_RESULT:
            raise UEConnectionError('Unexpected response type from Unreal Editor.')

        result_data = response.data or {}
        success = bool(result_data.get('success', False))
        result_str = str(result_data.get('result', ''))
        output_str = str(result_data.get('output', ''))

        # Parse __MCP_RESULT__ sentinel
        parsed = None
        combined = output_str + result_str
        sentinel_idx = combined.find(_MCP_RESULT_SENTINEL)
        if sentinel_idx != -1:
            json_str = combined[sentinel_idx + len(_MCP_RESULT_SENTINEL):].strip()
            try:
                parsed = json.loads(json_str)
            except json.JSONDecodeError:
                logger.debug('Failed to parse MCP result JSON: %s', json_str[:200])

        return {
            'success': success,
            'result': result_str,
            'output': output_str,
            'parsed': parsed,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Close the command connection and stop discovery."""
        if self._remote_node_id and self._broadcast_socket:
            try:
                self._send_broadcast(_Message(_TYPE_CLOSE_CONNECTION, self._node_id, self._remote_node_id))
            except Exception:
                pass
        self._cleanup_command_sockets()
        self._running = False
        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
            self._listen_thread = None
        if self._broadcast_socket:
            try:
                self._broadcast_socket.close()
            except Exception:
                pass
            self._broadcast_socket = None
        self._remote_node_id = None
        self._active_command_endpoint = self._configured_command_endpoint
        self._fallback_used = False
        self._fallback_reason = None

    def _cleanup_command_sockets(self) -> None:
        if self._command_channel_socket:
            try:
                self._command_channel_socket.close()
            except Exception:
                pass
            self._command_channel_socket = None
        if self._command_listen_socket:
            try:
                self._command_listen_socket.close()
            except Exception:
                pass
            self._command_listen_socket = None

    def is_connected(self) -> bool:
        """Return True if a TCP command channel is open."""
        return self._command_channel_socket is not None

    def get_connected_node_id(self) -> Optional[str]:
        """Return the node ID of the currently connected editor, or None."""
        return self._remote_node_id if self.is_connected() else None

    def get_status(self) -> dict[str, Any]:
        """Return connection state plus configured/active command endpoint diagnostics."""
        status = {
            'connected': self.is_connected(),
            'node_id': self.get_connected_node_id(),
            'configured_command_endpoint': list(self._configured_command_endpoint),
            'active_command_endpoint': list(self._active_command_endpoint),
            'fallback_used': self._fallback_used,
        }
        if self._fallback_reason:
            status['fallback_reason'] = self._fallback_reason
        return status


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_connection: Optional[UEConnection] = None
_connection_lock = threading.Lock()
_cleanup_registered = False


def get_connection() -> UEConnection:
    """Return the module-level singleton UEConnection, creating it if needed."""
    global _connection
    with _connection_lock:
        if _connection is None:
            _connection = UEConnection()
        return _connection


def disconnect_connection() -> None:
    """Disconnect and clear the module-level singleton, if one exists."""
    global _connection
    with _connection_lock:
        if _connection is not None:
            _connection.disconnect()
            _connection = None


def register_process_cleanup() -> None:
    """Ensure stdio MCP process teardown releases the Unreal command channel."""
    global _cleanup_registered
    if _cleanup_registered:
        return
    _cleanup_registered = True
    atexit.register(disconnect_connection)

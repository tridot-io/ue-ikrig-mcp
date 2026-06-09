import json
import re
import uuid
import time
import socket
import logging
import threading
import errno
import os
import sys
import atexit
import shutil
import subprocess
import tempfile
from importlib import metadata as importlib_metadata
from pathlib import Path
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
_WINDOWS_BRIDGE_RESULT_PREFIX = '__UE_IKRIG_MCP_BRIDGE_RESULT__'
_PACKAGE_NAME = 'ue-ikrig-mcp'

_WINDOWS_BRIDGE_SCRIPT = r'''
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid

PROTOCOL_VERSION = 1
PROTOCOL_MAGIC = "ue_py"
TYPE_PING = "ping"
TYPE_PONG = "pong"
TYPE_OPEN_CONNECTION = "open_connection"
TYPE_CLOSE_CONNECTION = "close_connection"
TYPE_COMMAND = "command"
TYPE_COMMAND_RESULT = "command_result"
BUFFER_SIZE = 8192
RESULT_PREFIX = "__UE_IKRIG_MCP_BRIDGE_RESULT__"

# Persistent daemon state: one bridge process can keep TCP command channels
# open to editor nodes across many requests.
SOURCE_ID = str(uuid.uuid4())
CHANNELS = {}


def emit(payload):
    # ensure_ascii=True so the sentinel line survives any pipe codepage
    # (Windows pipes default to the ANSI codepage, e.g. cp949).
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=True), flush=True)


def make_message(type_, source, dest=None, data=None):
    obj = {
        "version": PROTOCOL_VERSION,
        "magic": PROTOCOL_MAGIC,
        "type": type_,
        "source": source,
    }
    if dest:
        obj["dest"] = dest
    if data:
        obj["data"] = data
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def parse_message(data):
    obj = json.loads(data.decode("utf-8"))
    if obj["version"] != PROTOCOL_VERSION:
        raise ValueError("Bad protocol version")
    if obj["magic"] != PROTOCOL_MAGIC:
        raise ValueError("Bad protocol magic")
    return obj


def passes_receive_filter(msg, source_id):
    return msg.get("source") != source_id and (not msg.get("dest") or msg.get("dest") == source_id)


def local_ipv4_candidates():
    addresses = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = info[4][0]
        if address and address not in addresses:
            addresses.append(address)
    route_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        route_probe.connect(("8.8.8.8", 80))
        address = route_probe.getsockname()[0]
        if address and address not in addresses:
            addresses.append(address)
    except OSError:
        pass
    finally:
        route_probe.close()
    return addresses


def target_candidates(group_host, port, configured_targets=None):
    targets = []

    def add(host):
        if host and (host, port) not in targets:
            targets.append((host, port))

    add(group_host)
    add("127.0.0.1")
    for item in configured_targets or []:
        if isinstance(item, (list, tuple)) and item:
            add(str(item[0]))
        elif isinstance(item, str):
            add(item)
    for address in local_ipv4_candidates():
        add(address)
    return targets


def open_udp_socket(group_host, port, ttl, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("0.0.0.0", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(ttl))
    try:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(group_host) + socket.inet_aton("0.0.0.0"),
        )
    except OSError:
        # Direct unicast probes can still reach editors bound on loopback or a
        # concrete Windows adapter even if multicast membership is rejected.
        pass
    sock.settimeout(timeout)
    return sock


def discover(payload):
    group_host, port = payload.get("group", ["239.0.0.1", 6766])
    port = int(port)
    ttl = int(payload.get("ttl", 1))
    timeout = max(0.1, float(payload.get("timeout", 2.0)))
    # Early-exit: once the first pong arrives, wait only a short settle window
    # for additional editors instead of burning the full timeout.
    settle = max(0.0, float(payload.get("settle", 0.25)))
    source_id = payload.get("source_id") or str(uuid.uuid4())
    targets = target_candidates(group_host, port, payload.get("targets"))
    nodes = {}
    parse_errors = []
    first_pong_at = None
    sock = open_udp_socket(group_host, port, ttl, 0.05)
    try:
        deadline = time.time() + timeout
        next_send = 0.0
        ping = make_message(TYPE_PING, source_id)
        while time.time() < deadline:
            now = time.time()
            if first_pong_at is not None and (now - first_pong_at) >= settle:
                break
            if now >= next_send:
                for target in targets:
                    try:
                        sock.sendto(ping, target)
                    except OSError:
                        pass
                next_send = now + 0.25
            try:
                data, source_address = sock.recvfrom(BUFFER_SIZE)
            except socket.timeout:
                continue
            try:
                msg = parse_message(data)
            except Exception as exc:
                parse_errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if not passes_receive_filter(msg, source_id) or msg.get("type") != TYPE_PONG:
                continue
            node_id = msg.get("source")
            if not node_id:
                continue
            node = dict(msg.get("data") or {})
            node["node_id"] = node_id
            node["_source_address"] = list(source_address)
            nodes[node_id] = node
            if first_pong_at is None:
                first_pong_at = time.time()
    finally:
        sock.close()
    return {
        "ok": bool(nodes),
        "nodes": list(nodes.values()),
        "source_id": source_id,
        "targets": [list(item) for item in targets],
        "parse_errors": parse_errors[-10:],
    }


class ChannelNotDelivered(OSError):
    """The command send failed before it could reach Unreal (safe to retry)."""


class PeerClosedNoData(OSError):
    """The channel closed before any response byte arrived (command unseen)."""


def recv_json_message(conn, timeout):
    """Receive one JSON protocol message, framed by parseability.

    The naive `len(part) < BUFFER_SIZE` heuristic truncates fragmented
    messages and hangs until timeout when a response is an exact multiple
    of the buffer size. Accumulate and return as soon as the buffer parses.
    """
    deadline = time.time() + max(0.1, timeout)
    data = b""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise socket.timeout("timed out waiting for command result")
        conn.settimeout(remaining)
        part = conn.recv(BUFFER_SIZE)
        if not part:
            if data:
                break
            raise PeerClosedNoData("connection closed by Unreal Editor")
        data += part
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            continue
    return json.loads(data.decode("utf-8"))


def resolve_node_id(payload):
    """Return (node_id, error_dict). Discovers the first editor when unset."""
    node_id = payload.get("node_id")
    if node_id:
        return node_id, None
    timeout = max(0.1, float(payload.get("timeout", 30.0)))
    discovery = discover({
        "group": payload.get("group", ["239.0.0.1", 6766]),
        "ttl": int(payload.get("ttl", 1)),
        "timeout": min(timeout, 5.0),
        "settle": payload.get("settle", 0.25),
        "source_id": payload.get("source_id"),
        "targets": payload.get("targets"),
    })
    if not discovery.get("nodes"):
        return None, {
            "ok": False,
            "error": "No Unreal Editor instances discovered from Windows bridge.",
            "discovery": discovery,
            "delivered": False,
        }
    return discovery["nodes"][0]["node_id"], None


def open_command_channel(payload, node_id):
    """Open a TCP command channel to a node. Returns (socket, error_dict)."""
    group_host, port = payload.get("group", ["239.0.0.1", 6766])
    port = int(port)
    ttl = int(payload.get("ttl", 1))
    timeout = max(0.1, float(payload.get("timeout", 30.0)))
    # The handshake is local-machine UDP + TCP accept; it never legitimately
    # needs the full command timeout (which may be minutes).
    handshake_timeout = min(timeout, max(0.5, float(payload.get("handshake_timeout", 10.0))))
    targets = target_candidates(group_host, port, payload.get("targets"))

    udp = open_udp_socket(group_host, port, ttl, 0.05)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.25)
        callback = {
            "command_ip": "127.0.0.1",
            "command_port": listener.getsockname()[1],
        }
        open_message = make_message(TYPE_OPEN_CONNECTION, SOURCE_ID, node_id, callback)
        deadline = time.time() + handshake_timeout
        while time.time() < deadline:
            for target in targets:
                try:
                    udp.sendto(open_message, target)
                except OSError:
                    pass
            try:
                channel, _address = listener.accept()
                channel.setblocking(True)
                return channel, None
            except socket.timeout:
                continue
        return None, {
            "ok": False,
            "error": "Unreal Editor did not connect back to the Windows bridge.",
            "callback": callback,
            "targets": [list(item) for item in targets],
            # The command channel never opened, so nothing was executed: the
            # caller may safely rediscover the current editor and retry.
            "delivered": False,
        }
    finally:
        udp.close()
        listener.close()


def run_command(channel, payload, node_id):
    """Send one command over an open channel and return the bridge result."""
    timeout = max(0.1, float(payload.get("timeout", 30.0)))
    command = make_message(TYPE_COMMAND, SOURCE_ID, node_id, {
        "command": payload.get("code", ""),
        "unattended": True,
        "exec_mode": payload.get("mode", "ExecuteFile"),
    })
    try:
        channel.sendall(command)
    except OSError as exc:
        # Nothing (complete) reached Unreal; callers may retry safely.
        raise ChannelNotDelivered("command send failed: %s" % (exc,))
    response = recv_json_message(channel, timeout)
    if response.get("version") != PROTOCOL_VERSION or response.get("magic") != PROTOCOL_MAGIC:
        raise ValueError("Bad protocol header in command response")
    if not passes_receive_filter(response, SOURCE_ID) or response.get("type") != TYPE_COMMAND_RESULT:
        # Channel-fatal: an off-type frame means this socket is desynced;
        # raising makes the caller drop it instead of caching it one
        # response behind.
        raise ValueError(
            "Unexpected command response from Unreal Editor: %r" % (response.get("type"),)
        )
    return {
        "ok": True,
        "node_id": node_id,
        "result": response.get("data") or {},
    }


def probe_channel(channel):
    """Return True when a cached command channel still looks alive."""
    try:
        previous = channel.gettimeout()
    except OSError:
        return False
    try:
        channel.settimeout(0.05)
        data = channel.recv(1, socket.MSG_PEEK)
    except socket.timeout:
        return True
    except OSError:
        return False
    finally:
        try:
            channel.settimeout(previous)
        except OSError:
            pass
    return data != b""


def drop_channel(node_id):
    channel = CHANNELS.pop(node_id, None)
    if channel is not None:
        try:
            channel.close()
        except OSError:
            pass


def close_all_channels():
    for node_id in list(CHANNELS):
        drop_channel(node_id)


def execute(payload):
    """One-shot execute: open a channel, run the command, close the channel."""
    node_id, error = resolve_node_id(payload)
    if error is not None:
        return error
    channel, error = open_command_channel(payload, node_id)
    if error is not None:
        return error
    try:
        return run_command(channel, payload, node_id)
    except socket.timeout as exc:
        return {"ok": False, "error": "Command timed out: %s" % (exc,)}
    except ChannelNotDelivered as exc:
        # The send failed: nothing (complete) reached Unreal, so the caller may
        # rediscover the current editor and retry safely.
        return {
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "delivered": False,
        }
    except (OSError, ValueError) as exc:
        # PeerClosedNoData (an OSError) and post-send errors land here: the
        # frame may have executed before the failure, so no delivered flag.
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        try:
            channel.close()
        except OSError:
            pass


def daemon_execute(payload):
    """Execute over a persistent channel; reopen once if a cached one is stale.

    Retry discipline: a command is re-sent ONLY when the first attempt provably
    never reached Unreal (send failed, or the peer closed before any response
    byte). Timeouts and post-send failures never retry — the editor may have
    executed (or still be executing) the command, and silently re-running
    non-idempotent code is worse than surfacing the error.
    """
    node_id, error = resolve_node_id(payload)
    if error is not None:
        return error

    channel = CHANNELS.get(node_id)
    opened_fresh = channel is None
    if channel is None:
        channel, error = open_command_channel(payload, node_id)
        if error is not None:
            return error
        CHANNELS[node_id] = channel

    try:
        return run_command(channel, payload, node_id)
    except socket.timeout as exc:
        # The editor may still be running the command on its game thread;
        # never re-send it. Drop the channel so the next call starts clean.
        drop_channel(node_id)
        return {"ok": False, "error": "Command timed out: %s" % (exc,)}
    except ChannelNotDelivered as exc:
        # The send itself failed, so nothing (complete) reached Unreal: safe to
        # retry. On a fresh channel, tell the client it may rediscover+retry;
        # on a cached channel, reopen and retry below.
        drop_channel(node_id)
        if opened_fresh:
            return {
                "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc),
                "delivered": False,
            }
    except PeerClosedNoData as exc:
        # The frame WAS sent and the peer then closed before responding. Unreal
        # may have executed it (side effects) before dying, so its state is
        # unknown - never auto-retry (double-execution guard). No delivered flag.
        drop_channel(node_id)
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    except (OSError, ValueError) as exc:
        # Post-send failure: execution state in Unreal is unknown — never
        # re-send the command.
        drop_channel(node_id)
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    # Cached channel went stale (editor restarted or closed it) without the
    # command being seen: reopen once and retry.
    channel, error = open_command_channel(payload, node_id)
    if error is not None:
        return error
    CHANNELS[node_id] = channel
    try:
        return run_command(channel, payload, node_id)
    except socket.timeout as exc:
        drop_channel(node_id)
        return {"ok": False, "error": "Command timed out: %s" % (exc,)}
    except (OSError, ValueError) as exc:
        drop_channel(node_id)
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def editor_process_check():
    """Is an Unreal Editor process alive on this (Windows) machine?

    Distinguishes 'editor busy on the game thread' (process alive, discovery
    silent) from 'editor gone'. editor_process_alive is None when the check
    itself was impossible (e.g. tasklist unavailable)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor*", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        alive = "UnrealEditor" in (out.stdout or "")
        return {"ok": True, "op": "process_check", "editor_process_alive": alive}
    except Exception as exc:
        return {
            "ok": True,
            "op": "process_check",
            "editor_process_alive": None,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


def handle_request(request):
    op = request.get("op")
    if op == "ping":
        # Channel membership alone is not liveness: probe each cached socket
        # and drop dead ones so a crashed editor never reports as connected.
        for node_id in list(CHANNELS):
            if not probe_channel(CHANNELS[node_id]):
                drop_channel(node_id)
        return {
            "ok": True,
            "op": "ping",
            "pid": os.getpid(),
            "channels": sorted(CHANNELS.keys()),
        }
    if op == "discover":
        return discover(request)
    if op == "execute":
        return daemon_execute(request)
    if op == "process_check":
        return editor_process_check()
    if op == "close":
        close_all_channels()
        return {"ok": True, "op": "close"}
    return {"ok": False, "error": "Unsupported bridge operation: %r" % (op,)}


def daemon_loop():
    """Serve JSON-line requests on stdin until EOF or a shutdown request."""
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get("id")
                if request.get("op") == "shutdown":
                    emit({"id": request_id, "ok": True, "op": "shutdown"})
                    break
                response = handle_request(request)
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "traceback": traceback.format_exc(),
                }
            response["id"] = request_id
            emit(response)
    finally:
        close_all_channels()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--daemon":
        daemon_loop()
        return
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        payload = json.load(f)
    try:
        op = payload.get("op")
        if op == "discover":
            emit(discover(payload))
            return
        if op == "execute":
            emit(execute(payload))
            return
        if op == "process_check":
            emit(editor_process_check())
            return
        emit({"ok": False, "error": f"Unsupported bridge operation: {op!r}"})
    except Exception as exc:
        emit({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })


if __name__ == "__main__":
    main()
'''

_WINDOWS_BRIDGE_LAUNCHER_SCRIPT = (
    "param([string]$ScriptPath, [string]$PayloadPath)\n"
    "$ErrorActionPreference = 'Stop'\n"
    "$py = Get-Command py -ErrorAction SilentlyContinue\n"
    "if ($py) { & $py.Source -3 $ScriptPath $PayloadPath; exit $LASTEXITCODE }\n"
    "$python = Get-Command python -ErrorAction SilentlyContinue\n"
    "if ($python) { & $python.Source $ScriptPath $PayloadPath; exit $LASTEXITCODE }\n"
    "throw 'Windows Python was not found on PATH.'\n"
)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return float(raw)
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


def _write_temp_text(content: str, *, suffix: str, prefix: str) -> str:
    with tempfile.NamedTemporaryFile(
        'w',
        suffix=suffix,
        prefix=prefix,
        delete=False,
        encoding='utf-8',
    ) as temp_file:
        temp_file.write(content)
        return temp_file.name


def _windows_bridge_failure(error: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {'ok': False, 'error': error}
    result.update(details)
    return result


def _split_address_list(raw: Optional[str], default: list[str]) -> list[str]:
    """Parse comma/semicolon separated IPv4 address candidates."""
    if raw is None or raw.strip() == '':
        return list(default)
    parts = raw.replace(';', ',').split(',')
    result: list[str] = []
    for part in parts:
        value = part.strip()
        if value and value not in result:
            result.append(value)
    return result or list(default)


def _dedupe_addresses(addresses: list[Optional[str]]) -> list[str]:
    result: list[str] = []
    for address in addresses:
        value = (address or '').strip()
        if value and value not in result:
            result.append(value)
    return result


def _is_wildcard_host(host: str) -> bool:
    return host in ('', '0.0.0.0', '::')


def _is_wsl(osrelease: Optional[str] = None, proc_version: Optional[str] = None) -> bool:
    """Return True when running inside WSL/WSL2."""
    if osrelease is None:
        try:
            with open('/proc/sys/kernel/osrelease', 'r', encoding='utf-8') as f:
                osrelease = f.read()
        except OSError:
            osrelease = ''
    if proc_version is None:
        try:
            with open('/proc/version', 'r', encoding='utf-8') as f:
                proc_version = f.read()
        except OSError:
            proc_version = ''
    probe = f'{osrelease}\n{proc_version}'.lower()
    return 'microsoft' in probe or 'wsl' in probe


def _read_resolv_nameserver(path: str = '/etc/resolv.conf') -> Optional[str]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('nameserver '):
                    value = stripped.split(None, 1)[1].strip()
                    if value:
                        return value
    except OSError:
        return None
    return None


def _local_ipv4_for_remote(remote_host: str) -> Optional[str]:
    """Infer the local IPv4 selected for a remote route without sending data."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, 80))
        local_ip = sock.getsockname()[0]
        return local_ip if local_ip and local_ip != '0.0.0.0' else None
    except OSError:
        return None
    finally:
        sock.close()


def _hostname_ipv4_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = info[4][0]
        if address and address not in addresses:
            addresses.append(address)
    return addresses


def _infer_wsl_local_ipv4(
    *,
    is_wsl: Optional[bool] = None,
    nameserver: Optional[str] = None,
) -> Optional[str]:
    """Infer the WSL-side local IPv4 that Windows can call back to.

    The `/etc/resolv.conf` nameserver usually points at the Windows host/gateway.
    We use it only as a route target to ask the kernel which local WSL interface
    address would be used; we do not advertise the Windows gateway as the
    callback address.
    """
    if is_wsl is None:
        is_wsl = _is_wsl()
    if not is_wsl:
        return None
    target = nameserver or _read_resolv_nameserver()
    if not target:
        return None
    return _local_ipv4_for_remote(target)


def _infer_wsl_callback_ipv4(
    multicast_host: str,
    *,
    is_wsl: Optional[bool] = None,
    nameserver: Optional[str] = None,
) -> Optional[str]:
    if is_wsl is None:
        is_wsl = _is_wsl()
    if not is_wsl:
        return None
    return (
        _local_ipv4_for_remote(multicast_host)
        or _infer_wsl_local_ipv4(is_wsl=is_wsl, nameserver=nameserver)
    )


def _network_diagnostics(multicast_host: str) -> dict[str, Any]:
    nameserver = _read_resolv_nameserver()
    wsl_detected = _is_wsl()
    return {
        'platform': sys.platform,
        'os_name': os.name,
        'hostname': socket.gethostname(),
        'hostname_ipv4_addresses': _hostname_ipv4_addresses(),
        'wsl_detected': wsl_detected,
        'resolv_nameserver': nameserver,
        'route_ipv4_to_multicast_group': _local_ipv4_for_remote(multicast_host),
        'route_ipv4_to_wsl_nameserver': (
            _local_ipv4_for_remote(nameserver) if nameserver else None
        ),
    }


def _package_diagnostics() -> dict[str, Any]:
    try:
        package_version = importlib_metadata.version(_PACKAGE_NAME)
    except importlib_metadata.PackageNotFoundError:
        package_version = None
    return {
        'name': _PACKAGE_NAME,
        'version': package_version,
        'source_file': __file__,
        'executable': sys.executable,
    }


def _callback_host_for(
    listen_host: str,
    *,
    explicit_host: Optional[str] = None,
    wsl_local_ip: Optional[str] = None,
) -> str:
    explicit = explicit_host.strip() if explicit_host else ''
    if explicit and not _is_wildcard_host(explicit):
        return explicit
    if not _is_wildcard_host(listen_host):
        return listen_host
    if wsl_local_ip:
        return wsl_local_ip
    if listen_host == '::':
        return '::1'
    return '127.0.0.1'


def _callback_host_config_error(explicit_host: Optional[str]) -> Optional[str]:
    explicit = explicit_host.strip() if explicit_host else ''
    if explicit and _is_wildcard_host(explicit):
        return (
            'UE_CALLBACK_HOST cannot be a wildcard address; it will not be '
            'advertised to Unreal. Use a reachable concrete host/IP instead.'
        )
    return None


def _find_powershell_executable() -> Optional[str]:
    powershell = shutil.which('powershell.exe')
    if powershell:
        return powershell
    fallback = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    return fallback if os.path.exists(fallback) else None


def _windows_path_to_wsl_executable_path(path: str) -> str:
    """Return a WSL-executable path for a Windows executable path when obvious."""
    value = path.strip().strip('"')
    if len(value) >= 3 and value[1] == ':' and value[2] in ('\\', '/'):
        drive = value[0].lower()
        tail = value[3:].replace('\\', '/')
        return f'/mnt/{drive}/{tail}'
    return value


def _candidate_windows_python_paths() -> list[tuple[str, str]]:
    """Return explicit and common Windows Python candidates as (source, path)."""
    candidates: list[tuple[str, str]] = []
    for env_name in ('UE_WINDOWS_PYTHON', 'UE_WINDOWS_BRIDGE_PYTHON'):
        configured = os.environ.get(env_name, '').strip()
        if configured:
            candidates.append((env_name, configured))

    cwd = Path.cwd()
    module_path = Path(__file__).resolve()
    repo_candidates = [
        cwd,
        module_path.parents[2] if len(module_path.parents) > 2 else module_path.parent,
    ]
    for root in repo_candidates:
        candidates.append(('repo .venv-win', str(root / '.venv-win' / 'Scripts' / 'python.exe')))

    userprofile = os.environ.get('USERPROFILE', '').strip()
    if userprofile:
        candidates.append((
            'USERPROFILE Python',
            str(Path(_windows_path_to_wsl_executable_path(userprofile)) / 'AppData' / 'Local' / 'Python' / 'bin' / 'python.exe'),
        ))

    username = os.environ.get('USERNAME') or os.environ.get('USER')
    if username:
        candidates.extend([
            (
                'Windows user Python',
                f'/mnt/c/Users/{username}/AppData/Local/Python/bin/python.exe',
            ),
            (
                'Windows Store Python',
                f'/mnt/c/Users/{username}/AppData/Local/Microsoft/WindowsApps/python.exe',
            ),
        ])

    for executable in ('python.exe', 'py.exe'):
        found = shutil.which(executable)
        if found:
            candidates.append((f'PATH {executable}', found))

    return candidates


def _windows_python_command(executable: str) -> list[str]:
    """Return command argv for a Windows Python executable."""
    command = [executable]
    if os.path.basename(executable).lower() == 'py.exe':
        command.append('-3')
    return command


def _is_explicit_windows_python_source(source: str) -> bool:
    return source in {'UE_WINDOWS_PYTHON', 'UE_WINDOWS_BRIDGE_PYTHON'}


def _windows_python_launcher_candidates() -> tuple[list[tuple[list[str], dict[str, Any]]], dict[str, Any]]:
    """Return direct Windows Python launchers plus probe diagnostics."""
    attempts: list[dict[str, Any]] = []
    seen: set[str] = set()
    launchers: list[tuple[list[str], dict[str, Any]]] = []
    candidate_paths = _candidate_windows_python_paths()
    explicit_candidates = [
        (source, candidate)
        for source, candidate in candidate_paths
        if _is_explicit_windows_python_source(source)
    ]
    candidates_to_probe = explicit_candidates or candidate_paths
    explicit_configured = bool(explicit_candidates)
    for source, candidate in candidates_to_probe:
        executable = _windows_path_to_wsl_executable_path(candidate)
        if executable in seen:
            continue
        seen.add(executable)
        explicit = _is_explicit_windows_python_source(source)
        attempt = {
            'source': source,
            'configured_path': candidate,
            'executable_path': executable,
            'explicit': explicit,
            'exists': os.path.exists(executable),
        }
        attempts.append(attempt)
        if attempt['exists']:
            diagnostics = {
                'type': 'direct_python',
                'source': source,
                'configured_path': candidate,
                'executable_path': executable,
                'explicit': explicit,
                'attempts': attempts,
            }
            launchers.append((_windows_python_command(executable), diagnostics))
            if explicit:
                return launchers, {
                    'type': 'direct_python_candidates',
                    'attempts': attempts,
                    'explicit_configured': True,
                }
    return launchers, {
        'type': 'direct_python_candidates',
        'attempts': attempts,
        'explicit_configured': explicit_configured,
    }


def _find_windows_python_launcher() -> tuple[Optional[list[str]], dict[str, Any]]:
    """Find the first Windows Python executable runnable from WSL for the bridge."""
    launchers, diagnostics = _windows_python_launcher_candidates()
    if launchers:
        return launchers[0]
    return None, {
        'type': 'direct_python',
        'source': None,
        'error': 'No Windows Python executable found.',
        'attempts': diagnostics.get('attempts', []),
    }


def _windows_bridge_launcher_candidates() -> tuple[list[tuple[list[str], dict[str, Any]]], dict[str, Any]]:
    """Return ordered bridge launchers and diagnostics.

    Explicit Windows Python configuration is authoritative. Auto-discovered
    Python candidates may fall through to later candidates or PowerShell if the
    selected executable fails before the bridge script emits a sentinel.
    """
    candidates, python_diagnostics = _windows_python_launcher_candidates()
    explicit_configured = bool(python_diagnostics.get('explicit_configured'))
    if explicit_configured:
        return candidates, {
            'type': 'bridge_launcher_candidates',
            'python': python_diagnostics,
            'powershell_skipped': 'explicit Windows Python configured',
        }

    powershell = _find_powershell_executable()
    if powershell:
        candidates.append(([powershell], {
            'type': 'powershell_path_lookup',
            'source': 'powershell.exe',
            'executable_path': powershell,
            'python': python_diagnostics,
        }))
    return candidates, {
        'type': 'bridge_launcher_candidates',
        'python': python_diagnostics,
        'powershell': powershell,
    }


def _windows_bridge_launcher() -> tuple[Optional[list[str]], dict[str, Any]]:
    candidates, diagnostics = _windows_bridge_launcher_candidates()
    if candidates:
        return candidates[0]
    return None, {
        'type': 'unavailable',
        'error': 'Neither Windows Python nor powershell.exe was found.',
        'diagnostics': diagnostics,
    }


def _wsl_path_to_windows(path: str) -> str:
    try:
        result = subprocess.run(
            ['wslpath', '-w', path],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return path
    converted = result.stdout.strip()
    return converted or path


def _ue_output_to_string(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and 'output' in item:
                parts.append(str(item.get('output') or ''))
            else:
                parts.append(str(item))
        return ''.join(parts)
    return str(value)


# Common Unreal Python failure signatures mapped to actionable guidance for
# the MCP driver. First match wins per pattern; at most four hints are emitted.
_FAILURE_HINT_PATTERNS: list[tuple['re.Pattern[str]', str]] = [
    (
        re.compile(r"AttributeError: module 'unreal' has no attribute '(\w+)'"),
        "unreal.<name> does not exist in this engine version — the API may be renamed, "
        "deprecated, or hallucinated. Use search_unreal_api('keyword') to find the real "
        "name locally, or probe print([n for n in dir(unreal) if 'keyword' in n.lower()]).",
    ),
    (
        re.compile(r"'NoneType' object has no attribute"),
        "A lookup returned None (commonly unreal.load_asset with a wrong path) and the "
        "script used it anyway. Guard every load: a = unreal.load_asset(p); "
        "if a is None: raise ValueError(p).",
    ),
    (
        re.compile(r"AttributeError: '[^']+' object has no attribute '(\w+)'"),
        "The object lacks that attribute/method in this engine version. Check "
        "search_unreal_api('<ClassName> <keyword>') or describe_unreal_api('<ClassName>'), "
        "or inspect live: print([n for n in dir(obj) if not n.startswith('_')]).",
    ),
    (
        re.compile(r'Failed to load|Failed to find|does not exist|could not be found|not found|is not a valid', re.IGNORECASE),
        "An asset/path lookup failed. Use object paths like /Game/Folder/Asset "
        "(no .uasset extension, no Content/ prefix), and list candidates first "
        "(e.g. list_skeletal_meshes or unreal.AssetRegistryHelpers.get_asset_registry()).",
    ),
    (
        re.compile(r'ModuleNotFoundError|ImportError'),
        "Module import failed inside Unreal's embedded Python. Only engine/project-bundled "
        "modules are importable; stdlib modules like json, re, and math are safe.",
    ),
    (
        re.compile(r'is deprecated', re.IGNORECASE),
        "The script used a deprecated API. Prefer editor subsystems, e.g. "
        "unreal.get_editor_subsystem(unreal.EditorActorSubsystem / unreal.LevelEditorSubsystem / "
        "unreal.EditorAssetSubsystem), over EditorLevelLibrary/EditorAssetLibrary.",
    ),
    (
        re.compile(r'SyntaxError'),
        "Unreal reported a Python syntax error. Check string escaping in generated code "
        "(quotes, backslashes in paths, f-string braces) and resend.",
    ),
]

_TIMEOUT_GUIDANCE = (
    ' The editor may still be running the script on its game thread. For long operations '
    'pass a larger timeout_seconds, keep scripts non-interactive (no input()/sleep polling), '
    'and split very large batches into smaller calls.'
)

EDITOR_BUSY_MESSAGE = (
    'Editor process is alive but not answering discovery - its game thread is busy '
    '(long compile/bake or a still-running script). Wait and retry; do not restart '
    'the editor or resend the last script.'
)

_EXECUTION_MODE_ALIASES = {
    'executefile': 'ExecuteFile',
    'execute': 'ExecuteFile',
    'exec': 'ExecuteFile',
    'file': 'ExecuteFile',
    'script': 'ExecuteFile',
    'run': 'ExecuteFile',
    'statement': 'ExecuteStatement',
    'executestatement': 'ExecuteStatement',
    'execstatement': 'ExecuteStatement',
    'eval': 'EvaluateStatement',
    'evaluate': 'EvaluateStatement',
    'expression': 'EvaluateStatement',
    'evaluatestatement': 'EvaluateStatement',
}


def _execution_mode_key(mode: Any) -> str:
    return re.sub(r'[^a-z]', '', str(mode).strip().lower())


def normalize_execution_mode(mode: Any) -> str:
    """Return the Unreal Remote Execution mode name accepted by the editor.

    Unreal's Python remote execution protocol expects enum-style mode names
    such as ``ExecuteFile``. Drivers often send human/tool-style aliases like
    ``execute``; normalize those locally so the editor never receives an
    unparsable ``exec_mode`` value.
    """
    if mode is None or str(mode).strip() == '':
        return 'ExecuteFile'
    key = _execution_mode_key(mode)
    if key in _EXECUTION_MODE_ALIASES:
        return _EXECUTION_MODE_ALIASES[key]
    raise ValueError(
        f"Invalid Unreal Python execution mode {mode!r}. Use one of: "
        "ExecuteFile, ExecuteStatement, EvaluateStatement."
    )


def _invalid_execution_mode_result(mode: Any, error: Exception) -> dict[str, Any]:
    return {
        'success': False,
        'result': str(error),
        'output': '',
        'parsed': None,
        'hints': [
            "The command was rejected locally before reaching Unreal (no editor round-trip was made).",
            "Use mode='ExecuteFile' for normal scripts. Aliases like 'execute' are normalized to ExecuteFile.",
            "Use mode='EvaluateStatement' only for a single expression; use 'ExecuteStatement' only for a single statement.",
        ],
    }


def _failure_hints(success: bool, combined: str, parsed: Any) -> list[str]:
    """Classify common UE Python failures into actionable driver hints."""
    hints: list[str] = []
    if not success:
        for pattern, hint in _FAILURE_HINT_PATTERNS:
            if pattern.search(combined):
                hints.append(hint)
                if len(hints) >= 4:
                    break
    elif parsed is None and _MCP_RESULT_SENTINEL not in combined:
        hints.append(
            "No __MCP_RESULT__ sentinel was found, so 'parsed' is null. End scripts with "
            "print('__MCP_RESULT__' + json.dumps(payload)) to return structured data."
        )
    return hints


def _script_syntax_preflight(code: str, mode: str) -> Optional[dict[str, Any]]:
    """Reject syntactically invalid scripts locally, before any UE round-trip.

    Returns a normalized failure result dict, or None when the script passes
    (or preflight is disabled via UE_SCRIPT_PREFLIGHT=0).
    """
    if not SCRIPT_PREFLIGHT_ENABLED:
        return None
    try:
        normalized_mode = normalize_execution_mode(mode)
    except ValueError as e:
        return _invalid_execution_mode_result(mode, e)
    compile_mode = 'eval' if normalized_mode == 'EvaluateStatement' else 'exec'
    try:
        compile(code, '<ue_python>', compile_mode)
    except SyntaxError as e:
        line_text = (e.text or '').strip()
        detail = f'SyntaxError: {e.msg} (line {e.lineno}, offset {e.offset})'
        if line_text:
            detail += f': {line_text}'
        if compile_mode == 'eval':
            extra = (
                "mode='EvaluateStatement' compiles as a single expression; use "
                "mode='ExecuteFile' (default) for statements or multi-line scripts."
            )
        else:
            extra = (
                'Fix the syntax and resend; check escaping of quotes, backslashes in '
                'asset paths, and f-string braces in generated code.'
            )
        return {
            'success': False,
            'result': detail,
            'output': '',
            'parsed': None,
            'hints': [
                'The script was rejected locally before reaching Unreal (no editor round-trip was made).',
                extra,
            ],
        }
    except ValueError:
        # Source containing null bytes etc. — let Unreal report it.
        return None
    return None


def _normalize_command_result(result_data: dict[str, Any]) -> dict[str, Any]:
    success = bool(result_data.get('success', False))
    result_str = _ue_output_to_string(result_data.get('result', ''))
    output_str = _ue_output_to_string(result_data.get('output', ''))

    parsed = None
    combined = output_str + result_str
    sentinel_idx = combined.find(_MCP_RESULT_SENTINEL)
    if sentinel_idx != -1:
        json_str = combined[sentinel_idx + len(_MCP_RESULT_SENTINEL):].strip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(json_str)
        except json.JSONDecodeError:
            logger.debug('Failed to parse MCP result JSON: %s', json_str[:200])

    return {
        'success': success,
        'result': result_str,
        'output': output_str,
        'parsed': parsed,
        'hints': _failure_hints(success, combined, parsed),
    }


def _multicast_socket_candidates(
    bind_candidates: list[str],
    interface_candidates: list[str],
    membership_candidates: list[str],
) -> list[tuple[str, str, str]]:
    """Return bind/outbound-interface/membership-interface combinations."""
    candidates: list[tuple[str, str, str]] = []
    for bind_address in bind_candidates:
        outbound_addresses = interface_candidates or [bind_address]
        member_addresses = membership_candidates or [bind_address]
        for outbound_address in outbound_addresses:
            for member_address in member_addresses:
                combo = (bind_address, outbound_address, member_address)
                if combo not in candidates:
                    candidates.append(combo)
    return candidates


def _default_multicast_ttl() -> int:
    """Default multicast TTL for the current host namespace.

    Unreal defaults to TTL 0 for same-host discovery. WSL and Windows are not
    the same network namespace, even when mirrored networking makes loopback
    ports visible across the boundary, so WSL needs TTL 1 by default.
    """
    return 1 if _is_wsl() else 0


def _default_multicast_bind_candidates(multicast_host: str) -> list[str]:
    """Return default UDP bind candidates for UE discovery.

    On WSL mirrored networking, a Windows process bound to 127.0.0.1:6766 can
    make Linux wildcard binds fail with EADDRINUSE. Binding the socket to the
    multicast group address avoids that local loopback collision while still
    allowing membership on the routed WSL interface.
    """
    candidates = ['0.0.0.0']
    if _is_wsl():
        candidates.append(multicast_host)
    return _dedupe_addresses(candidates)


def _default_multicast_interface_candidates(multicast_host: str) -> list[str]:
    if not _is_wsl():
        return []
    return _dedupe_addresses([
        _local_ipv4_for_remote(multicast_host),
        '0.0.0.0',
    ])


def _default_multicast_membership_candidates(multicast_host: str) -> list[str]:
    if not _is_wsl():
        return []
    return _dedupe_addresses([
        _local_ipv4_for_remote(multicast_host),
        '0.0.0.0',
    ])


MULTICAST_GROUP = (os.environ.get('UE_MULTICAST_GROUP', '239.0.0.1'), _int_env('UE_MULTICAST_PORT', 6766))
MULTICAST_BIND_CANDIDATES = _split_address_list(
    os.environ.get('UE_MULTICAST_BIND'),
    _default_multicast_bind_candidates(MULTICAST_GROUP[0]),
)
MULTICAST_INTERFACE_CANDIDATES = _split_address_list(
    os.environ.get('UE_MULTICAST_INTERFACE'),
    _default_multicast_interface_candidates(MULTICAST_GROUP[0]),
)
MULTICAST_MEMBERSHIP_CANDIDATES = _split_address_list(
    os.environ.get('UE_MULTICAST_MEMBERSHIP'),
    _default_multicast_membership_candidates(MULTICAST_GROUP[0]),
)
MULTICAST_TTL = _int_env('UE_MULTICAST_TTL', _default_multicast_ttl())
COMMAND_ENDPOINT = (os.environ.get('UE_COMMAND_HOST', '0.0.0.0'), _int_env('UE_COMMAND_PORT', 6777))
COMMAND_PORT_STRICT = _bool_env('UE_COMMAND_PORT_STRICT', False)
CALLBACK_HOST = os.environ.get('UE_CALLBACK_HOST', '').strip() or None
WINDOWS_BRIDGE_ENABLED = _bool_env('UE_WINDOWS_BRIDGE', True)
WINDOWS_BRIDGE_DISCOVERY_TIMEOUT = _int_env('UE_WINDOWS_BRIDGE_DISCOVERY_TIMEOUT', 5)
WINDOWS_BRIDGE_EXEC_TIMEOUT = _int_env('UE_WINDOWS_BRIDGE_EXEC_TIMEOUT', 120)
COMMAND_EXEC_TIMEOUT = max(1, _int_env('UE_COMMAND_EXEC_TIMEOUT', WINDOWS_BRIDGE_EXEC_TIMEOUT))
CONNECTION_STATUS_TIMEOUT = max(0.01, _float_env('UE_CONNECTION_STATUS_TIMEOUT', 0.25))
# Persistent Windows bridge daemon (eliminates the per-call process spawn and
# per-call UDP/TCP handshake). Falls back to one-shot subprocesses when off or
# when no direct Windows Python launcher can host the daemon.
WINDOWS_BRIDGE_DAEMON_ENABLED = _bool_env('UE_WINDOWS_BRIDGE_DAEMON', True)
WINDOWS_BRIDGE_DAEMON_START_TIMEOUT = max(1.0, _float_env('UE_WINDOWS_BRIDGE_DAEMON_START_TIMEOUT', 15.0))
WINDOWS_BRIDGE_DAEMON_COOLDOWN = max(1.0, _float_env('UE_WINDOWS_BRIDGE_DAEMON_COOLDOWN', 60.0))
# Bridge discovery results are cached briefly so discover/connect/status calls
# made back-to-back do not each pay a full discovery round.
WINDOWS_BRIDGE_NODE_TTL = max(0.0, _float_env('UE_BRIDGE_NODE_CACHE_TTL', 5.0))
WINDOWS_BRIDGE_EMPTY_NODE_TTL = max(0.0, _float_env('UE_BRIDGE_EMPTY_CACHE_TTL', 2.0))
# Extra wait after the first discovery pong to catch additional editors.
DISCOVERY_SETTLE_SECONDS = max(0.0, _float_env('UE_DISCOVERY_SETTLE', 0.25))
# Local syntax check before shipping scripts to Unreal.
SCRIPT_PREFLIGHT_ENABLED = _bool_env('UE_SCRIPT_PREFLIGHT', True)

_MCP_RESULT_SENTINEL = '__MCP_RESULT__'

logger = logging.getLogger(__name__)


class UENotRunningError(Exception):
    """Raised when no Unreal Editor instances are discovered."""


class UEConnectionError(Exception):
    """Raised when a connection attempt to Unreal Editor fails."""


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


class _WindowsBridgeDaemon:
    """Long-lived Windows-side bridge process speaking JSON lines over stdio.

    One daemon replaces the spawn-per-call subprocess bridge: it keeps the
    Windows Python interpreter warm and holds persistent TCP command channels
    to editor nodes, so repeated execute calls skip both the process spawn and
    the UDP open_connection handshake.
    """

    def __init__(self, command: list[str], diagnostics: dict[str, Any]):
        self.command = list(command)
        self.diagnostics = dict(diagnostics)
        self._proc: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._responses: dict[str, dict[str, Any]] = {}
        # Only responses for ids in _pending are stored: a late reply to a
        # request that already timed out is dropped instead of accumulating.
        self._pending: set[str] = set()
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._eof = False
        self._stdout_tail: list[str] = []
        self._stderr_tail: list[str] = []

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ready_timeout: float = WINDOWS_BRIDGE_DAEMON_START_TIMEOUT) -> bool:
        if self._proc is not None:
            # One instance hosts one process; callers create a fresh instance
            # to restart (re-spawning here would orphan the previous child).
            return self.alive()
        self._script_path = _write_temp_text(
            _WINDOWS_BRIDGE_SCRIPT,
            suffix='.py',
            prefix='ue_ikrig_mcp_bridge_daemon_',
        )
        script_win = _wsl_path_to_windows(self._script_path)
        try:
            self._proc = subprocess.Popen(
                self.command + [script_win, '--daemon'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.diagnostics['start_error'] = f'{type(e).__name__}: {e}'
            self._proc = None
            self._unlink_script()
            return False
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        ready = self.request({'op': 'ping'}, timeout=ready_timeout)
        if ready is None or not ready.get('ok'):
            self.diagnostics['start_error'] = (
                'Bridge daemon did not answer the readiness ping.'
            )
            self.stop()
            return False
        return True

    def _read_stdout(self) -> None:
        proc = self._proc
        try:
            for line in proc.stdout:
                line = line.rstrip('\r\n')
                if line.startswith(_WINDOWS_BRIDGE_RESULT_PREFIX):
                    try:
                        payload = json.loads(line[len(_WINDOWS_BRIDGE_RESULT_PREFIX):])
                    except json.JSONDecodeError:
                        continue
                    request_id = payload.get('id')
                    if request_id:
                        with self._cond:
                            if str(request_id) in self._pending:
                                self._responses[str(request_id)] = payload
                            self._cond.notify_all()
                elif line:
                    self._stdout_tail.append(line)
                    del self._stdout_tail[:-20]
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for line in proc.stderr:
                line = line.rstrip('\r\n')
                if line:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-20]
        except (OSError, ValueError):
            pass

    def request(self, payload: dict[str, Any], timeout: float) -> Optional[dict[str, Any]]:
        """Send one request and wait for its response. None on timeout/death."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return None
        request_id = str(uuid.uuid4())
        # ensure_ascii so the request line survives any Windows pipe codepage.
        line = json.dumps({**payload, 'id': request_id}, ensure_ascii=True)
        with self._cond:
            self._pending.add(request_id)
        # Note: while the daemon is busy with a long command it does not read
        # stdin, so a concurrent oversized request line can block here until
        # the daemon loops back — head-of-line waiting, not a deadlock (the
        # response reader runs on its own thread).
        with self._write_lock:
            try:
                proc.stdin.write(line + '\n')
                proc.stdin.flush()
            except (OSError, ValueError):
                with self._cond:
                    self._pending.discard(request_id)
                return None
        deadline = time.time() + max(0.1, float(timeout))
        with self._cond:
            try:
                while request_id not in self._responses:
                    if self._eof:
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=min(0.25, remaining))
                return self._responses.pop(request_id, None)
            finally:
                self._pending.discard(request_id)

    def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                with self._write_lock:
                    try:
                        proc.stdin.write(
                            json.dumps({'op': 'shutdown', 'id': str(uuid.uuid4())}) + '\n'
                        )
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            self._proc = None
        self._unlink_script()

    def _unlink_script(self) -> None:
        if self._script_path:
            try:
                os.unlink(self._script_path)
            except OSError:
                pass
            self._script_path = None

    def tails(self) -> dict[str, Any]:
        return {
            'stdout_tail': list(self._stdout_tail),
            'stderr_tail': list(self._stderr_tail),
        }


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
        multicast_group: Optional[tuple[str, int]] = None,
        multicast_bind_candidates: Optional[list[str]] = None,
        multicast_interface_candidates: Optional[list[str]] = None,
        multicast_membership_candidates: Optional[list[str]] = None,
        multicast_ttl: Optional[int] = None,
        callback_host: Optional[str] = None,
    ):
        self._node_id = str(uuid.uuid4())
        self._nodes = _NodeSet()
        self._broadcast_socket: Optional[socket.socket] = None
        self._broadcast_sockets: list[socket.socket] = []
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
        self._multicast_group = multicast_group or MULTICAST_GROUP
        self._multicast_bind_candidates = list(multicast_bind_candidates or MULTICAST_BIND_CANDIDATES)
        self._multicast_interface_candidates = list(multicast_interface_candidates or MULTICAST_INTERFACE_CANDIDATES)
        self._multicast_membership_candidates = list(multicast_membership_candidates or MULTICAST_MEMBERSHIP_CANDIDATES)
        self._multicast_ttl = MULTICAST_TTL if multicast_ttl is None else multicast_ttl
        self._callback_host_override = CALLBACK_HOST if callback_host is None else callback_host
        self._active_multicast_bind_address: Optional[str] = None
        self._active_multicast_interface_address: Optional[str] = None
        self._active_multicast_membership_address: Optional[str] = None
        self._active_multicast_sockets: list[dict[str, str]] = []
        self._discovery_socket_attempts: list[dict[str, Any]] = []
        self._last_discovery_error: Optional[str] = None
        self._pong_events: list[dict[str, Any]] = []
        self._packet_parse_errors: list[dict[str, Any]] = []
        self._last_ping_sent_at: Optional[float] = None
        self._last_ping_error: Optional[str] = None
        self._last_udp_send_attempts: list[dict[str, Any]] = []
        self._last_ping_send_attempts: list[dict[str, Any]] = []
        self._last_callback_request: Optional[dict[str, Any]] = None
        self._windows_bridge_node_ids: set[str] = set()
        self._windows_bridge_nodes: list[dict[str, Any]] = []
        self._windows_bridge_nodes_at: float = 0.0
        self._windows_bridge_connected = False
        self._last_windows_bridge_result: Optional[dict[str, Any]] = None
        self._bridge_daemon: Optional[_WindowsBridgeDaemon] = None
        self._bridge_daemon_cooldowns: dict[tuple[str, ...], float] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def start_discovery(self) -> None:
        """Open UDP broadcast socket and start the discovery listen thread."""
        self._running = True
        self._last_ping = None
        self._nodes = _NodeSet()
        self._pong_events = []
        self._packet_parse_errors = []
        self._last_ping_sent_at = None
        self._last_ping_error = None
        self._last_udp_send_attempts = []
        self._last_ping_send_attempts = []
        try:
            self._init_broadcast_socket()
        except Exception:
            self._running = False
            raise
        self._listen_thread = threading.Thread(target=self._run_broadcast_listen, daemon=True)
        self._listen_thread.start()

    def _init_broadcast_socket(self) -> None:
        self._discovery_socket_attempts = []
        self._broadcast_socket = None
        self._broadcast_sockets = []
        self._active_multicast_sockets = []
        self._active_multicast_bind_address = None
        self._active_multicast_interface_address = None
        self._active_multicast_membership_address = None
        for bind_address, interface_address, membership_address in _multicast_socket_candidates(
            self._multicast_bind_candidates,
            self._multicast_interface_candidates,
            self._multicast_membership_candidates,
        ):
            attempt = {
                'bind_address': bind_address,
                'interface_address': interface_address,
                'membership_address': membership_address,
                'success': False,
            }
            sock: Optional[socket.socket] = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, 'SO_REUSEPORT'):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                sock.bind((bind_address, self._multicast_group[1]))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._multicast_ttl)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_address))
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(self._multicast_group[0]) + socket.inet_aton(membership_address),
                )
                sock.settimeout(0.1)
                self._last_discovery_error = None
                attempt['success'] = True
                self._discovery_socket_attempts.append(attempt)
                self._broadcast_sockets.append(sock)
                self._active_multicast_sockets.append({
                    'bind_address': bind_address,
                    'interface_address': interface_address,
                    'membership_address': membership_address,
                })
                # Keep the first socket in the legacy singular attribute for
                # compatibility, but send/receive on every active socket.
                if self._broadcast_socket is None:
                    self._broadcast_socket = sock
                    self._active_multicast_bind_address = bind_address
                    self._active_multicast_interface_address = interface_address
                    self._active_multicast_membership_address = membership_address
            except (OSError, ValueError) as e:
                attempt['error'] = f'{type(e).__name__}: {e}'
                self._last_discovery_error = attempt['error']
                self._discovery_socket_attempts.append(attempt)
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        if self._broadcast_sockets:
            return
        raise UEConnectionError(
            'Failed to open UE Python remote-execution UDP multicast socket. '
            f'Attempts: {self._discovery_socket_attempts}'
        )

    def _run_broadcast_listen(self) -> None:
        while self._running:
            for sock in list(self._broadcast_sockets):
                while True:
                    try:
                        data, source_address = sock.recvfrom(_DEFAULT_RECEIVE_BUFFER_SIZE)
                    except socket.timeout:
                        data = None
                        source_address = None
                    except OSError as e:
                        self._last_discovery_error = f'{type(e).__name__}: {e}'
                        data = None
                        source_address = None
                    if data:
                        self._handle_broadcast_data(data, source_address)
                    else:
                        break
            now = time.time()
            self._broadcast_ping(now)
            self._nodes.timeout()
            time.sleep(0.1)

    def _broadcast_ping(self, now: float) -> None:
        if not self._last_ping or (self._last_ping + _NODE_PING_SECONDS) < now:
            self._last_ping = now
            try:
                self._send_broadcast(_Message(_TYPE_PING, self._node_id))
                self._last_ping_sent_at = now
                self._last_ping_error = None
            except OSError as e:
                self._last_ping_error = f'{type(e).__name__}: {e}'
                raise

    def _send_broadcast(self, msg: _Message) -> None:
        sockets = list(self._broadcast_sockets)
        if not sockets and self._broadcast_socket:
            sockets = [self._broadcast_socket]
        errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        payload = msg.to_json_bytes()
        for index, sock in enumerate(sockets):
            socket_config = (
                self._active_multicast_sockets[index]
                if index < len(self._active_multicast_sockets)
                else None
            )
            attempt = {
                'socket_index': index,
                'socket_config': socket_config,
                'multicast_group': list(self._multicast_group),
                'message_type': msg.type_,
                'success': False,
            }
            try:
                sock.sendto(payload, self._multicast_group)
                attempt['success'] = True
            except OSError as e:
                error = f'{type(e).__name__}: {e}'
                attempt['error'] = error
                errors.append(error)
            attempts.append(attempt)
        self._last_udp_send_attempts = attempts
        if msg.type_ == _TYPE_PING:
            self._last_ping_send_attempts = attempts
        if errors and len(errors) == len(sockets):
            raise OSError('; '.join(errors))
        if errors:
            self._last_discovery_error = (
                f'Partial UDP send failure: {len(errors)}/{len(sockets)} sockets failed'
            )
        elif attempts and self._last_discovery_error and self._last_discovery_error.startswith('Partial UDP send failure:'):
            self._last_discovery_error = None

    def _handle_broadcast_data(self, data: bytes, source_address: Optional[tuple[str, int]] = None) -> None:
        msg = _Message(None, None)
        if not msg.from_json_bytes(data):
            self._packet_parse_errors.append({
                'timestamp': time.time(),
                'source_address': list(source_address) if source_address else None,
                'packet_size': len(data),
                'classification': 'PROTOCOL_VERSION_OR_MAGIC_MISMATCH',
            })
            self._packet_parse_errors = self._packet_parse_errors[-50:]
            return
        if not msg.passes_receive_filter(self._node_id):
            return
        if msg.type_ == _TYPE_PONG:
            data_payload = dict(msg.data or {})
            if source_address:
                data_payload['_source_address'] = list(source_address)
            self._nodes.update(msg.source, data_payload)
            self._pong_events.append({
                'timestamp': time.time(),
                'node_id': msg.source,
                'source_address': list(source_address) if source_address else None,
                'data_keys': sorted(data_payload.keys()),
            })
            self._pong_events = self._pong_events[-50:]

    def get_remote_nodes(self) -> list:
        """Return the currently discovered remote editor nodes."""
        nodes = self._nodes.remote_nodes
        if nodes:
            return nodes
        if self._windows_bridge_supported():
            return self._discover_windows_bridge_nodes(
                timeout=float(WINDOWS_BRIDGE_DISCOVERY_TIMEOUT),
            )
        return nodes

    def _windows_bridge_supported(self) -> bool:
        """Return True when WSL can delegate UE transport to Windows Python."""
        if not WINDOWS_BRIDGE_ENABLED or not _is_wsl():
            return False
        launcher, _diagnostics = _windows_bridge_launcher()
        return bool(launcher)

    def _ensure_bridge_daemon(
        self,
        launchers: list[tuple[list[str], dict[str, Any]]],
    ) -> Optional[_WindowsBridgeDaemon]:
        """Return a live bridge daemon, starting one if possible."""
        daemon = self._bridge_daemon
        if daemon is not None:
            if daemon.alive():
                return daemon
            daemon.stop()
            self._bridge_daemon = None
        now = time.time()
        for launcher, diagnostics in launchers:
            if diagnostics.get('type') != 'direct_python':
                # PowerShell-wrapped launchers stay on the one-shot path.
                continue
            key = tuple(launcher)
            last_failure = self._bridge_daemon_cooldowns.get(key)
            if last_failure is not None and (now - last_failure) < WINDOWS_BRIDGE_DAEMON_COOLDOWN:
                continue
            daemon = _WindowsBridgeDaemon(launcher, diagnostics)
            if daemon.start():
                self._bridge_daemon_cooldowns.pop(key, None)
                self._bridge_daemon = daemon
                return daemon
            self._bridge_daemon_cooldowns[key] = now
            daemon.stop()
        return None

    def _run_windows_bridge_via_daemon(
        self,
        payload: dict[str, Any],
        timeout: float,
        launchers: list[tuple[list[str], dict[str, Any]]],
    ) -> Optional[dict[str, Any]]:
        """Route a bridge request through the persistent daemon.

        Returns None when no daemon is available (caller falls back to the
        one-shot subprocess bridge); otherwise a bridge-shaped result dict.
        """
        daemon = self._ensure_bridge_daemon(launchers)
        if daemon is None:
            return None
        result = daemon.request(payload, timeout=max(1.0, float(timeout)))
        diagnostics = {
            **daemon.diagnostics,
            'transport': 'persistent_daemon',
            'pid': daemon.pid,
        }
        if result is None:
            tails = daemon.tails()
            daemon.stop()
            self._bridge_daemon = None
            return _windows_bridge_failure(
                f'Windows bridge timed out after {timeout} seconds.',
                _bridge_launcher=diagnostics,
                _bridge_process={'daemon': True, **tails},
                _bridge_launcher_failures=[],
            )
        result.pop('id', None)
        result.setdefault('ok', False)
        result['_bridge_launcher'] = diagnostics
        result['_bridge_process'] = {'daemon': True, 'pid': daemon.pid}
        # Shape parity with the one-shot path.
        result.setdefault('_bridge_launcher_failures', [])
        return result

    def _run_windows_bridge(self, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        launchers, launcher_diagnostics = _windows_bridge_launcher_candidates()
        if not launchers:
            return {
                'ok': False,
                'error': 'Windows bridge launcher was not found.',
                '_bridge_launcher': launcher_diagnostics,
            }

        if WINDOWS_BRIDGE_DAEMON_ENABLED:
            daemon_result = self._run_windows_bridge_via_daemon(payload, timeout, launchers)
            if daemon_result is not None:
                self._last_windows_bridge_result = daemon_result
                return daemon_result

        script_path = ''
        payload_path = ''
        launcher_path = ''
        launcher_failures: list[dict[str, Any]] = []
        try:
            script_path = _write_temp_text(
                _WINDOWS_BRIDGE_SCRIPT,
                suffix='.py',
                prefix='ue_ikrig_mcp_bridge_',
            )
            payload_path = _write_temp_text(
                json.dumps(payload, ensure_ascii=False),
                suffix='.json',
                prefix='ue_ikrig_mcp_bridge_payload_',
            )
            launcher_path = _write_temp_text(
                _WINDOWS_BRIDGE_LAUNCHER_SCRIPT,
                suffix='.ps1',
                prefix='ue_ikrig_mcp_bridge_launcher_',
            )

            script_win = _wsl_path_to_windows(script_path)
            payload_win = _wsl_path_to_windows(payload_path)
            launcher_win = _wsl_path_to_windows(launcher_path)

            for launcher, current_launcher_diagnostics in launchers:
                if current_launcher_diagnostics.get('type') == 'direct_python':
                    command = list(launcher) + [script_win, payload_win]
                else:
                    command = list(launcher) + [
                        '-NoProfile',
                        '-ExecutionPolicy',
                        'Bypass',
                        '-File',
                        launcher_win,
                        script_win,
                        payload_win,
                    ]
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        errors='replace',
                        timeout=max(1.0, float(timeout)),
                    )
                except subprocess.TimeoutExpired as e:
                    result = _windows_bridge_failure(
                        f'Windows bridge timed out after {timeout} seconds.',
                        stdout=e.stdout,
                        stderr=e.stderr,
                        _bridge_launcher=current_launcher_diagnostics,
                    )
                except (OSError, subprocess.SubprocessError) as e:
                    result = _windows_bridge_failure(
                        f'{type(e).__name__}: {e}',
                        _bridge_launcher=current_launcher_diagnostics,
                    )
                else:
                    bridge_payload: Optional[dict[str, Any]] = None
                    for line in reversed(completed.stdout.splitlines()):
                        if line.startswith(_WINDOWS_BRIDGE_RESULT_PREFIX):
                            try:
                                bridge_payload = json.loads(line[len(_WINDOWS_BRIDGE_RESULT_PREFIX):])
                            except json.JSONDecodeError as e:
                                bridge_payload = {
                                    'ok': False,
                                    'error': f'Failed to parse Windows bridge result JSON: {e}',
                                }
                            break
                    if bridge_payload is None:
                        bridge_payload = {
                            'ok': False,
                            'error': 'Windows bridge did not emit a result sentinel.',
                        }
                    bridge_payload.setdefault('ok', False)
                    bridge_payload['_bridge_launcher'] = current_launcher_diagnostics
                    bridge_payload['_bridge_process'] = {
                        'returncode': completed.returncode,
                        'stdout_tail': completed.stdout[-2000:],
                        'stderr_tail': completed.stderr[-2000:],
                    }
                    if completed.returncode != 0 and bridge_payload.get('ok'):
                        bridge_payload['ok'] = False
                        bridge_payload['error'] = (
                            f'Windows bridge process exited with code {completed.returncode}.'
                        )
                    result = bridge_payload

                result['_bridge_launcher_failures'] = list(launcher_failures)
                launcher_failed_before_bridge = (
                    not result.get('ok')
                    and (
                        result.get('error') == 'Windows bridge did not emit a result sentinel.'
                        or str(result.get('error', '')).startswith('Windows bridge timed out')
                        or str(result.get('error', '')).startswith(('TimeoutExpired:', 'OSError:', 'SubprocessError:'))
                        or result.get('_bridge_process', {}).get('returncode', 0) != 0
                    )
                )
                explicit = bool(current_launcher_diagnostics.get('explicit'))
                if launcher_failed_before_bridge and not explicit:
                    launcher_failures.append({
                        'source': current_launcher_diagnostics.get('source'),
                        'type': current_launcher_diagnostics.get('type'),
                        'error': result.get('error'),
                        'returncode': result.get('_bridge_process', {}).get('returncode'),
                    })
                    continue

                result['_bridge_launcher_failures'] = list(launcher_failures)
                self._last_windows_bridge_result = result
                return result

            result = _windows_bridge_failure(
                'All Windows bridge launchers failed before emitting a bridge result.',
                _bridge_launcher=launcher_diagnostics,
                _bridge_launcher_failures=launcher_failures,
            )
            self._last_windows_bridge_result = result
            return result
        finally:
            for path in (script_path, payload_path, launcher_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def _discover_windows_bridge_nodes(
        self,
        timeout: float = 2.0,
        max_age: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        # Serve recent discovery results from cache so back-to-back
        # discover/connect/status calls do not each pay a discovery round.
        if max_age is None:
            max_age = WINDOWS_BRIDGE_NODE_TTL if self._windows_bridge_nodes else WINDOWS_BRIDGE_EMPTY_NODE_TTL
        if (
            max_age > 0
            and self._windows_bridge_nodes_at > 0
            and (time.time() - self._windows_bridge_nodes_at) < max_age
        ):
            return list(self._windows_bridge_nodes)

        result = self._run_windows_bridge(
            {
                'op': 'discover',
                'group': list(self._multicast_group),
                'ttl': max(1, int(self._multicast_ttl)),
                'timeout': timeout,
                'settle': DISCOVERY_SETTLE_SECONDS,
            },
            timeout=max(5.0, timeout + 3.0),
        )
        nodes: list[dict[str, Any]] = []
        self._windows_bridge_node_ids = set()
        if not result.get('ok'):
            self._windows_bridge_nodes = []
            self._windows_bridge_nodes_at = time.time()
            return nodes
        for node in result.get('nodes') or []:
            if not isinstance(node, dict):
                continue
            node_id = node.get('node_id')
            if not node_id:
                continue
            entry = dict(node)
            entry['_transport'] = 'windows_subprocess'
            nodes.append(entry)
            self._windows_bridge_node_ids.add(str(node_id))
        self._windows_bridge_nodes = nodes
        self._windows_bridge_nodes_at = time.time()
        return nodes

    def _activate_windows_bridge_node(self, node_id: Optional[str]) -> bool:
        if node_id is None or node_id not in self._windows_bridge_node_ids:
            return False
        self._remote_node_id = node_id
        self._windows_bridge_connected = True
        return True

    def _discover_and_activate_windows_bridge_node(self, node_id: Optional[str]) -> bool:
        if node_id is None or not self._windows_bridge_supported():
            return False
        if node_id not in self._windows_bridge_node_ids:
            self._discover_windows_bridge_nodes(
                timeout=float(WINDOWS_BRIDGE_DISCOVERY_TIMEOUT),
            )
        return self._activate_windows_bridge_node(node_id)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def preflight_discovery(
        self,
        timeout: float = 2.0,
        test_callback: bool = True,
        callback_timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Run deterministic UE Remote Execution transport diagnostics.

        The preflight sends the same UDP `ping` packet Unreal's Python Remote
        Execution protocol expects. If no `pong` is observed, it stops with a
        classified UDP discovery failure instead of blindly trying connect or
        execute. If a `pong` is observed and `test_callback` is true, it opens
        and immediately closes the TCP callback channel without executing Python
        in Unreal.
        """
        timeout = max(0.1, float(timeout))
        callback_timeout = max(0.1, float(callback_timeout))
        started_at = time.time()
        started_discovery = False
        result: dict[str, Any] = {
            'ok': False,
            'phase': 'udp_discovery',
            'classification': 'NO_PONG_RECEIVED_UNPROVEN',
            'timeout_seconds': timeout,
            'callback_timeout_seconds': callback_timeout,
            'test_callback': test_callback,
            'protocol': {
                'version': _PROTOCOL_VERSION,
                'magic': _PROTOCOL_MAGIC,
                'ping_type': _TYPE_PING,
                'pong_type': _TYPE_PONG,
                'open_connection_type': _TYPE_OPEN_CONNECTION,
            },
        }

        try:
            if not self._running:
                self.start_discovery()
                started_discovery = True
            # Force an immediate ping for deterministic doctor output even if
            # the background loop already sent one recently.
            self._last_ping = None
            ping_window_start = time.time()
            self._broadcast_ping(ping_window_start)
        except UEConnectionError as e:
            result.update({
                'classification': 'CLIENT_UDP_BIND_FAILED',
                'error': str(e),
                'started_discovery': started_discovery,
                'effective_config': self.get_status(),
            })
            return result
        except OSError as e:
            result.update({
                'classification': 'CLIENT_UDP_BIND_FAILED',
                'error': f'{type(e).__name__}: {e}',
                'started_discovery': started_discovery,
                'effective_config': self.get_status(),
            })
            return result

        deadline = started_at + timeout
        while time.time() < deadline:
            fresh_pong_events = [
                event for event in self._pong_events
                if event.get('timestamp', 0) >= ping_window_start
            ]
            if fresh_pong_events:
                break
            time.sleep(0.05)

        # Keep direct UDP evidence separate from the Windows bridge retry so a
        # bridge success does not hide the failed WSL multicast path.
        all_nodes = self._nodes.remote_nodes
        pong_events = [
            event for event in self._pong_events
            if event.get('timestamp', 0) >= ping_window_start
        ]
        fresh_node_ids = {
            event.get('node_id')
            for event in pong_events
            if event.get('node_id')
        }
        nodes = [
            node for node in all_nodes
            if node.get('node_id') in fresh_node_ids
        ]
        cached_nodes = [
            node for node in all_nodes
            if node.get('node_id') not in fresh_node_ids
        ]
        parse_errors = [
            event for event in self._packet_parse_errors
            if event.get('timestamp', 0) >= ping_window_start
        ]

        result.update({
            'started_discovery': started_discovery,
            'ping_sent_at': self._last_ping_sent_at,
            'ping_error': self._last_ping_error,
            'pong_count': len(pong_events),
            'pong_events': pong_events,
            'nodes': nodes,
            'cached_nodes': cached_nodes,
            'packet_parse_errors': parse_errors,
            'effective_config': self.get_status(),
            'network': _network_diagnostics(self._multicast_group[0]),
        })

        if not nodes:
            if parse_errors:
                result['classification'] = 'PROTOCOL_VERSION_OR_MAGIC_MISMATCH'
            else:
                bridge_nodes: list[dict[str, Any]] = []
                bridge_result: Optional[dict[str, Any]] = None
                if self._windows_bridge_supported():
                    # Preflight is a doctor: always probe live, never serve cache.
                    bridge_nodes = self._discover_windows_bridge_nodes(
                        timeout=float(WINDOWS_BRIDGE_DISCOVERY_TIMEOUT),
                        max_age=0.0,
                    )
                    bridge_result = self._last_windows_bridge_result
                if bridge_result is not None:
                    result['windows_bridge_result'] = bridge_result
                if bridge_nodes:
                    result.update({
                        'ok': True,
                        'classification': 'PONG_RECEIVED_VIA_WINDOWS_BRIDGE',
                        'phase': 'windows_bridge_discovery',
                        'nodes': bridge_nodes,
                        'callback_classification': 'NOT_RUN_WINDOWS_BRIDGE_DISCOVERY_ONLY',
                        'effective_config': self.get_status(),
                        'next_action': (
                            'Direct WSL UDP did not receive a pong, but Windows-side Python '
                            'discovered Unreal. Use connect_to_editor/execute_python with the '
                            'windows_subprocess transport.'
                        ),
                    })
                    return result
                # No pong anywhere. Busy editor vs absent editor changes the
                # right next action completely - check the process.
                editor_alive = self._editor_process_alive()
                result['editor_process_alive'] = editor_alive
                if editor_alive is True:
                    result['classification'] = 'EDITOR_PROCESS_ALIVE_BUT_SILENT'
                    result['next_action'] = EDITOR_BUSY_MESSAGE
                    return result
                possible = [
                    'UE_REMOTE_EXECUTION_DISABLED',
                    'UE_MULTICAST_ENDPOINT_MISMATCH',
                    'WINDOWS_FIREWALL_BLOCKED_UDP',
                    'NO_PONG_RECEIVED_UNPROVEN',
                ]
                if _is_wsl():
                    possible.insert(0, 'WSL_MULTICAST_NAMESPACE_BLOCKED')
                    if self._multicast_ttl == 0:
                        possible.insert(0, 'MULTICAST_TTL_SCOPE_BLOCKED')
                result['possible_classifications'] = possible
                result['next_action'] = (
                    'Do not call connect_to_editor or execute_python yet. Verify Unreal Python '
                    'Remote Execution settings, multicast endpoint/bind address, WSL/network '
                    'namespace, and firewall until at least one pong is observed.'
                )
            return result

        result['classification'] = 'PONG_RECEIVED'
        result['phase'] = 'tcp_callback' if test_callback else 'udp_discovery'
        if not test_callback:
            result['ok'] = True
            result['next_action'] = 'UDP discovery is healthy. Run callback preflight or connect_to_editor next.'
            return result

        node_id = nodes[0]['node_id']
        callback_attempts = 2
        callback_accept_timeout = max(0.1, callback_timeout / callback_attempts)
        previous_active_endpoint = self._active_command_endpoint
        previous_fallback_used = self._fallback_used
        previous_fallback_reason = self._fallback_reason
        try:
            self._open_command_channel_with_fallback(
                node_id,
                accept_timeout=callback_accept_timeout,
                attempts=callback_attempts,
            )
            result.update({
                'ok': True,
                'classification': 'PONG_RECEIVED',
                'callback_classification': 'CALLBACK_REACHABLE',
                'callback_request': self._last_callback_request,
                'next_action': 'Transport preflight passed. MCP discover/connect calls may proceed.',
            })
        except (UEConnectionError, OSError, socket.timeout) as e:
            result.update({
                'ok': False,
                'classification': 'PONG_RECEIVED_CALLBACK_UNREACHABLE',
                'callback_error': f'{type(e).__name__}: {e}',
                'callback_request': self._last_callback_request,
                'next_action': (
                    'UDP discovery is healthy, but Unreal did not reach the TCP callback listener. '
                    'Inspect UE_CALLBACK_HOST/UE_COMMAND_HOST, callback port/firewall, and WSL reachability.'
                ),
            })
        except Exception as e:
            result.update({
                'ok': False,
                'classification': 'CLIENT_INTERNAL_ERROR',
                'callback_classification': 'CLIENT_INTERNAL_ERROR',
                'internal_error': f'{type(e).__name__}: {e}',
                'callback_request': self._last_callback_request,
                'next_action': (
                    'UDP discovery is healthy, but the client failed while setting up '
                    'the callback diagnostic. Preserve this error as a client/library bug '
                    'instead of treating it as a firewall or Unreal reachability issue.'
                ),
            })
        finally:
            self._cleanup_command_sockets()
            self._remote_node_id = None
            self._active_command_endpoint = previous_active_endpoint
            self._fallback_used = previous_fallback_used
            self._fallback_reason = previous_fallback_reason
        result['effective_config'] = self.get_status()
        return result

    def _editor_process_alive(self) -> Optional[bool]:
        """True/False when the Windows bridge could check for a running
        UnrealEditor process; None when unknown (no bridge, check failed)."""
        if not self._windows_bridge_supported():
            return None
        try:
            result = self._run_windows_bridge({'op': 'process_check'}, timeout=15.0)
        except Exception:
            return None
        if not isinstance(result, dict) or not result.get('ok'):
            return None
        alive = result.get('editor_process_alive')
        return alive if isinstance(alive, bool) else None

    def _no_nodes_error(self) -> str:
        """Discovery found nothing - but is the editor gone, or just deaf?

        UE answers discovery pings on the game thread, so a long compile,
        bake, or script makes a healthy editor look absent. Misreporting
        that as 'no editor' sends drivers into restart loops."""
        if self._editor_process_alive() is True:
            return EDITOR_BUSY_MESSAGE
        return 'No Unreal Editor instances discovered within timeout.'

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
        if self.is_connected():
            self._refresh_connection_liveness()
        if self.is_connected() and (node_id is None or node_id == self._remote_node_id):
            return
        if self.is_connected():
            self._cleanup_command_sockets()
            self._windows_bridge_connected = False

        if self._windows_bridge_supported():
            if node_id is None:
                bridge_nodes = self.get_remote_nodes()
                if bridge_nodes:
                    node_id = bridge_nodes[0]['node_id']
            elif self._discover_and_activate_windows_bridge_node(node_id):
                return
            if self._activate_windows_bridge_node(node_id):
                return

        if not self._running:
            try:
                self.start_discovery()
            except UEConnectionError:
                if not self._windows_bridge_supported():
                    raise

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
                raise UENotRunningError(self._no_nodes_error())

        self._remote_node_id = node_id
        if self._activate_windows_bridge_node(node_id):
            return
        if self._discover_and_activate_windows_bridge_node(node_id):
            return

        self._open_command_channel_with_fallback(node_id)

    def _open_command_channel_with_fallback(
        self,
        node_id: str,
        *,
        accept_timeout: float = 5.0,
        attempts: int = 6,
    ) -> None:
        try:
            self._open_command_channel(
                node_id,
                self._active_command_endpoint,
                accept_timeout=accept_timeout,
                attempts=attempts,
            )
            return
        except OSError as e:
            self._cleanup_command_sockets()
            if not should_attempt_command_port_fallback(e, self._strict_command_port):
                raise UEConnectionError(self._format_connect_error('TCP command listener failed', e)) from e

        fallback_endpoint = (self._configured_command_endpoint[0], 0)
        self._active_command_endpoint = fallback_endpoint
        self._fallback_used = True
        self._fallback_reason = (
            f'Configured command endpoint '
            f'{self._configured_command_endpoint[0]}:{self._configured_command_endpoint[1]} '
            f'was unavailable (EADDRINUSE)'
        )
        try:
            self._open_command_channel(
                node_id,
                fallback_endpoint,
                accept_timeout=accept_timeout,
                attempts=attempts,
            )
            return
        except (UEConnectionError, OSError) as e:
            self._cleanup_command_sockets()
            raise UEConnectionError(self._format_connect_error('TCP command fallback endpoint failed', e)) from e

    def _open_command_channel(
        self,
        node_id: str,
        endpoint: tuple[str, int],
        *,
        accept_timeout: float = 5.0,
        attempts: int = 6,
    ) -> None:
        listen_host, listen_port = endpoint
        self._cleanup_command_sockets()

        self._command_listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self._command_listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

        try:
            self._command_listen_socket.bind((listen_host, listen_port))
            self._command_listen_socket.listen(1)
            self._command_listen_socket.settimeout(accept_timeout)
            actual_port = int(self._command_listen_socket.getsockname()[1])
            self._active_command_endpoint = (listen_host, actual_port)

            callback_ip = self._resolve_callback_host(listen_host)
            self._last_callback_request = {
                'command_ip': callback_ip,
                'command_port': actual_port,
                'listen_host': listen_host,
                'node_id': node_id,
            }
            for _ in range(attempts):
                self._send_broadcast(_Message(_TYPE_OPEN_CONNECTION, self._node_id, node_id, {
                    'command_ip': callback_ip,
                    'command_port': actual_port,
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

    def _resolve_callback_host(self, listen_host: str) -> str:
        return _callback_host_for(
            listen_host,
            explicit_host=self._callback_host_override,
            wsl_local_ip=_infer_wsl_callback_ipv4(self._multicast_group[0]),
        )

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

    def _bridge_execute_payload(self, code: str, mode: str, command_timeout: float) -> dict[str, Any]:
        # node_id is read live so a reconnect to a new editor retargets here.
        return {
            'op': 'execute',
            'group': list(self._multicast_group),
            'ttl': max(1, int(self._multicast_ttl)),
            'node_id': self._remote_node_id,
            'code': code,
            'mode': mode,
            'timeout': command_timeout,
        }

    def _reconnect_after_restart(self) -> bool:
        """Drop a stale pinned editor node and reconnect to the current one.

        Called only when the last command provably never ran, so re-running it
        on the new editor is safe. Forces a fresh discovery (bypasses the node
        TTL cache, which may still list the dead editor). Returns True when the
        Windows bridge is connected again."""
        self._windows_bridge_connected = False
        self._remote_node_id = None
        self._mark_transport_disconnected()
        self._windows_bridge_nodes_at = 0.0
        self._windows_bridge_node_ids = set()
        self._windows_bridge_nodes = []
        # Also drop the direct-UDP registry so a stale node there cannot be
        # re-pinned by connect() on non-bridge transports.
        self._nodes = _NodeSet()
        try:
            self.connect()
        except (UENotRunningError, UEConnectionError):
            return False
        return self._windows_bridge_connected

    def execute(self, code: str, mode: str = 'ExecuteFile', timeout: Optional[float] = None) -> dict:
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
        try:
            mode = normalize_execution_mode(mode)
        except ValueError as e:
            return _invalid_execution_mode_result(mode, e)

        # Local syntax preflight: reject malformed scripts in microseconds
        # instead of paying a full editor round-trip to learn the same thing.
        preflight_failure = _script_syntax_preflight(code, mode)
        if preflight_failure is not None:
            return preflight_failure

        if self._command_channel_socket is not None and not self._windows_bridge_connected:
            self._probe_direct_command_socket_liveness()
        if not self.is_connected():
            raise UEConnectionError(
                'Not connected to Unreal Editor. Call connect_to_editor first '
                '(or connect() when driving UEConnection directly).'
            )

        if self._windows_bridge_connected:
            command_timeout = max(
                0.1,
                float(WINDOWS_BRIDGE_EXEC_TIMEOUT if timeout is None else timeout),
            )
            result = self._run_windows_bridge(
                self._bridge_execute_payload(code, mode, command_timeout),
                timeout=command_timeout + 5.0,
            )
            # First call after an editor restart hits a pinned node that no
            # longer exists: the channel handshake never completes, so the
            # command provably never ran (delivered is False). Rediscover the
            # current editor and retry once instead of surfacing a spurious
            # failure that only clears on the *next* call.
            if (
                not result.get('ok')
                and result.get('delivered') is False
                and self._reconnect_after_restart()
            ):
                result = self._run_windows_bridge(
                    self._bridge_execute_payload(code, mode, command_timeout),
                    timeout=command_timeout + 5.0,
                )
            if not result.get('ok'):
                self._windows_bridge_connected = False
                self._mark_transport_disconnected()
                error_text = str(result.get('error', 'unknown error'))
                guidance = _TIMEOUT_GUIDANCE if 'timed out' in error_text.lower() else ''
                raise UEConnectionError(
                    f'Windows bridge command execution failed: {error_text}{guidance}'
            )
            return _normalize_command_result(result.get('result') or {})

        command_timeout = max(
            0.1,
            float(COMMAND_EXEC_TIMEOUT if timeout is None else timeout),
        )
        msg = _Message(_TYPE_COMMAND, self._node_id, self._remote_node_id, {
            'command': code,
            'unattended': True,
            'exec_mode': mode,
        })
        command_socket = self._command_channel_socket
        previous_timeout = command_socket.gettimeout()
        try:
            command_socket.settimeout(command_timeout)
            command_socket.sendall(msg.to_json_bytes())

            # Receive the full response, framed by JSON parseability: the old
            # `len(part) < buffer` heuristic truncated fragmented messages and
            # hung until timeout on responses sized at exact buffer multiples.
            data = b''
            while True:
                part = command_socket.recv(_DEFAULT_RECEIVE_BUFFER_SIZE)
                if not part:
                    break
                data += part
                try:
                    json.loads(data.decode('utf-8'))
                    break
                except ValueError:
                    continue
        except socket.timeout as e:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            raise UEConnectionError(
                f'Command channel timed out after {command_timeout:g} seconds waiting for Unreal Editor response.'
                + _TIMEOUT_GUIDANCE
            ) from e
        except OSError as e:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            raise UEConnectionError(f'Command channel socket failed: {e}') from e
        finally:
            if self._command_channel_socket is command_socket:
                try:
                    command_socket.settimeout(previous_timeout)
                except OSError:
                    self._cleanup_command_sockets()
                    self._mark_transport_disconnected()

        if not data:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            raise UEConnectionError('No response received from Unreal Editor.')

        response = _Message(None, None)
        if not response.from_json_bytes(data):
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            raise UEConnectionError('Failed to parse response from Unreal Editor.')
        if not response.passes_receive_filter(self._node_id) or response.type_ != _TYPE_COMMAND_RESULT:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            raise UEConnectionError('Unexpected response type from Unreal Editor.')

        result_data = response.data or {}
        return _normalize_command_result(result_data)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """Close the command connection and stop discovery."""
        if (
            self._remote_node_id
            and not self._windows_bridge_connected
            and (self._broadcast_socket or self._broadcast_sockets)
        ):
            try:
                self._send_broadcast(_Message(_TYPE_CLOSE_CONNECTION, self._node_id, self._remote_node_id))
            except Exception:
                pass
        self._cleanup_command_sockets()
        if self._bridge_daemon is not None:
            try:
                self._bridge_daemon.request({'op': 'close'}, timeout=2.0)
            except Exception:
                pass
            self._bridge_daemon.stop()
            self._bridge_daemon = None
        self._running = False
        if self._listen_thread:
            self._listen_thread.join(timeout=2.0)
            self._listen_thread = None
        for sock in list(self._broadcast_sockets):
            try:
                sock.close()
            except Exception:
                pass
        self._broadcast_sockets = []
        self._broadcast_socket = None
        self._remote_node_id = None
        self._active_command_endpoint = self._configured_command_endpoint
        self._fallback_used = False
        self._fallback_reason = None
        self._windows_bridge_connected = False
        self._windows_bridge_node_ids = set()
        self._windows_bridge_nodes = []
        self._windows_bridge_nodes_at = 0.0

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
        return self._windows_bridge_connected or self._command_channel_socket is not None

    def get_connected_node_id(self) -> Optional[str]:
        """Return the node ID of the currently connected editor, or None."""
        return self._remote_node_id if self.is_connected() else None

    def _mark_transport_disconnected(self) -> None:
        if not self._windows_bridge_connected and self._command_channel_socket is None:
            self._remote_node_id = None

    def _probe_direct_command_socket_liveness(self) -> dict[str, Any]:
        command_socket = self._command_channel_socket
        if command_socket is None:
            return {
                'transport': 'direct_tcp',
                'ok': False,
                'state': 'not_connected',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        previous_timeout = None
        try:
            previous_timeout = command_socket.gettimeout()
        except OSError as e:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            return {
                'transport': 'direct_tcp',
                'ok': False,
                'state': 'socket_error',
                'error': f'{type(e).__name__}: {e}',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        flags = getattr(socket, 'MSG_PEEK', 0) | getattr(socket, 'MSG_DONTWAIT', 0)
        try:
            if flags:
                try:
                    data = command_socket.recv(1, flags)
                except TypeError:
                    command_socket.settimeout(CONNECTION_STATUS_TIMEOUT)
                    data = command_socket.recv(1)
            else:
                command_socket.settimeout(CONNECTION_STATUS_TIMEOUT)
                data = command_socket.recv(1)
        except (BlockingIOError, InterruptedError, socket.timeout):
            return {
                'transport': 'direct_tcp',
                'ok': True,
                'state': 'open_no_pending_data',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }
        except OSError as e:
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            return {
                'transport': 'direct_tcp',
                'ok': False,
                'state': 'socket_error',
                'error': f'{type(e).__name__}: {e}',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }
        finally:
            if self._command_channel_socket is command_socket:
                try:
                    command_socket.settimeout(previous_timeout)
                except OSError:
                    self._cleanup_command_sockets()
                    self._mark_transport_disconnected()

        if data == b'':
            self._cleanup_command_sockets()
            self._mark_transport_disconnected()
            return {
                'transport': 'direct_tcp',
                'ok': False,
                'state': 'peer_closed',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        return {
            'transport': 'direct_tcp',
            'ok': True,
            'state': 'open_pending_data',
            'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
        }

    def _probe_windows_bridge_liveness(self) -> dict[str, Any]:
        node_id = self._remote_node_id
        if not self._windows_bridge_connected or not node_id:
            self._windows_bridge_connected = False
            self._mark_transport_disconnected()
            return {
                'transport': 'windows_subprocess',
                'ok': False,
                'state': 'not_connected',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        if not self._windows_bridge_supported():
            self._windows_bridge_connected = False
            self._mark_transport_disconnected()
            return {
                'transport': 'windows_subprocess',
                'ok': False,
                'state': 'bridge_unavailable',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        # Fast path: ask the persistent daemon. An open command channel to the
        # node is direct proof of liveness with no discovery round at all.
        daemon = self._bridge_daemon
        if WINDOWS_BRIDGE_DAEMON_ENABLED and daemon is not None and daemon.alive():
            ping = daemon.request({'op': 'ping'}, timeout=max(1.0, CONNECTION_STATUS_TIMEOUT * 4))
            if ping is not None and ping.get('ok'):
                channels = ping.get('channels') or []
                if node_id in channels:
                    return {
                        'transport': 'windows_subprocess',
                        'ok': True,
                        'state': 'daemon_channel_open',
                        'node_id': node_id,
                        'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
                    }
            else:
                # Wedged daemon: clear it so the next call restarts cleanly.
                daemon.stop()
                self._bridge_daemon = None

        # Liveness must be proven fresh: invalidate the node cache so the
        # fallback discovery below cannot serve a stale (up to TTL-old) hit
        # for an editor that just died. The cache timestamp is reset rather
        # than passing max_age so subclass overrides of
        # _discover_windows_bridge_nodes keep their (timeout) signature.
        self._windows_bridge_nodes_at = 0.0
        try:
            nodes = self._discover_windows_bridge_nodes(timeout=CONNECTION_STATUS_TIMEOUT)
        except Exception as e:
            self._windows_bridge_connected = False
            self._mark_transport_disconnected()
            return {
                'transport': 'windows_subprocess',
                'ok': False,
                'state': 'bridge_liveness_error',
                'error': f'{type(e).__name__}: {e}',
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        if any(node.get('node_id') == node_id for node in nodes):
            return {
                'transport': 'windows_subprocess',
                'ok': True,
                'state': 'node_discovered',
                'node_id': node_id,
                'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
            }

        self._windows_bridge_connected = False
        self._mark_transport_disconnected()
        return {
            'transport': 'windows_subprocess',
            'ok': False,
            'state': 'node_not_discovered',
            'node_id': node_id,
            'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
        }

    def _refresh_connection_liveness(self) -> dict[str, Any]:
        if self._windows_bridge_connected:
            return self._probe_windows_bridge_liveness()
        if self._command_channel_socket is not None:
            return self._probe_direct_command_socket_liveness()
        self._mark_transport_disconnected()
        return {
            'transport': None,
            'ok': False,
            'state': 'not_connected',
            'timeout_seconds': CONNECTION_STATUS_TIMEOUT,
        }

    def get_status(self) -> dict[str, Any]:
        """Return connection state plus configured/active command endpoint diagnostics."""
        liveness = self._refresh_connection_liveness()
        wsl_detected = _is_wsl()
        wsl_local_ipv4 = _infer_wsl_callback_ipv4(
            self._multicast_group[0],
            is_wsl=wsl_detected,
        )
        status = {
            'connected': self.is_connected(),
            'node_id': self.get_connected_node_id(),
            'configured_command_endpoint': list(self._configured_command_endpoint),
            'active_command_endpoint': list(self._active_command_endpoint),
            'fallback_used': self._fallback_used,
            'package': _package_diagnostics(),
            'connection_liveness': liveness,
        }
        if self._fallback_reason:
            status['fallback_reason'] = self._fallback_reason
        status['discovery'] = {
            'multicast_group': list(self._multicast_group),
            'multicast_ttl': self._multicast_ttl,
            'bind_candidates': list(self._multicast_bind_candidates),
            'interface_candidates': list(self._multicast_interface_candidates),
            'membership_candidates': list(self._multicast_membership_candidates),
            'active_bind_address': self._active_multicast_bind_address,
            'active_interface_address': self._active_multicast_interface_address,
            'active_membership_address': self._active_multicast_membership_address,
            'active_socket_count': len(self._broadcast_sockets),
            'active_sockets': list(self._active_multicast_sockets),
            'last_error': self._last_discovery_error,
            'socket_attempts': list(self._discovery_socket_attempts),
            'last_ping_sent_at': self._last_ping_sent_at,
            'last_ping_error': self._last_ping_error,
            'last_udp_send_attempts': list(self._last_udp_send_attempts),
            'last_ping_send_attempts': list(self._last_ping_send_attempts),
            'pong_events': list(self._pong_events[-10:]),
            'packet_parse_errors': list(self._packet_parse_errors[-10:]),
        }
        callback_config_error = _callback_host_config_error(self._callback_host_override)
        status['callback'] = {
            'override_host': self._callback_host_override,
            'advertised_host': _callback_host_for(
                self._active_command_endpoint[0],
                explicit_host=self._callback_host_override,
                wsl_local_ip=wsl_local_ipv4,
            ),
            'advertised_port': self._active_command_endpoint[1],
            'wsl_detected': wsl_detected,
            'wsl_local_ipv4': wsl_local_ipv4,
            'last_callback_request': self._last_callback_request,
        }
        if callback_config_error:
            status['callback']['config_error'] = callback_config_error
        last_bridge_result = None
        if self._last_windows_bridge_result is not None:
            last_bridge_result = {
                key: value
                for key, value in self._last_windows_bridge_result.items()
                if key not in {'traceback'}
            }
        bridge_launcher, bridge_launcher_diagnostics = _windows_bridge_launcher()
        status['windows_bridge'] = {
            'enabled': WINDOWS_BRIDGE_ENABLED,
            'supported': bool(WINDOWS_BRIDGE_ENABLED and wsl_detected and bridge_launcher),
            'connected': self._windows_bridge_connected,
            'launcher': bridge_launcher_diagnostics,
            'node_ids': sorted(self._windows_bridge_node_ids),
            'nodes': list(self._windows_bridge_nodes),
            'last_result': last_bridge_result,
            'daemon': {
                'enabled': WINDOWS_BRIDGE_DAEMON_ENABLED,
                'running': bool(self._bridge_daemon is not None and self._bridge_daemon.alive()),
                'pid': self._bridge_daemon.pid if self._bridge_daemon is not None else None,
            },
        }
        status['network'] = _network_diagnostics(self._multicast_group[0])
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

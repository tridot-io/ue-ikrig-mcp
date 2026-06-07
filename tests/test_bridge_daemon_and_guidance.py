import asyncio
import io
import json
import os
import socket
import sys
import threading
import time
import types
import unittest

from ue_ikrig_mcp import ue_connection as uc
from ue_ikrig_mcp.tools import batch as batch_tools
from ue_ikrig_mcp.tools import guide as guide_tools


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn

        if args and callable(args[0]) and not kwargs:
            return decorator(args[0])
        return decorator


def _exec_bridge_script():
    """Execute the embedded bridge script into a fresh namespace."""
    namespace = {"__name__": "bridge_under_test"}
    exec(uc._WINDOWS_BRIDGE_SCRIPT, namespace)
    return namespace


def _local_python_launchers():
    """Launcher candidates that run the bridge daemon on the local Python."""
    return (
        [([sys.executable], {"type": "direct_python", "source": "test-local"})],
        {"type": "available"},
    )


class WindowsBridgeDaemonTests(unittest.TestCase):
    def setUp(self):
        self._original_candidates = uc._windows_bridge_launcher_candidates
        self._original_wsl_path = uc._wsl_path_to_windows
        uc._windows_bridge_launcher_candidates = _local_python_launchers
        # Local python reads the script straight from the WSL/Linux path.
        uc._wsl_path_to_windows = lambda path: path
        self.conn = uc.UEConnection()

    def tearDown(self):
        self.conn.disconnect()
        uc._windows_bridge_launcher_candidates = self._original_candidates
        uc._wsl_path_to_windows = self._original_wsl_path

    def test_daemon_ping_roundtrip_and_process_reuse(self):
        first = self.conn._run_windows_bridge({"op": "ping"}, timeout=15)

        self.assertTrue(first["ok"])
        self.assertEqual(first["op"], "ping")
        self.assertEqual(first["channels"], [])
        self.assertTrue(first["_bridge_process"]["daemon"])
        self.assertEqual(first["_bridge_launcher"]["transport"], "persistent_daemon")

        second = self.conn._run_windows_bridge({"op": "ping"}, timeout=15)

        self.assertTrue(second["ok"])
        self.assertEqual(
            first["_bridge_process"]["pid"],
            second["_bridge_process"]["pid"],
            "daemon process should be reused across bridge calls",
        )

    def test_daemon_restarts_after_process_death(self):
        first = self.conn._run_windows_bridge({"op": "ping"}, timeout=15)
        self.assertTrue(first["ok"])

        self.conn._bridge_daemon._proc.kill()
        self.conn._bridge_daemon._proc.wait(timeout=5)

        second = self.conn._run_windows_bridge({"op": "ping"}, timeout=15)

        self.assertTrue(second["ok"])
        self.assertNotEqual(
            first["_bridge_process"]["pid"],
            second["_bridge_process"]["pid"],
            "a dead daemon should be replaced by a fresh process",
        )

    def test_daemon_disabled_falls_back_to_one_shot(self):
        original_enabled = uc.WINDOWS_BRIDGE_DAEMON_ENABLED
        original_run = uc.subprocess.run
        calls = []
        try:
            uc.WINDOWS_BRIDGE_DAEMON_ENABLED = False

            def fake_run(args, **kwargs):
                calls.append(list(args))
                return uc.subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=uc._WINDOWS_BRIDGE_RESULT_PREFIX
                    + json.dumps({"ok": True, "nodes": []})
                    + "\n",
                    stderr="",
                )

            uc.subprocess.run = fake_run
            result = self.conn._run_windows_bridge(
                {"op": "discover", "group": ["239.0.0.1", 6766], "ttl": 1, "timeout": 0.1},
                timeout=1,
            )
        finally:
            uc.WINDOWS_BRIDGE_DAEMON_ENABLED = original_enabled
            uc.subprocess.run = original_run

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.conn._bridge_daemon)


class BridgeNodeCacheTests(unittest.TestCase):
    def _make_counting_connection(self):
        class CountingBridgeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self.discover_calls = 0

            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                if payload["op"] == "discover":
                    self.discover_calls += 1
                    return {
                        "ok": True,
                        "nodes": [{"node_id": "cached-node", "project_name": "P"}],
                    }
                raise AssertionError(payload)

        conn = CountingBridgeConnection()
        conn._running = True
        return conn

    def test_repeat_discovery_is_served_from_cache_within_ttl(self):
        conn = self._make_counting_connection()

        first = conn.get_remote_nodes()
        second = conn.get_remote_nodes()

        self.assertEqual(first[0]["node_id"], "cached-node")
        self.assertEqual(second[0]["node_id"], "cached-node")
        self.assertEqual(conn.discover_calls, 1)

    def test_max_age_zero_bypasses_cache(self):
        conn = self._make_counting_connection()

        conn.get_remote_nodes()
        conn._discover_windows_bridge_nodes(timeout=0.1, max_age=0.0)

        self.assertEqual(conn.discover_calls, 2)


class ScriptGuidanceTests(unittest.TestCase):
    def test_syntax_preflight_blocks_before_any_transport(self):
        class NoTransportConnection(uc.UEConnection):
            def _run_windows_bridge(self, payload, timeout=10.0):
                raise AssertionError("transport must not be used for invalid syntax")

        conn = NoTransportConnection()
        conn._windows_bridge_connected = True
        conn._remote_node_id = "node-1"

        result = conn.execute("def broken(:\n    pass")

        self.assertFalse(result["success"])
        self.assertIn("SyntaxError", result["result"])
        self.assertTrue(result["hints"])
        self.assertIn("no editor round-trip", result["hints"][0])

    def test_evaluate_statement_mode_compiles_as_expression(self):
        result = uc._script_syntax_preflight("x = 1", "EvaluateStatement")

        self.assertIsNotNone(result)
        self.assertIn("EvaluateStatement", result["hints"][1])

        self.assertIsNone(uc._script_syntax_preflight("1 + 1", "EvaluateStatement"))
        self.assertIsNone(uc._script_syntax_preflight("x = 1", "ExecuteFile"))

    def test_failure_hints_classify_unreal_attribute_error(self):
        result = uc._normalize_command_result({
            "success": False,
            "result": "AttributeError: module 'unreal' has no attribute 'EditorLevelLib'",
            "output": "",
        })

        self.assertTrue(result["hints"])
        self.assertIn("dir(unreal)", result["hints"][0])

    def test_failure_hints_classify_bad_asset_path(self):
        result = uc._normalize_command_result({
            "success": False,
            "result": "",
            "output": "LogPython: Error: ValueError: Asset not found: /Game/Missing",
        })

        self.assertTrue(any("/Game/Folder/Asset" in hint for hint in result["hints"]))

    def test_success_without_sentinel_hints_at_result_protocol(self):
        result = uc._normalize_command_result({
            "success": True,
            "result": "None",
            "output": "hello\n",
        })

        self.assertTrue(result["success"])
        self.assertIsNone(result["parsed"])
        self.assertTrue(any("__MCP_RESULT__" in hint for hint in result["hints"]))

    def test_success_with_sentinel_has_no_protocol_hint(self):
        result = uc._normalize_command_result({
            "success": True,
            "result": "None",
            "output": '__MCP_RESULT__{"ok": true}',
        })

        self.assertEqual(result["parsed"], {"ok": True})
        self.assertEqual(result["hints"], [])


class RecvFramingTests(unittest.TestCase):
    def test_recv_json_message_survives_fragmentation_and_buffer_multiples(self):
        namespace = _exec_bridge_script()
        recv_json_message = namespace["recv_json_message"]
        buffer_size = namespace["BUFFER_SIZE"]

        # JSON document padded to exactly two full buffers: the legacy
        # `len(part) < BUFFER_SIZE` framing would hang until timeout here.
        document = json.dumps({"version": 1, "magic": "ue_py", "type": "command_result"})
        payload = document.ljust(buffer_size * 2).encode("utf-8")
        self.assertEqual(len(payload) % buffer_size, 0)

        server_sock, client_sock = socket.socketpair()
        try:
            def feed():
                for start in range(0, len(payload), 1000):
                    server_sock.sendall(payload[start:start + 1000])

            feeder = threading.Thread(target=feed)
            feeder.start()
            message = recv_json_message(client_sock, timeout=5.0)
            feeder.join(timeout=5)
        finally:
            server_sock.close()
            client_sock.close()

        self.assertEqual(message["type"], "command_result")

    def test_direct_execute_parses_fragmented_response(self):
        response = uc._Message(
            uc._TYPE_COMMAND_RESULT,
            "editor-node",
            "client-node",
            {"success": True, "result": "None", "output": "x" * 9000},
        ).to_json_bytes()

        class FragmentingSocket:
            def __init__(self, data):
                # First chunk is a full buffer, remainder arrives separately:
                # the legacy framing would truncate after a short second chunk
                # or hang on an exact-multiple first chunk.
                self.chunks = [data[:8192], data[8192:]]
                self.closed = False

            def gettimeout(self):
                return None

            def settimeout(self, value):
                pass

            def sendall(self, data):
                pass

            def recv(self, size, flags=0):
                if flags:
                    raise BlockingIOError()
                if self.chunks:
                    return self.chunks.pop(0)
                return b""

            def close(self):
                self.closed = True

        conn = uc.UEConnection()
        conn._node_id = "client-node"
        conn._remote_node_id = "editor-node"
        conn._command_channel_socket = FragmentingSocket(response)

        result = conn.execute("print('x')")

        self.assertTrue(result["success"])
        self.assertEqual(len(result["output"]), 9000)


class ExecutePythonAutoConnectTests(unittest.TestCase):
    def _run_execute_python(self, fake_conn):
        fake_server = _FakeServer()
        original_get_connection = batch_tools.get_connection
        try:
            batch_tools.get_connection = lambda: fake_conn
            batch_tools.register(fake_server)
            return asyncio.run(fake_server.tools["execute_python"]("print('hi')"))
        finally:
            batch_tools.get_connection = original_get_connection

    def test_auto_connects_when_disconnected(self):
        class DisconnectedConnection:
            def __init__(self):
                self.connect_calls = 0
                self.execute_calls = []

            def is_connected(self):
                return self.connect_calls > 0

            def connect(self, node_id=None, timeout=5.0):
                self.connect_calls += 1

            def execute(self, code, mode="ExecuteFile", timeout=None):
                self.execute_calls.append(code)
                return {"success": True, "result": "", "output": "", "parsed": None}

        fake_conn = DisconnectedConnection()
        result = self._run_execute_python(fake_conn)

        self.assertEqual(fake_conn.connect_calls, 1)
        self.assertEqual(fake_conn.execute_calls, ["print('hi')"])
        self.assertTrue(json.loads(result[0].text)["success"])

    def test_auto_connect_failure_reports_preflight_guidance(self):
        class UnreachableConnection:
            def is_connected(self):
                return False

            def connect(self, node_id=None, timeout=5.0):
                raise uc.UENotRunningError("No Unreal Editor instances discovered")

            def execute(self, code, mode="ExecuteFile", timeout=None):
                raise AssertionError("execute must not run when auto-connect fails")

        payload = json.loads(self._run_execute_python(UnreachableConnection())[0].text)

        self.assertTrue(payload["error"])
        self.assertIn("preflight_discovery", payload["message"])


class DaemonExecuteRetryDisciplineTests(unittest.TestCase):
    """A command may only be re-sent when it provably never reached Unreal."""

    def _bad_header_response(self):
        return json.dumps({
            "version": 99,
            "magic": "ue_py",
            "type": "command_result",
            "source": "editor",
        }).encode("utf-8")

    def test_post_send_failure_on_cached_channel_never_resends(self):
        ns = _exec_bridge_script()

        class PoisonedChannel:
            def __init__(self, response):
                self.sendall_count = 0
                self.response = response

            def gettimeout(self):
                return None

            def settimeout(self, value):
                pass

            def sendall(self, data):
                self.sendall_count += 1

            def recv(self, size, flags=0):
                # Valid JSON with a bad protocol header: raised AFTER sendall.
                return self.response

            def close(self):
                pass

        channel = PoisonedChannel(self._bad_header_response())
        ns["CHANNELS"]["node-1"] = channel
        ns["open_command_channel"] = lambda payload, node_id: (_ for _ in ()).throw(
            AssertionError("must not reopen/retry after a post-send failure")
        )

        result = ns["daemon_execute"]({"node_id": "node-1", "code": "x", "timeout": 1.0})

        self.assertFalse(result["ok"])
        self.assertEqual(channel.sendall_count, 1)
        self.assertNotIn("node-1", ns["CHANNELS"])

    def test_pre_send_failure_on_cached_channel_retries_once(self):
        ns = _exec_bridge_script()

        class DeadChannel:
            def sendall(self, data):
                raise OSError("broken pipe")

            def close(self):
                pass

        good_response = uc._Message(
            uc._TYPE_COMMAND_RESULT,
            "editor",
            None,
            {"success": True, "result": "", "output": ""},
        ).to_json_bytes()

        class GoodChannel:
            def __init__(self):
                self.sendall_count = 0

            def gettimeout(self):
                return None

            def settimeout(self, value):
                pass

            def sendall(self, data):
                self.sendall_count += 1

            def recv(self, size, flags=0):
                return good_response

        good = GoodChannel()
        ns["CHANNELS"]["node-1"] = DeadChannel()
        ns["open_command_channel"] = lambda payload, node_id: (good, None)

        result = ns["daemon_execute"]({"node_id": "node-1", "code": "x", "timeout": 1.0})

        self.assertTrue(result["ok"])
        self.assertEqual(good.sendall_count, 1)
        self.assertIs(ns["CHANNELS"]["node-1"], good)


class DaemonPingLivenessTests(unittest.TestCase):
    def test_ping_drops_dead_channels_and_keeps_live_ones(self):
        ns = _exec_bridge_script()

        live_a, live_b = socket.socketpair()
        dead_a, dead_b = socket.socketpair()
        dead_b.close()  # peer (editor) gone -> recv on dead_a returns b''
        try:
            ns["CHANNELS"]["live-node"] = live_a
            ns["CHANNELS"]["dead-node"] = dead_a

            response = ns["handle_request"]({"op": "ping"})

            self.assertTrue(response["ok"])
            self.assertEqual(response["channels"], ["live-node"])
            self.assertNotIn("dead-node", ns["CHANNELS"])
        finally:
            for sock in (live_a, live_b, dead_a):
                try:
                    sock.close()
                except OSError:
                    pass


class LivenessFreshnessTests(unittest.TestCase):
    def test_liveness_probe_never_trusts_the_node_cache(self):
        class DeadEditorConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self.discover_calls = 0

            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                self.discover_calls += 1
                return {"ok": False, "error": "no editors"}

        conn = DeadEditorConnection()
        conn._remote_node_id = "node-x"
        conn._windows_bridge_connected = True
        conn._windows_bridge_node_ids = {"node-x"}
        # Freshly cached discovery still lists the node: a dead editor must
        # not be reported alive off the cache.
        conn._windows_bridge_nodes = [{"node_id": "node-x"}]
        conn._windows_bridge_nodes_at = time.time()

        status = conn.get_status()

        self.assertFalse(status["connected"])
        self.assertGreaterEqual(conn.discover_calls, 1)
        self.assertFalse(status["connection_liveness"]["ok"])


class DaemonResponseGatingTests(unittest.TestCase):
    """Late replies to abandoned requests must not accumulate in _responses."""

    def _make_daemon_with_pipe(self):
        daemon = uc._WindowsBridgeDaemon(["unused"], {"type": "direct_python"})
        read_fd, write_fd = os.pipe()
        reader = io.TextIOWrapper(io.FileIO(read_fd, "rb"), encoding="utf-8")
        writer = io.TextIOWrapper(io.FileIO(write_fd, "wb"), encoding="utf-8")
        sent_lines = []

        fake_stdin = types.SimpleNamespace(
            write=lambda line: sent_lines.append(line),
            flush=lambda: None,
            close=lambda: None,
        )
        daemon._proc = types.SimpleNamespace(
            stdout=reader,
            stderr=None,
            stdin=fake_stdin,
            poll=lambda: None,
            pid=4242,
        )
        thread = threading.Thread(target=daemon._read_stdout, daemon=True)
        thread.start()
        return daemon, writer, sent_lines

    def _reply(self, writer, request_id, extra=None):
        payload = {"id": request_id, "ok": True}
        payload.update(extra or {})
        writer.write(uc._WINDOWS_BRIDGE_RESULT_PREFIX + json.dumps(payload) + "\n")
        writer.flush()

    def test_late_reply_after_timeout_is_dropped(self):
        daemon, writer, sent_lines = self._make_daemon_with_pipe()
        try:
            result = daemon.request({"op": "slow"}, timeout=0.2)
            self.assertIsNone(result)

            request_id = json.loads(sent_lines[0])["id"]
            self._reply(writer, request_id)

            deadline = time.time() + 2.0
            while time.time() < deadline and not daemon._eof:
                with daemon._cond:
                    if not daemon._pending and not daemon._responses:
                        time.sleep(0.05)
                # allow reader thread to consume the line
                time.sleep(0.05)
                break

            with daemon._cond:
                self.assertEqual(daemon._responses, {})
                self.assertEqual(daemon._pending, set())
        finally:
            writer.close()

    def test_prompt_reply_is_delivered_and_reclaimed(self):
        daemon, writer, sent_lines = self._make_daemon_with_pipe()
        try:
            def responder():
                deadline = time.time() + 2.0
                while time.time() < deadline and not sent_lines:
                    time.sleep(0.01)
                request_id = json.loads(sent_lines[0])["id"]
                self._reply(writer, request_id, {"op": "ping"})

            thread = threading.Thread(target=responder)
            thread.start()
            result = daemon.request({"op": "ping"}, timeout=5.0)
            thread.join(timeout=5)

            self.assertIsNotNone(result)
            self.assertTrue(result["ok"])
            with daemon._cond:
                self.assertEqual(daemon._responses, {})
                self.assertEqual(daemon._pending, set())
        finally:
            writer.close()


class GuideToolTests(unittest.TestCase):
    def _register(self):
        fake_server = _FakeServer()
        guide_tools.register(fake_server)
        return fake_server.tools["ue_python_guide"]

    def test_full_guide_covers_failure_modes(self):
        text = asyncio.run(self._register()())[0].text

        self.assertIn("__MCP_RESULT__", text)
        self.assertIn("/Game/Folder/Asset", text)
        self.assertIn("get_editor_subsystem", text)
        self.assertIn("timeout_seconds", text)

    def test_topic_filter_and_unknown_topic(self):
        tool = self._register()

        protocol_only = asyncio.run(tool(topic="protocol"))[0].text
        self.assertIn("__MCP_RESULT__", protocol_only)
        self.assertNotIn("ScopedSlowTask", protocol_only)

        unknown = json.loads(asyncio.run(tool(topic="nope"))[0].text)
        self.assertTrue(unknown["error"])
        self.assertIn("protocol", unknown["topics"])


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import os
import socket
import tempfile
import threading
import unittest

from ue_ikrig_mcp import api_index
from ue_ikrig_mcp import script_exec
from ue_ikrig_mcp import ue_connection as uc
from ue_ikrig_mcp.tools import api_catalog as api_catalog_tools
from ue_ikrig_mcp.tools import batch as batch_tools
from ue_ikrig_mcp.tools import guide as guide_tools
from ue_ikrig_mcp.tools import script_store as script_store_tools


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


def _exec_editor_protocol_script():
    """Execute the embedded editor-protocol script into a fresh namespace."""
    namespace = {"__name__": "editor_protocol_under_test"}
    exec(uc._EDITOR_PROTOCOL_SCRIPT, namespace)
    return namespace


class ScriptGuidanceTests(unittest.TestCase):
    def test_syntax_preflight_blocks_before_any_transport(self):
        class PoisonSocket:
            def gettimeout(self):
                raise AssertionError("transport must not be used for invalid syntax")

            def settimeout(self, value):
                raise AssertionError("transport must not be used for invalid syntax")

            def sendall(self, data):
                raise AssertionError("transport must not be used for invalid syntax")

            def recv(self, size, flags=0):
                raise AssertionError("transport must not be used for invalid syntax")

            def close(self):
                pass

        conn = uc.UEConnection()
        # Look connected on the direct path; preflight must reject the malformed
        # script before any transport call touches this poison socket.
        conn._remote_node_id = "node-1"
        conn._command_channel_socket = PoisonSocket()

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

    def test_execution_mode_aliases_are_normalized_before_unreal(self):
        self.assertEqual(uc.normalize_execution_mode("execute"), "ExecuteFile")
        self.assertEqual(uc.normalize_execution_mode("eval"), "EvaluateStatement")
        self.assertIsNone(uc._script_syntax_preflight("x = 1", "execute"))

        result = uc._script_syntax_preflight("x = 1", "definitely-not-a-mode")
        self.assertIsNotNone(result)
        self.assertFalse(result["success"])
        self.assertIn("Invalid Unreal Python execution mode", result["result"])

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
        namespace = _exec_editor_protocol_script()
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
    def _assert_multiple_editors_payload(self, payload):
        self.assertTrue(payload["error"])
        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertIn("Multiple Unreal Editor instances", payload["message"])
        self.assertIn("node_id", payload["next_action"])
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])

    def _ambiguous_connection(self):
        class MultiNodeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._running = True

            def _broker_supported(self):
                return False

            def get_remote_nodes(self):
                return [
                    {"node_id": "node-a", "project_name": "ProjectA"},
                    {"node_id": "node-b", "project_name": "ProjectB"},
                ]

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("ambiguous auto-connect must not open a command channel")

            def execute(self, code, mode="ExecuteFile", timeout=None):
                raise AssertionError("execute must not run when auto-connect is ambiguous")

        return MultiNodeConnection()

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
        self.assertEqual(len(fake_conn.execute_calls), 1)
        # ExecuteFile mode injects the helper prelude ahead of the user code.
        self.assertTrue(fake_conn.execute_calls[0].endswith("print('hi')"))
        self.assertIn("def mcp_result", fake_conn.execute_calls[0])
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

    def test_auto_connect_ambiguity_payload_is_preserved(self):
        class AmbiguousConnection:
            def is_connected(self):
                return False

            def connect(self, node_id=None, timeout=5.0):
                raise uc.UEMultipleEditorsAmbiguousError([
                    {"node_id": "node-a", "project_name": "A", "_transport": "direct_udp"},
                    {"node_id": "node-b", "project_name": "B", "_transport": "direct_udp"},
                ])

            def execute(self, code, mode="ExecuteFile", timeout=None):
                raise AssertionError("execute must not run when editor selection is ambiguous")

        payload = json.loads(self._run_execute_python(AmbiguousConnection())[0].text)

        self.assertTrue(payload["error"])
        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])
        self.assertEqual(
            payload["next_action"],
            "Call connect_to_editor(node_id=<one of nodes[].node_id>).",
        )
        self.assertFalse(payload["message"].startswith("Auto-connect to Unreal Editor failed"))


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
        ns = _exec_editor_protocol_script()

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

    def test_peer_closed_after_send_never_retries(self):
        # sendall succeeds, then the peer closes before any response byte:
        # Unreal may have executed before dying, so this must NOT auto-retry
        # and must NOT be tagged delivered (would license a client retry).
        ns = _exec_editor_protocol_script()

        class ClosedAfterSend:
            def __init__(self):
                self.sendall_count = 0

            def gettimeout(self):
                return None

            def settimeout(self, value):
                pass

            def sendall(self, data):
                self.sendall_count += 1

            def recv(self, size, flags=0):
                return b""  # peer closed, no data -> PeerClosedNoData

            def close(self):
                pass

        channel = ClosedAfterSend()
        ns["CHANNELS"]["node-1"] = channel
        ns["open_command_channel"] = lambda payload, node_id: (_ for _ in ()).throw(
            AssertionError("must not reopen/retry after peer-closed-post-send")
        )

        result = ns["daemon_execute"]({"node_id": "node-1", "code": "x", "timeout": 1.0})

        self.assertFalse(result["ok"])
        self.assertNotIn("delivered", result)  # no retry license for the client
        self.assertEqual(channel.sendall_count, 1)

    def test_one_shot_execute_tags_send_failure_delivered_false(self):
        ns = _exec_editor_protocol_script()

        class DeadChannel:
            def sendall(self, data):
                raise OSError("broken pipe")

            def close(self):
                pass

        ns["resolve_node_id"] = lambda payload: ("node-1", None)
        ns["open_command_channel"] = lambda payload, node_id: (DeadChannel(), None)

        result = ns["execute"]({"node_id": "node-1", "code": "x", "timeout": 1.0})

        self.assertFalse(result["ok"])
        self.assertIs(result["delivered"], False)

    def test_pre_send_failure_on_cached_channel_retries_once(self):
        ns = _exec_editor_protocol_script()

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
        ns = _exec_editor_protocol_script()

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


class _CapturingConnection:
    """Fake UEConnection capturing execute() calls and returning a canned result."""

    def __init__(self, result=None):
        self.execute_calls = []
        self.result = result or {
            "success": True,
            "result": "",
            "output": "",
            "parsed": None,
            "hints": [],
        }

    def is_connected(self):
        return True

    def connect(self, node_id=None, timeout=5.0):
        pass

    def execute(self, code, mode="ExecuteFile", timeout=None):
        self.execute_calls.append((code, mode, timeout))
        return dict(self.result)


class PreludeInjectionTests(unittest.TestCase):
    def _run_execute_python(self, fake_conn, code, **kwargs):
        fake_server = _FakeServer()
        original = batch_tools.get_connection
        try:
            batch_tools.get_connection = lambda: fake_conn
            batch_tools.register(fake_server)
            return asyncio.run(fake_server.tools["execute_python"](code, **kwargs))
        finally:
            batch_tools.get_connection = original

    def test_execute_file_mode_gets_prelude(self):
        conn = _CapturingConnection()
        self._run_execute_python(conn, "mcp_result({'x': 1})")

        sent_code = conn.execute_calls[0][0]
        self.assertIn("def load(path):", sent_code)
        self.assertIn("def mcp_result(payload):", sent_code)
        self.assertTrue(sent_code.endswith("mcp_result({'x': 1})"))
        self.assertEqual(conn.execute_calls[0][1], "ExecuteFile")

    def test_execute_alias_gets_prelude_and_normalized_mode(self):
        conn = _CapturingConnection()
        self._run_execute_python(conn, "mcp_result({'x': 1})", mode="execute")

        sent_code, sent_mode, _ = conn.execute_calls[0]
        self.assertIn("def mcp_result(payload):", sent_code)
        self.assertEqual(sent_mode, "ExecuteFile")

    def test_statement_mode_and_opt_out_skip_prelude(self):
        conn = _CapturingConnection()
        self._run_execute_python(conn, "print(1)", mode="ExecuteStatement")
        self._run_execute_python(conn, "print(2)", inject_helpers=False)

        self.assertEqual(conn.execute_calls[0][0], "print(1)")
        self.assertEqual(conn.execute_calls[1][0], "print(2)")

    def test_syntax_error_reports_user_line_numbers_unshifted(self):
        conn = _CapturingConnection()
        result = self._run_execute_python(conn, "x = 1\ndef broken(:\n")

        payload = json.loads(result[0].text)
        self.assertFalse(payload["success"])
        self.assertIn("line 2", payload["result"])
        self.assertEqual(conn.execute_calls, [])

    def test_runtime_failure_gets_line_offset_hint(self):
        conn = _CapturingConnection(result={
            "success": False,
            "result": 'Traceback ... File "<string>", line 22 ...',
            "output": "",
            "parsed": None,
            "hints": [],
        })
        result = self._run_execute_python(conn, "raise ValueError('boom')")

        payload = json.loads(result[0].text)
        offset_hints = [h for h in payload["hints"] if "injected helper lines" in h]
        self.assertEqual(len(offset_hints), 1)
        self.assertIn(str(script_exec.PRELUDE_LINE_OFFSET), offset_hints[0])

    def test_prelude_helpers_work(self):
        import io as _io
        import contextlib

        class _FakeUnreal:
            @staticmethod
            def load_asset(path):
                return {"path": path} if path.startswith("/Game/Ok") else None

        namespace = {"unreal": _FakeUnreal()}
        # Strip the real import so the stub above is used.
        prelude = script_exec.EXECUTE_PYTHON_PRELUDE.replace("import unreal\n", "")
        exec(prelude, namespace)

        self.assertEqual(namespace["load"]("/Game/Ok/Asset"), {"path": "/Game/Ok/Asset"})
        with self.assertRaisesRegex(ValueError, "/Game/Missing"):
            namespace["load"]("/Game/Missing")

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            namespace["mcp_result"]({"obj": object()})
        line = buffer.getvalue()
        self.assertTrue(line.startswith("__MCP_RESULT__"))
        json.loads(line[len("__MCP_RESULT__"):])  # str-coerced, parseable


class ResultShapingTests(unittest.TestCase):
    def test_compact_drops_raw_echo_when_parsed_present(self):
        shaped = script_exec.shape_result({
            "success": True,
            "result": "None",
            "output": "x" * 5000 + '__MCP_RESULT__{"ok": true}',
            "parsed": {"ok": True},
            "hints": [],
        })

        self.assertEqual(shaped["output"], "")
        self.assertEqual(shaped["result"], "")
        self.assertEqual(shaped["parsed"], {"ok": True})
        self.assertGreater(shaped["raw_omitted"]["output_chars"], 5000)

    def test_compact_keeps_output_on_failure(self):
        big = "log spam\n" * 3000  # ~27k chars
        shaped = script_exec.shape_result({
            "success": False,
            "result": "",
            "output": big + "Traceback: the actual error",
            "parsed": None,
            "hints": [],
        })

        self.assertLess(len(shaped["output"]), 9000)
        self.assertIn("[truncated", shaped["output"])
        self.assertIn("the actual error", shaped["output"])  # tail preserved

    def test_zero_limit_disables_truncation_and_compact_false_keeps_echo(self):
        big = "y" * 20000
        untouched = script_exec.shape_result(
            {"success": False, "result": "", "output": big, "parsed": None},
            max_output_chars=0,
        )
        self.assertEqual(untouched["output"], big)

        kept = script_exec.shape_result(
            {"success": True, "result": "r", "output": "o", "parsed": {"a": 1}},
            compact=False,
        )
        self.assertEqual(kept["output"], "o")
        self.assertNotIn("raw_omitted", kept)


class ScriptStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["UE_MCP_SCRIPT_DIR"] = self._tmp.name
        self.fake_server = _FakeServer()
        self.conn = _CapturingConnection()
        script_store_tools.register(self.fake_server, connection=self.conn)

    def tearDown(self):
        os.environ.pop("UE_MCP_SCRIPT_DIR", None)
        self._tmp.cleanup()

    def _call(self, tool, *args, **kwargs):
        return json.loads(asyncio.run(self.fake_server.tools[tool](*args, **kwargs))[0].text)

    def test_save_list_run_delete_roundtrip(self):
        saved = self._call(
            "save_script",
            "list-bones",
            "mesh = load(ARGS['mesh'])\nmcp_result({'mesh': str(mesh)})",
            description="List bones of a mesh",
        )
        self.assertTrue(saved["saved"])
        self.assertFalse(saved["replaced"])

        listing = self._call("list_scripts")
        self.assertEqual(listing["scripts"][0]["name"], "list-bones")
        self.assertEqual(listing["scripts"][0]["description"], "List bones of a mesh")

        self._call("run_script", "list-bones", args={"mesh": "/Game/X"})
        sent_code, mode, _ = self.conn.execute_calls[0]
        self.assertEqual(mode, "ExecuteFile")
        self.assertIn("def mcp_result", sent_code)  # prelude injected
        self.assertIn("ARGS = __import__('json').loads", sent_code)
        self.assertIn('"/Game/X"', sent_code.replace("\\", ""))
        self.assertIn("mesh = load(ARGS['mesh'])", sent_code)
        # The stored file's description header must not ship to Unreal.
        self.assertNotIn("# description:", sent_code)

        deleted = self._call("delete_script", "list-bones")
        self.assertTrue(deleted["deleted"])
        self.assertEqual(self._call("list_scripts")["scripts"], [])

    def test_save_rejects_future_imports(self):
        # run_script injects ARGS + prelude above the code, so a __future__
        # import would fail at run time with a confusing editor-side error.
        rejected = self._call(
            "save_script", "future-script",
            "from __future__ import annotations\nmcp_result({})",
        )
        self.assertTrue(rejected["error"])
        self.assertIn("inject_helpers=False", rejected["message"])
        self.assertEqual(self._call("list_scripts")["scripts"], [])

    def test_save_rejects_bad_names_and_bad_syntax(self):
        bad_name = self._call("save_script", "../escape", "print(1)")
        self.assertTrue(bad_name["error"])

        bad_syntax = self._call("save_script", "broken", "def broken(:")
        self.assertFalse(bad_syntax["success"])
        self.assertIn("SyntaxError", bad_syntax["result"])
        self.assertEqual(self._call("list_scripts")["scripts"], [])

    def test_run_unknown_script_points_at_list_scripts(self):
        missing = self._call("run_script", "nope")
        self.assertTrue(missing["error"])
        self.assertIn("list_scripts", missing["message"])

    def test_run_script_auto_connect_ambiguity_payload_is_preserved(self):
        class AmbiguousConnection(_CapturingConnection):
            def is_connected(self):
                return False

            def connect(self, node_id=None, timeout=5.0):
                raise uc.UEMultipleEditorsAmbiguousError([
                    {"node_id": "node-a", "project_name": "A", "_transport": "direct_udp"},
                    {"node_id": "node-b", "project_name": "B", "_transport": "direct_udp"},
                ])

            def execute(self, code, mode="ExecuteFile", timeout=None):
                raise AssertionError("execute must not run when editor selection is ambiguous")

        fake_server = _FakeServer()
        script_store_tools.register(fake_server, connection=AmbiguousConnection())
        asyncio.run(fake_server.tools["save_script"]("probe", "mcp_result({'ok': True})"))

        payload = json.loads(asyncio.run(fake_server.tools["run_script"]("probe"))[0].text)

        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])
        self.assertFalse(payload["message"].startswith("Auto-connect to Unreal Editor failed"))


class _HarvestSimConn(_CapturingConnection):
    """Plays the editor's role for build flows: answers the engine-version
    probe and 'writes' the harvest file the UE-side script would produce."""

    def __init__(self, engine, entries):
        super().__init__()
        self._engine = engine
        self._entries = entries

    def execute(self, code, mode="ExecuteFile", timeout=None):
        self.execute_calls.append((code, mode, timeout))
        base = {"success": True, "result": "", "output": "", "hints": []}
        if "ARFilter" in code:  # the harvest script; the probe has no registry pass
            tmp = api_index.catalog_path_for_version(self._engine)
            tmp = tmp.with_name(f"_harvest_tmp_{tmp.name}")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({
                "engine": self._engine, "harvested_at": 1.0,
                "entries": self._entries,
            }), encoding="utf-8")
            return {**base, "parsed": {"count": len(self._entries), "engine": self._engine}}
        return {**base, "parsed": {"engine": self._engine}}


class ApiCatalogTests(unittest.TestCase):
    _ENGINE = "TestEngine-1.0"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["UE_MCP_CATALOG_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("UE_MCP_CATALOG_DIR", None)
        self._tmp.cleanup()

    def _write_catalog(self):
        path = api_index.catalog_path_for_version(self._ENGINE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "engine": self._ENGINE,
            "harvested_at": 1.0,
            "entries": [
                {"n": "IKRetargeterController", "p": "", "k": "class",
                 "s": "Controller for editing IK Retargeter assets", "d": ""},
                {"n": "set_chain_settings", "p": "IKRetargeterController", "k": "method",
                 "s": "set_chain_settings(settings, chain) -> bool",
                 "d": "Set the settings of a retarget chain"},
                {"n": "get_bone_transform", "p": "SkeletalMeshComponent", "k": "method",
                 "s": "get_bone_transform(name) -> Transform", "d": "World bone transform"},
                {"n": "StaticMesh", "p": "", "k": "class",
                 "s": "A piece of static geometry", "d": ""},
                # Ancestor-chain depth: members live on the defining class.
                {"n": "SkeletalMeshComponent", "p": "", "k": "class",
                 "s": "Renders a skeletal mesh", "d": "",
                 "b": ["MeshComponent", "SceneComponent", "Object"]},
                {"n": "set_material", "p": "MeshComponent", "k": "method",
                 "s": "set_material(index, material)", "d": "Set a material"},
                {"n": "get_world_location", "p": "SceneComponent", "k": "method",
                 "s": "get_world_location() -> Vector", "d": ""},
                # Project layer: 'p' is the parent class, 's' the asset path.
                {"n": "B_SignUpViewModel", "p": "MVVMViewModelBase", "k": "widget",
                 "s": "/Game/ViewModels/Home/B_SignUpViewModel",
                 "d": "widget asset, parent MVVMViewModelBase"},
                {"n": "BP_Luna", "p": "Actor", "k": "blueprint",
                 "s": "/Game/MetaHumans/Luna/BP_Luna",
                 "d": "blueprint asset, parent Actor"},
            ],
        }), encoding="utf-8")
        return path

    def _register(self, conn):
        fake_server = _FakeServer()
        api_catalog_tools.register(fake_server, connection=conn)
        return fake_server

    def _call(self, server, tool, *args, **kwargs):
        return json.loads(asyncio.run(server.tools[tool](*args, **kwargs))[0].text)

    def test_search_ranks_method_and_filters_by_kind(self):
        self._write_catalog()
        server = self._register(_CapturingConnection())

        ranked = self._call(server, "search_unreal_api", "set chain settings")
        self.assertEqual(
            ranked["matches"][0]["symbol"],
            "IKRetargeterController.set_chain_settings",
        )

        classes_only = self._call(server, "search_unreal_api", "retargeter", kind="class")
        self.assertTrue(classes_only["matches"])
        self.assertTrue(all(m["kind"] == "class" for m in classes_only["matches"]))

    def test_search_recall_ladder_synonym_substring_fuzzy_and_miss_hint(self):
        path = api_index.catalog_path_for_version(self._ENGINE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "engine": self._ENGINE, "harvested_at": 1.0,
            "entries": [
                {"n": "IKRetargeterController", "p": "", "k": "class",
                 "s": "Controller for IK Retargeter assets", "d": ""},
                {"n": "set_actor_rotation", "p": "Actor", "k": "method",
                 "s": "set_actor_rotation(new_rotation) -> bool", "d": ""},
            ],
        }), encoding="utf-8")
        server = self._register(_CapturingConnection())

        # BM25 hit reports its mode.
        direct = self._call(server, "search_unreal_api", "retargeter controller")
        self.assertEqual(direct["match_mode"], "bm25")

        # 'rotator' is UE-speak; the entry says 'rotation' - synonym pass.
        syn = self._call(server, "search_unreal_api", "rotator")
        self.assertEqual(syn["match_mode"], "synonyms")
        self.assertEqual(syn["matches"][0]["symbol"], "Actor.set_actor_rotation")

        # No token boundary in the query - substring pass.
        sub = self._call(server, "search_unreal_api", "retargetercontrol")
        self.assertEqual(sub["match_mode"], "substring")
        self.assertEqual(sub["matches"][0]["symbol"], "IKRetargeterController")

        # Typo - fuzzy pass.
        fuzzy = self._call(server, "search_unreal_api", "retargetter controler")
        self.assertEqual(fuzzy["match_mode"], "fuzzy")
        self.assertEqual(fuzzy["matches"][0]["symbol"], "IKRetargeterController")

        # Total miss carries an actionable hint, not a bare empty list.
        miss = self._call(server, "search_unreal_api", "zzqq xkcd")
        self.assertEqual(miss["match_mode"], "none")
        self.assertEqual(miss["matches"], [])
        self.assertIn("force=true", miss["hint"])

    def test_describe_class_returns_ancestors_and_inherited_members(self):
        self._write_catalog()

        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        server = self._register(DisconnectedConn())

        shallow = self._call(server, "describe_unreal_api", "SkeletalMeshComponent")
        self.assertEqual(
            shallow["ancestors"], ["MeshComponent", "SceneComponent", "Object"]
        )
        self.assertNotIn("inherited", shallow)

        deep = self._call(
            server, "describe_unreal_api", "SkeletalMeshComponent",
            include_inherited=True,
        )
        self.assertEqual(deep["inherited"]["MeshComponent"], ["set_material"])
        self.assertEqual(deep["inherited"]["SceneComponent"], ["get_world_location"])
        # Own members still listed in full alongside the inherited map.
        symbols = [e["symbol"] for e in deep["entries"]]
        self.assertIn("SkeletalMeshComponent.get_bone_transform", symbols)

    def test_search_matches_classes_by_ancestor_name(self):
        self._write_catalog()
        server = self._register(_CapturingConnection())

        found = self._call(server, "search_unreal_api", "scene component", kind="class")

        self.assertIn(
            "SkeletalMeshComponent",
            [m["symbol"] for m in found["matches"]],
        )

    def test_kind_filter_surfaces_rare_kinds_behind_common_tokens(self):
        # 100 native classes share the query token; the single animbp entry
        # ranks below all of them, so a fixed over-fetch window would miss it.
        path = api_index.catalog_path_for_version(self._ENGINE)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"n": f"AnimNode{i}", "p": "", "k": "class", "s": "anim node", "d": ""}
            for i in range(100)
        ]
        entries.append({
            "n": "ABP_Hero", "p": "AnimInstance", "k": "animbp",
            "s": "/Game/Characters/Hero/ABP_Hero", "d": "animbp asset, parent AnimInstance",
        })
        path.write_text(json.dumps({
            "engine": self._ENGINE, "harvested_at": 1.0, "entries": entries,
        }), encoding="utf-8")
        server = self._register(_CapturingConnection())

        found = self._call(server, "search_unreal_api", "anim", limit=5, kind="animbp")

        self.assertEqual([m["symbol"] for m in found["matches"]], ["ABP_Hero"])

    def test_search_without_catalog_points_at_build_tool(self):
        server = self._register(_CapturingConnection())
        missing = self._call(server, "search_unreal_api", "anything")

        self.assertTrue(missing["error"])
        self.assertIn("build_api_catalog", missing["message"])

    def test_search_auto_builds_catalog_on_first_miss(self):
        conn = _HarvestSimConn(self._ENGINE, [
            {"n": "IKRetargeterController", "p": "", "k": "class",
             "s": "Controller for IK Retargeter assets", "d": ""},
        ])
        server = self._register(conn)

        found = self._call(server, "search_unreal_api", "retargeter controller")

        self.assertEqual(found["matches"][0]["symbol"], "IKRetargeterController")
        self.assertEqual(found["auto_built"]["engine"], self._ENGINE)
        self.assertTrue(api_index.catalog_path_for_version(self._ENGINE).exists())
        self.assertEqual(len(conn.execute_calls), 2)  # version probe + harvest

        again = self._call(server, "search_unreal_api", "retargeter controller")
        self.assertNotIn("auto_built", again)
        self.assertEqual(len(conn.execute_calls), 2)  # no re-harvest

    def test_search_does_not_build_when_disconnected(self):
        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        conn = DisconnectedConn()
        server = self._register(conn)

        missing = self._call(server, "search_unreal_api", "anything")

        self.assertTrue(missing["error"])
        self.assertEqual(conn.execute_calls, [])  # no probe, no harvest

    def test_describe_auto_builds_catalog_on_first_miss(self):
        conn = _HarvestSimConn(self._ENGINE, [
            {"n": "IKRetargeterController", "p": "", "k": "class",
             "s": "Controller for IK Retargeter assets", "d": ""},
        ])
        server = self._register(conn)

        described = self._call(server, "describe_unreal_api", "IKRetargeterController")

        # The fake's live getattr probe returns no doc, so the response comes
        # from the catalogue that the miss just built.
        self.assertEqual(described["source"], "catalog")
        self.assertEqual(
            described["entries"][0]["symbol"], "IKRetargeterController"
        )

    def test_harvest_and_version_scripts_compile(self):
        compile(api_index.ENGINE_VERSION_SCRIPT, "<version>", "exec")
        harvest = api_index.build_harvest_script("C:\\temp\\cat.json")
        compile(harvest, "<harvest>", "exec")
        self.assertIn("ARFilter", harvest)  # project-asset pass present
        self.assertIn("mro", harvest)       # ancestor-chain harvest present
        for kind in ("blueprint", "struct"):
            compile(
                api_index.build_asset_describe_script("/Game/X/BP_Foo", "BP_Foo", kind),
                "<asset-describe>", "exec",
            )

    def test_search_finds_project_blueprints_and_kind_filter(self):
        self._write_catalog()
        server = self._register(_CapturingConnection())

        ranked = self._call(server, "search_unreal_api", "signup viewmodel")
        top = ranked["matches"][0]
        self.assertEqual(top["symbol"], "B_SignUpViewModel")
        self.assertEqual(top["kind"], "widget")
        self.assertEqual(top["asset_path"], "/Game/ViewModels/Home/B_SignUpViewModel")
        self.assertEqual(top["parent"], "MVVMViewModelBase")

        bp_only = self._call(server, "search_unreal_api", "luna", kind="blueprint")
        self.assertTrue(bp_only["matches"])
        self.assertTrue(all(m["kind"] == "blueprint" for m in bp_only["matches"]))

    def test_describe_blueprint_accepts_generated_class_suffix(self):
        self._write_catalog()

        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        server = self._register(DisconnectedConn())
        described = self._call(server, "describe_unreal_api", "B_SignUpViewModel_C")

        self.assertEqual(described["source"], "catalog")
        self.assertEqual(described["entries"][0]["symbol"], "B_SignUpViewModel")
        self.assertEqual(
            described["entries"][0]["asset_path"],
            "/Game/ViewModels/Home/B_SignUpViewModel",
        )

    def test_describe_project_entry_survives_member_collision_cap(self):
        # 70 native classes define a member named 'Bar'; the blueprint asset
        # 'Bar' must still be in the (capped-at-60) describe entries.
        path = api_index.catalog_path_for_version(self._ENGINE)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"n": "Bar", "p": f"NativeClass{i}", "k": "method", "s": "x.bar()", "d": ""}
            for i in range(70)
        ]
        entries.append({
            "n": "Bar", "p": "Actor", "k": "blueprint",
            "s": "/Game/Stuff/Bar", "d": "blueprint asset, parent Actor",
        })
        path.write_text(json.dumps({
            "engine": self._ENGINE, "harvested_at": 1.0, "entries": entries,
        }), encoding="utf-8")

        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        server = self._register(DisconnectedConn())
        described = self._call(server, "describe_unreal_api", "Bar")

        self.assertEqual(described["entries"][0]["asset_path"], "/Game/Stuff/Bar")

    def test_describe_parent_class_does_not_dump_derived_blueprints(self):
        # BP_Luna has parent 'Actor'; describing 'Actor' must not return it
        # (for module entries 'p' means containing class, for project entries
        # it means parent class - only the former is a member listing).
        self._write_catalog()

        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        server = self._register(DisconnectedConn())
        described = self._call(server, "describe_unreal_api", "Actor")
        self.assertTrue(described["error"])

    def test_describe_blueprint_live_loads_asset(self):
        self._write_catalog()
        conn = _CapturingConnection(result={
            "success": True, "result": "", "output": "",
            "parsed": {
                "asset_path": "/Game/ViewModels/Home/B_SignUpViewModel",
                "asset_class": "WidgetBlueprint",
                "parent_class": "MVVMViewModelBase",
                "generated_class": "B_SignUpViewModel_C",
            },
            "hints": [],
        })
        server = self._register(conn)

        described = self._call(server, "describe_unreal_api", "B_SignUpViewModel")

        self.assertEqual(described["source"], "editor")
        self.assertEqual(described["symbol"], "B_SignUpViewModel")
        self.assertEqual(described["kind"], "widget")
        self.assertEqual(described["generated_class"], "B_SignUpViewModel_C")
        sent_code = conn.execute_calls[0][0]
        self.assertIn('load_asset("/Game/ViewModels/Home/B_SignUpViewModel")', sent_code)
        self.assertIn("B_SignUpViewModel_C", sent_code)  # generated-class probe

    def test_build_skips_harvest_when_cached(self):
        self._write_catalog()
        conn = _CapturingConnection(result={
            "success": True, "result": "", "output": "",
            "parsed": {"engine": self._ENGINE}, "hints": [],
        })
        server = self._register(conn)

        built = self._call(server, "build_api_catalog")

        self.assertTrue(built["cached"])
        self.assertFalse(built["built"])
        self.assertEqual(built["entry_count"], 9)
        # Only the engine-version probe ran; no harvest execution.
        self.assertEqual(len(conn.execute_calls), 1)

    def test_describe_uses_catalog_when_disconnected(self):
        self._write_catalog()

        class DisconnectedConn(_CapturingConnection):
            def is_connected(self):
                return False

        server = self._register(DisconnectedConn())
        described = self._call(server, "describe_unreal_api", "IKRetargeterController")

        self.assertEqual(described["source"], "catalog")
        symbols = [e["symbol"] for e in described["entries"]]
        self.assertIn("IKRetargeterController", symbols)
        self.assertIn("IKRetargeterController.set_chain_settings", symbols)

    def test_describe_rejects_injection_shaped_symbols(self):
        server = self._register(_CapturingConnection())
        bad = self._call(server, "describe_unreal_api", "x'); import os #")
        self.assertTrue(bad["error"])


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

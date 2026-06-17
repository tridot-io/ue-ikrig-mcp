import asyncio
import json
import os
import socket
import types
import unittest

from ue_ikrig_mcp import ue_connection as uc
from ue_ikrig_mcp.tools import batch as batch_tools
from ue_ikrig_mcp.tools import connection as connection_tools


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


class _FakeConnection:
    def __init__(self):
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True

    def is_connected(self):
        return True

    def get_connected_node_id(self):
        return "fake-node"

    def get_status(self):
        return {
            "connected": True,
            "node_id": "fake-node",
            "configured_command_endpoint": ["0.0.0.0", 6777],
            "active_command_endpoint": ["127.0.0.1", 61234],
            "fallback_used": True,
            "fallback_reason": "EADDRINUSE",
        }

    def preflight_discovery(self, timeout=2.0, test_callback=True, callback_timeout=2.0):
        return {
            "ok": False,
            "classification": "NO_PONG_RECEIVED_UNPROVEN",
            "timeout_seconds": timeout,
            "test_callback": test_callback,
            "callback_timeout_seconds": callback_timeout,
        }


class ConnectionLifecycleTests(unittest.TestCase):
    def _assert_multiple_editors_payload(self, payload):
        self.assertTrue(payload["error"])
        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertIn("Multiple Unreal Editor instances", payload["message"])
        self.assertIn("node_id", payload["next_action"])
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])

    def test_address_list_helpers_split_and_dedupe(self):
        self.assertEqual(
            uc._split_address_list("127.0.0.1, 0.0.0.0;127.0.0.1", ["fallback"]),
            ["127.0.0.1", "0.0.0.0"],
        )
        self.assertEqual(uc._split_address_list("", ["0.0.0.0"]), ["0.0.0.0"])

    def test_multicast_socket_candidates_keep_bind_interface_membership_separate(self):
        candidates = uc._multicast_socket_candidates(
            ["0.0.0.0"],
            ["172.30.1.10", "192.168.1.10"],
            ["0.0.0.0", "172.30.1.10"],
        )

        self.assertEqual(
            candidates,
            [
                ("0.0.0.0", "172.30.1.10", "0.0.0.0"),
                ("0.0.0.0", "172.30.1.10", "172.30.1.10"),
                ("0.0.0.0", "192.168.1.10", "0.0.0.0"),
                ("0.0.0.0", "192.168.1.10", "172.30.1.10"),
            ],
        )

    def test_callback_host_precedence(self):
        self.assertEqual(
            uc._callback_host_for("0.0.0.0", explicit_host="10.0.0.5", wsl_local_ip="172.30.1.10"),
            "10.0.0.5",
        )
        self.assertEqual(
            uc._callback_host_for("192.168.1.20", explicit_host="0.0.0.0", wsl_local_ip="172.30.1.10"),
            "192.168.1.20",
        )
        self.assertEqual(
            uc._callback_host_for("0.0.0.0", explicit_host="::", wsl_local_ip="172.30.1.10"),
            "172.30.1.10",
        )
        self.assertEqual(
            uc._callback_host_for("192.168.1.20", wsl_local_ip="172.30.1.10"),
            "192.168.1.20",
        )
        self.assertEqual(
            uc._callback_host_for("0.0.0.0", wsl_local_ip="172.30.1.10"),
            "172.30.1.10",
        )
        self.assertEqual(uc._callback_host_for("0.0.0.0"), "127.0.0.1")

    def test_wsl_local_ipv4_inference_uses_nameserver_as_route_target_only(self):
        seen = []
        original = uc._local_ipv4_for_remote
        try:
            uc._local_ipv4_for_remote = lambda remote_host: seen.append(remote_host) or "172.30.1.10"

            self.assertEqual(
                uc._infer_wsl_local_ipv4(is_wsl=True, nameserver="172.30.0.1"),
                "172.30.1.10",
            )
        finally:
            uc._local_ipv4_for_remote = original

        self.assertEqual(seen, ["172.30.0.1"])

    def test_wsl_callback_ipv4_prefers_multicast_route_over_nameserver_route(self):
        original_is_wsl = uc._is_wsl
        original_local_ipv4_for_remote = uc._local_ipv4_for_remote
        original_read_resolv_nameserver = uc._read_resolv_nameserver
        try:
            uc._is_wsl = lambda: True
            uc._read_resolv_nameserver = lambda: "10.255.255.254"
            uc._local_ipv4_for_remote = lambda remote_host: {
                "239.0.0.1": "192.168.2.76",
                "10.255.255.254": "10.255.255.254",
            }.get(remote_host)

            self.assertEqual(uc._infer_wsl_callback_ipv4("239.0.0.1"), "192.168.2.76")
        finally:
            uc._is_wsl = original_is_wsl
            uc._local_ipv4_for_remote = original_local_ipv4_for_remote
            uc._read_resolv_nameserver = original_read_resolv_nameserver

    def test_wsl_multicast_defaults_cross_windows_namespace(self):
        original_is_wsl = uc._is_wsl
        original_local_ipv4_for_remote = uc._local_ipv4_for_remote
        try:
            uc._is_wsl = lambda: True
            uc._local_ipv4_for_remote = lambda remote_host: "192.168.2.76"

            self.assertEqual(uc._default_multicast_ttl(), 1)
            self.assertEqual(
                uc._default_multicast_bind_candidates("239.0.0.1"),
                ["0.0.0.0", "239.0.0.1"],
            )
            self.assertEqual(
                uc._default_multicast_interface_candidates("239.0.0.1"),
                ["192.168.2.76", "0.0.0.0"],
            )
            self.assertEqual(
                uc._default_multicast_membership_candidates("239.0.0.1"),
                ["192.168.2.76", "0.0.0.0"],
            )
        finally:
            uc._is_wsl = original_is_wsl
            uc._local_ipv4_for_remote = original_local_ipv4_for_remote

    def test_non_wsl_multicast_defaults_preserve_local_namespace_behavior(self):
        original_is_wsl = uc._is_wsl
        original_local_ipv4_for_remote = uc._local_ipv4_for_remote
        try:
            uc._is_wsl = lambda: False
            uc._local_ipv4_for_remote = lambda remote_host: "192.168.2.76"

            self.assertEqual(uc._default_multicast_ttl(), 0)
            self.assertEqual(
                uc._default_multicast_bind_candidates("239.0.0.1"),
                ["0.0.0.0"],
            )
            self.assertEqual(uc._default_multicast_interface_candidates("239.0.0.1"), [])
            self.assertEqual(uc._default_multicast_membership_candidates("239.0.0.1"), [])
        finally:
            uc._is_wsl = original_is_wsl
            uc._local_ipv4_for_remote = original_local_ipv4_for_remote

    def test_fallback_policy_is_only_for_local_bind_errors(self):
        bind_error = OSError("Address already in use while binding command listener")
        bind_error.errno = getattr(uc.errno, "EADDRINUSE")

        self.assertTrue(uc.is_local_bind_error(bind_error))
        self.assertTrue(uc.should_attempt_command_port_fallback(bind_error, strict=False))
        self.assertFalse(uc.should_attempt_command_port_fallback(bind_error, strict=True))

        timeout_error = uc.UEConnectionError("Unreal Editor did not connect back within timeout.")
        self.assertFalse(uc.is_local_bind_error(timeout_error))
        self.assertFalse(uc.should_attempt_command_port_fallback(timeout_error, strict=False))

    def test_status_exposes_configured_active_and_fallback_endpoint(self):
        conn = uc.UEConnection(
            command_endpoint=("0.0.0.0", 6777),
            active_command_endpoint=("127.0.0.1", 61234),
            fallback_used=True,
            fallback_reason="EADDRINUSE",
        )

        status = conn.get_status()

        self.assertFalse(status["connected"])
        self.assertEqual(status["configured_command_endpoint"], ["0.0.0.0", 6777])
        self.assertEqual(status["active_command_endpoint"], ["127.0.0.1", 61234])
        self.assertTrue(status["fallback_used"])
        self.assertEqual(status["fallback_reason"], "EADDRINUSE")
        self.assertEqual(status["discovery"]["multicast_group"], ["239.0.0.1", 6766])
        self.assertEqual(status["discovered_nodes"], [])
        self.assertFalse(status["selection_required"])
        self.assertTrue(status["one_active_editor_per_process"])
        self.assertEqual(status["callback"]["advertised_host"], "127.0.0.1")
        self.assertIn("wsl_detected", status["callback"])
        self.assertIn("route_ipv4_to_multicast_group", status["network"])

    def test_connection_status_clears_direct_socket_when_peer_closed(self):
        class ClosedCommandSocket:
            def __init__(self):
                self.closed = False
                self.timeout_values = []

            def gettimeout(self):
                return None

            def settimeout(self, value):
                self.timeout_values.append(value)

            def recv(self, size, flags=0):
                self.peeked = (size, flags)
                return b""

            def close(self):
                self.closed = True

        conn = uc.UEConnection()
        command_socket = ClosedCommandSocket()
        conn._remote_node_id = "stale-node"
        conn._command_channel_socket = command_socket

        status = conn.get_status()

        self.assertFalse(status["connected"])
        self.assertIsNone(status["node_id"])
        self.assertIsNone(conn._command_channel_socket)
        self.assertTrue(command_socket.closed)
        self.assertEqual(status["connection_liveness"]["transport"], "direct_tcp")
        self.assertFalse(status["connection_liveness"]["ok"])

    def test_connection_status_clears_windows_bridge_when_node_disappears(self):
        class LostBridgeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _discover_windows_bridge_nodes(self, timeout=2.0):
                self.discovery_timeout = timeout
                self._last_windows_bridge_result = {
                    "ok": False,
                    "error": "No Unreal Editor instances discovered from Windows bridge.",
                }
                self._windows_bridge_node_ids = set()
                self._windows_bridge_nodes = []
                return []

        conn = LostBridgeConnection()
        conn._remote_node_id = "stale-node"
        conn._windows_bridge_connected = True
        conn._windows_bridge_node_ids = {"stale-node"}
        conn._windows_bridge_nodes = [{"node_id": "stale-node"}]

        status = conn.get_status()

        self.assertFalse(status["connected"])
        self.assertIsNone(status["node_id"])
        self.assertFalse(status["windows_bridge"]["connected"])
        self.assertEqual(status["connection_liveness"]["transport"], "windows_subprocess")
        self.assertFalse(status["connection_liveness"]["ok"])

    def test_status_flags_wildcard_callback_host_without_advertising_it(self):
        conn = uc.UEConnection(
            command_endpoint=("127.0.0.1", 6777),
            callback_host="0.0.0.0",
        )

        status = conn.get_status()

        self.assertEqual(status["callback"]["advertised_host"], "127.0.0.1")
        self.assertIn("config_error", status["callback"])
        self.assertIn("cannot be a wildcard", status["callback"]["config_error"])

    def test_preflight_classifies_udp_bind_failure(self):
        class BindFailConnection(uc.UEConnection):
            def start_discovery(self):
                raise uc.UEConnectionError("bind failed")

        result = BindFailConnection().preflight_discovery(timeout=0.1, test_callback=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "CLIENT_UDP_BIND_FAILED")
        self.assertIn("bind failed", result["error"])

    def test_preflight_no_pong_stops_before_callback(self):
        class NoPongConnection(uc.UEConnection):
            def start_discovery(self):
                self._running = True

            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("callback should not be attempted without a pong")

            def _windows_bridge_supported(self):
                return False

        result = NoPongConnection().preflight_discovery(timeout=0.1, test_callback=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "NO_PONG_RECEIVED_UNPROVEN")
        self.assertIn("UE_REMOTE_EXECUTION_DISABLED", result["possible_classifications"])

    def test_preflight_no_pong_retries_with_windows_bridge_when_supported(self):
        class BridgeAfterNoPongConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self.bridge_calls = []

            def start_discovery(self):
                self._running = True

            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now

            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                self.bridge_calls.append((payload, timeout))
                return {
                    "ok": True,
                    "nodes": [{
                        "node_id": "launched-node",
                        "project_name": "ProjectAvadot",
                    }],
                }

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("direct WSL callback should not be used for bridge retry")

        conn = BridgeAfterNoPongConnection()

        result = conn.preflight_discovery(timeout=0.1, test_callback=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "PONG_RECEIVED_VIA_WINDOWS_BRIDGE")
        self.assertEqual(conn.bridge_calls[0][0]["op"], "discover")
        self.assertEqual(conn.bridge_calls[0][0]["group"], ["239.0.0.1", 6766])
        self.assertGreaterEqual(conn.bridge_calls[0][0]["ttl"], 1)
        self.assertEqual(result["nodes"][0]["node_id"], "launched-node")
        self.assertEqual(result["nodes"][0]["_transport"], "windows_subprocess")

    def test_preflight_bridge_discovery_marks_callback_not_run(self):
        class BridgeAfterNoPongConnection(uc.UEConnection):
            def start_discovery(self):
                self._running = True

            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now

            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                return {
                    "ok": True,
                    "nodes": [{
                        "node_id": "launched-node",
                        "project_name": "ProjectAvadot",
                    }],
                }

        result = BridgeAfterNoPongConnection().preflight_discovery(
            timeout=0.1,
            test_callback=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["classification"], "PONG_RECEIVED_VIA_WINDOWS_BRIDGE")
        self.assertEqual(result["callback_classification"], "NOT_RUN_WINDOWS_BRIDGE_DISCOVERY_ONLY")

    def test_get_remote_nodes_uses_windows_bridge_when_wsl_multicast_has_no_nodes(self):
        class BridgeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                self.bridge_payload = payload
                return {
                    "ok": True,
                    "nodes": [{
                        "node_id": "win-node",
                        "project_name": "ProjectAvadot",
                    }],
                }

        conn = BridgeConnection()
        conn._running = True

        nodes = conn.get_remote_nodes()

        self.assertEqual(nodes[0]["node_id"], "win-node")
        self.assertEqual(nodes[0]["_transport"], "windows_subprocess")
        self.assertIn("win-node", conn._windows_bridge_node_ids)
        self.assertEqual(conn.bridge_payload["op"], "discover")

    def test_get_remote_nodes_preserves_multiple_windows_bridge_nodes(self):
        class BridgeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                self.bridge_payload = payload
                return {
                    "ok": True,
                    "nodes": [
                        {"node_id": "node-a", "project_name": "ProjectA"},
                        {"node_id": "node-b", "project_name": "ProjectB"},
                    ],
                }

        conn = BridgeConnection()
        conn._running = True

        nodes = conn.get_remote_nodes()

        self.assertEqual([node["node_id"] for node in nodes], ["node-a", "node-b"])
        self.assertEqual([node["_transport"] for node in nodes], ["windows_subprocess", "windows_subprocess"])
        self.assertEqual(conn._windows_bridge_node_ids, {"node-a", "node-b"})
        self.assertEqual(conn.bridge_payload["op"], "discover")

    def test_direct_connect_without_node_id_raises_multiple_editors_payload(self):
        self.assertTrue(
            hasattr(uc, "UEMultipleEditorsAmbiguousError"),
            "UEConnection.connect must expose a dedicated multi-editor ambiguity exception",
        )

        class MultiNodeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._running = True

            def _windows_bridge_supported(self):
                return False

            def get_remote_nodes(self):
                return [
                    {"node_id": "node-a", "project_name": "ProjectA"},
                    {"node_id": "node-b", "project_name": "ProjectB"},
                ]

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("ambiguous no-node_id connect must not open a command channel")

        with self.assertRaises(uc.UEMultipleEditorsAmbiguousError) as raised:
            MultiNodeConnection().connect()

        self._assert_multiple_editors_payload(raised.exception.to_payload())

    def test_windows_bridge_connect_without_node_id_raises_multiple_editors_payload(self):
        self.assertTrue(
            hasattr(uc, "UEMultipleEditorsAmbiguousError"),
            "UEConnection.connect must expose a dedicated multi-editor ambiguity exception",
        )

        class BridgeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                if payload["op"] == "discover":
                    return {
                        "ok": True,
                        "nodes": [
                            {"node_id": "node-a", "project_name": "ProjectA"},
                            {"node_id": "node-b", "project_name": "ProjectB"},
                        ],
                    }
                raise AssertionError(payload)

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("ambiguous bridge connect must not fall back to direct TCP")

        with self.assertRaises(uc.UEMultipleEditorsAmbiguousError) as raised:
            BridgeConnection().connect()

        payload = raised.exception.to_payload()
        self._assert_multiple_editors_payload(payload)
        self.assertEqual([node["_transport"] for node in payload["nodes"]], ["windows_subprocess", "windows_subprocess"])

    def test_explicit_node_id_selects_one_direct_node_from_many(self):
        class MultiNodeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._running = True
                self.opened_node_ids = []

            def _windows_bridge_supported(self):
                return False

            def get_remote_nodes(self):
                return [
                    {"node_id": "node-a", "project_name": "ProjectA"},
                    {"node_id": "node-b", "project_name": "ProjectB"},
                ]

            def _open_command_channel_with_fallback(self, node_id, *args, **kwargs):
                self.opened_node_ids.append(node_id)

        conn = MultiNodeConnection()

        conn.connect(node_id="node-b")

        self.assertEqual(conn._remote_node_id, "node-b")
        self.assertEqual(conn.opened_node_ids, ["node-b"])

    def test_already_connected_without_node_id_reuses_active_node_before_ambiguity_check(self):
        class ConnectedMultiNodeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._remote_node_id = "node-a"
                self.discovery_calls = 0
                self.opened_node_ids = []

            def is_connected(self):
                return True

            def _refresh_connection_liveness(self):
                return {
                    "transport": "direct_tcp",
                    "ok": True,
                    "state": "open",
                    "node_id": self._remote_node_id,
                }

            def get_remote_nodes(self):
                self.discovery_calls += 1
                return [
                    {"node_id": "node-a", "project_name": "ProjectA"},
                    {"node_id": "node-b", "project_name": "ProjectB"},
                ]

            def _open_command_channel_with_fallback(self, node_id, *args, **kwargs):
                self.opened_node_ids.append(node_id)

        conn = ConnectedMultiNodeConnection()

        conn.connect()

        self.assertEqual(conn._remote_node_id, "node-a")
        self.assertEqual(conn.discovery_calls, 0)
        self.assertEqual(conn.opened_node_ids, [])

    def test_connection_status_exposes_discovered_nodes_and_selection_required(self):
        class StatusConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._running = True
                self._nodes.update("node-a", {"project_name": "ProjectA"})
                self._nodes.update("node-b", {"project_name": "ProjectB"})

            def _windows_bridge_supported(self):
                return False

            def _refresh_connection_liveness(self):
                self._mark_transport_disconnected()
                return {
                    "transport": None,
                    "ok": False,
                    "state": "not_connected",
                    "timeout_seconds": uc.CONNECTION_STATUS_TIMEOUT,
                }

        status = StatusConnection().get_status()

        self.assertFalse(status["connected"])
        self.assertIsNone(status["node_id"])
        self.assertIs(status["one_active_editor_per_process"], True)
        self.assertIs(status["selection_required"], True)
        self.assertEqual([node["node_id"] for node in status["discovered_nodes"]], ["node-a", "node-b"])
        self.assertEqual(status["selection_error_code"], "MULTIPLE_EDITORS_DISCOVERED")

    def test_connection_status_suppresses_selection_required_for_active_reuse(self):
        class ConnectedStatusConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self._remote_node_id = "node-a"
                self._nodes.update("node-a", {"project_name": "ProjectA"})
                self._nodes.update("node-b", {"project_name": "ProjectB"})

            def is_connected(self):
                return True

            def _refresh_connection_liveness(self):
                return {
                    "transport": "direct_tcp",
                    "ok": True,
                    "state": "open",
                    "node_id": "node-a",
                    "timeout_seconds": uc.CONNECTION_STATUS_TIMEOUT,
                }

        status = ConnectedStatusConnection().get_status()

        self.assertTrue(status["connected"])
        self.assertEqual(status["node_id"], "node-a")
        self.assertIs(status["one_active_editor_per_process"], True)
        self.assertIs(status["selection_required"], False)
        self.assertEqual([node["node_id"] for node in status["discovered_nodes"]], ["node-a", "node-b"])

    def test_connect_and_execute_can_use_windows_bridge_transport(self):
        class BridgeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                if payload["op"] == "discover":
                    return {
                        "ok": True,
                        "nodes": [{
                            "node_id": "win-node",
                            "project_name": "ProjectAvadot",
                        }],
                    }
                if payload["op"] == "execute":
                    self.execute_payload = payload
                    return {
                        "ok": True,
                        "result": {
                            "success": True,
                            "result": "None",
                            "output": "__MCP_RESULT__{\"ok\": true}",
                        },
                    }
                raise AssertionError(payload)

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("direct WSL callback should not be used for bridge nodes")

            def start_discovery(self):
                raise AssertionError("bridge connect should not start direct WSL discovery first")

        conn = BridgeConnection()

        conn.connect()
        result = conn.execute("print('hi')", mode="ExecuteStatement")

        self.assertTrue(conn.is_connected())
        self.assertTrue(result["success"])
        self.assertEqual(result["parsed"], {"ok": True})
        self.assertEqual(conn.execute_payload["node_id"], "win-node")
        self.assertEqual(conn.execute_payload["mode"], "ExecuteStatement")

    def test_connect_omitted_node_id_raises_ambiguity_for_direct_multi_node_discovery(self):
        class DirectMultiNodeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self.opened = []

            def _windows_bridge_supported(self):
                return False

            def start_discovery(self):
                self._running = True
                self._nodes.update("node-a", {"project_name": "A"})
                self._nodes.update("node-b", {"project_name": "B"})

            def _windows_bridge_supported(self):
                return False

            def _open_command_channel_with_fallback(self, node_id, *args, **kwargs):
                self.opened.append(node_id)

        conn = DirectMultiNodeConnection()

        with self.assertRaises(uc.UEMultipleEditorsAmbiguousError) as ctx:
            conn.connect(timeout=0.05)

        payload = ctx.exception.to_payload()
        self.assertTrue(payload["error"])
        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertIn("Retry with node_id", payload["message"])
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])
        self.assertEqual(
            payload["next_action"],
            "Call connect_to_editor(node_id=<one of nodes[].node_id>).",
        )
        self.assertEqual(conn.opened, [])

        conn.connect(node_id="node-b", timeout=0.05)
        self.assertEqual(conn.opened, ["node-b"])

    def test_connect_omitted_node_id_raises_ambiguity_for_bridge_multi_node_discovery(self):
        class BridgeMultiNodeConnection(uc.UEConnection):
            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                if payload["op"] == "discover":
                    return {
                        "ok": True,
                        "nodes": [
                            {"node_id": "win-a", "project_name": "A"},
                            {"node_id": "win-b", "project_name": "B"},
                        ],
                    }
                raise AssertionError(payload)

            def start_discovery(self):
                raise AssertionError("bridge ambiguity should be resolved before direct discovery")

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("bridge nodes should not use direct callback")

        conn = BridgeMultiNodeConnection()

        with self.assertRaises(uc.UEMultipleEditorsAmbiguousError) as ctx:
            conn.connect()

        payload = ctx.exception.to_payload()
        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["win-a", "win-b"])
        self.assertEqual(payload["nodes"][0]["_transport"], "windows_subprocess")

        conn.connect(node_id="win-b")
        self.assertTrue(conn.is_connected())
        self.assertEqual(conn.get_connected_node_id(), "win-b")

    def test_connect_omitted_node_id_reuses_valid_active_editor_before_ambiguity(self):
        class ConnectedBridgeConnection(uc.UEConnection):
            def _refresh_connection_liveness(self):
                return {
                    "transport": "windows_subprocess",
                    "ok": True,
                    "state": "test_connected",
                    "node_id": self._remote_node_id,
                }

            def get_remote_nodes(self):
                raise AssertionError("valid active editor must be reused before discovery")

        conn = ConnectedBridgeConnection()
        conn._windows_bridge_connected = True
        conn._remote_node_id = "win-a"
        conn._windows_bridge_nodes = [
            {"node_id": "win-a", "project_name": "A", "_transport": "windows_subprocess"},
            {"node_id": "win-b", "project_name": "B", "_transport": "windows_subprocess"},
        ]
        conn._windows_bridge_node_ids = {"win-a", "win-b"}

        conn.connect()
        status = conn.get_status()

        self.assertEqual(conn.get_connected_node_id(), "win-a")
        self.assertTrue(status["connected"])
        self.assertEqual(status["node_id"], "win-a")
        self.assertEqual(status["active_node_id"], "win-a")
        self.assertFalse(status["selection_required"])
        self.assertTrue(status["one_active_editor_per_process"])
        self.assertEqual([node["node_id"] for node in status["discovered_nodes"]], ["win-a", "win-b"])

    def test_two_direct_connections_to_same_node_keep_distinct_local_ports_and_disconnect_isolated(self):
        class LoopbackConnection(uc.UEConnection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.peer_sockets = []

            def _send_broadcast(self, msg):
                if msg.type_ != uc._TYPE_OPEN_CONNECTION:
                    return
                peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                peer.connect((msg.data["command_ip"], msg.data["command_port"]))
                self.peer_sockets.append(peer)

            def close_peers(self):
                for peer in self.peer_sockets:
                    try:
                        peer.close()
                    except OSError:
                        pass
                self.peer_sockets = []

        port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_probe.bind(("127.0.0.1", 0))
        shared_port = port_probe.getsockname()[1]
        port_probe.close()

        first = LoopbackConnection(command_endpoint=("127.0.0.1", shared_port))
        second = LoopbackConnection(command_endpoint=("127.0.0.1", shared_port))
        try:
            first._remote_node_id = "shared-node"
            second._remote_node_id = "shared-node"

            first._open_command_channel_with_fallback(
                "shared-node",
                accept_timeout=0.1,
                attempts=1,
            )
            second._open_command_channel_with_fallback(
                "shared-node",
                accept_timeout=0.1,
                attempts=1,
            )

            first_status = first.get_status()
            second_status = second.get_status()

            self.assertTrue(first_status["connected"])
            self.assertTrue(second_status["connected"])
            self.assertEqual(first_status["node_id"], "shared-node")
            self.assertEqual(second_status["node_id"], "shared-node")
            self.assertEqual(first_status["active_command_endpoint"], ["127.0.0.1", shared_port])
            self.assertFalse(first_status["fallback_used"])
            self.assertTrue(second_status["fallback_used"])
            self.assertEqual(second_status["configured_command_endpoint"], ["127.0.0.1", shared_port])
            self.assertNotEqual(second_status["active_command_endpoint"][1], shared_port)
            self.assertEqual(first._last_callback_request["node_id"], "shared-node")
            self.assertEqual(second._last_callback_request["node_id"], "shared-node")

            second_active_endpoint = list(second_status["active_command_endpoint"])
            first.disconnect()

            second_after_disconnect = second.get_status()
            self.assertTrue(second_after_disconnect["connected"])
            self.assertEqual(second_after_disconnect["node_id"], "shared-node")
            self.assertEqual(second_after_disconnect["active_command_endpoint"], second_active_endpoint)
        finally:
            first.disconnect()
            second.disconnect()
            first.close_peers()
            second.close_peers()

    def test_two_bridge_connections_to_same_node_keep_local_state_isolated(self):
        class BridgeConnection(uc.UEConnection):
            def __init__(self):
                super().__init__()
                self.execute_payloads = []

            def _windows_bridge_supported(self):
                return True

            def _run_windows_bridge(self, payload, timeout=10.0):
                if payload["op"] == "discover":
                    return {
                        "ok": True,
                        "nodes": [{
                            "node_id": "shared-node",
                            "project_name": "ProjectAvadot",
                        }],
                    }
                if payload["op"] == "execute":
                    self.execute_payloads.append(dict(payload))
                    return {
                        "ok": True,
                        "result": {
                            "success": True,
                            "result": "None",
                            "output": "__MCP_RESULT__{\"ok\": true}",
                        },
                    }
                raise AssertionError(payload)

        first = BridgeConnection()
        second = BridgeConnection()
        try:
            first.connect()
            second.connect()

            self.assertTrue(first.is_connected())
            self.assertTrue(second.is_connected())
            self.assertEqual(first.get_connected_node_id(), "shared-node")
            self.assertEqual(second.get_connected_node_id(), "shared-node")

            self.assertTrue(first.execute("print('first')")["success"])
            self.assertTrue(second.execute("print('second')")["success"])

            first.disconnect()

            second_status = second.get_status()
            self.assertTrue(second_status["connected"])
            self.assertTrue(second_status["windows_bridge"]["connected"])
            self.assertEqual(second_status["node_id"], "shared-node")

            after_disconnect = second.execute("print('after disconnect')")
            self.assertTrue(after_disconnect["success"])
            self.assertEqual(
                [payload["node_id"] for payload in second.execute_payloads],
                ["shared-node", "shared-node"],
            )
        finally:
            first.disconnect()
            second.disconnect()

    def test_direct_execute_uses_bounded_socket_timeout_for_stalled_editor(self):
        class SilentCommandSocket:
            def __init__(self):
                self.timeout_values = []
                self.closed = False

            def gettimeout(self):
                return None

            def settimeout(self, value):
                self.timeout_values.append(value)

            def sendall(self, data):
                self.sent = data

            def recv(self, size):
                if not self.timeout_values:
                    raise AssertionError("recv called before configuring a timeout")
                raise uc.socket.timeout("timed out")

            def close(self):
                self.closed = True

        conn = uc.UEConnection()
        command_socket = SilentCommandSocket()
        conn._remote_node_id = "node-1"
        conn._command_channel_socket = command_socket

        with self.assertRaisesRegex(uc.UEConnectionError, "timed out"):
            conn.execute("print('hi')")

        self.assertGreater(command_socket.timeout_values[0], 0)
        self.assertTrue(command_socket.closed)
        self.assertIsNone(conn._command_channel_socket)

    def test_direct_execute_honors_per_call_timeout_for_stalled_editor(self):
        class SilentCommandSocket:
            def __init__(self):
                self.timeout_values = []

            def gettimeout(self):
                return None

            def settimeout(self, value):
                self.timeout_values.append(value)

            def sendall(self, data):
                self.sent = data

            def recv(self, size, flags=0):
                raise uc.socket.timeout("timed out")

            def close(self):
                self.closed = True

        conn = uc.UEConnection()
        command_socket = SilentCommandSocket()
        conn._remote_node_id = "node-1"
        conn._command_channel_socket = command_socket

        with self.assertRaisesRegex(uc.UEConnectionError, "0.25 seconds"):
            conn.execute("print('hi')", timeout=0.25)

        self.assertIn(0.25, command_socket.timeout_values)

    def test_execute_python_tool_forwards_mode_and_timeout_and_returns_transport_errors(self):
        class TimeoutConnection:
            def __init__(self):
                self.calls = []

            def execute(self, code, mode="ExecuteFile", timeout=None):
                self.calls.append((code, mode, timeout))
                raise uc.UEConnectionError("Command channel timed out after 0.25 seconds")

        fake_server = _FakeServer()
        fake_conn = TimeoutConnection()
        original_get_connection = batch_tools.get_connection
        try:
            batch_tools.get_connection = lambda: fake_conn
            batch_tools.register(fake_server)

            result = asyncio.run(
                fake_server.tools["execute_python"](
                    "print('hi')",
                    mode="ExecuteStatement",
                    timeout_seconds=0.25,
                )
            )
        finally:
            batch_tools.get_connection = original_get_connection

        payload = json.loads(result[0].text)
        self.assertEqual(fake_conn.calls, [("print('hi')", "ExecuteStatement", 0.25)])
        self.assertTrue(payload["error"])
        self.assertIn("timed out", payload["message"])

    def test_windows_python_launcher_honors_explicit_windows_path(self):
        original_env = os.environ.get("UE_WINDOWS_PYTHON")
        original_exists = uc.os.path.exists
        original_which = uc.shutil.which
        try:
            os.environ["UE_WINDOWS_PYTHON"] = r"C:\Users\me\AppData\Local\Python\bin\python.exe"
            uc.os.path.exists = lambda path: path == "/mnt/c/Users/me/AppData/Local/Python/bin/python.exe"
            uc.shutil.which = lambda name: None

            command, diagnostics = uc._find_windows_python_launcher()
        finally:
            if original_env is None:
                os.environ.pop("UE_WINDOWS_PYTHON", None)
            else:
                os.environ["UE_WINDOWS_PYTHON"] = original_env
            uc.os.path.exists = original_exists
            uc.shutil.which = original_which

        self.assertEqual(command, ["/mnt/c/Users/me/AppData/Local/Python/bin/python.exe"])
        self.assertEqual(diagnostics["source"], "UE_WINDOWS_PYTHON")

    def test_run_windows_bridge_prefers_direct_windows_python_launcher(self):
        original_candidates = uc._windows_bridge_launcher_candidates
        original_powershell = uc._find_powershell_executable
        original_run = uc.subprocess.run
        original_wsl_path = uc._wsl_path_to_windows
        captured = {}
        try:
            uc._windows_bridge_launcher_candidates = lambda: ([
                (
                    ["/mnt/c/Users/me/AppData/Local/Python/bin/python.exe"],
                    {"type": "direct_python", "source": "test"},
                ),
            ],
                {"type": "available"},
            )
            uc._find_powershell_executable = lambda: (_ for _ in ()).throw(
                AssertionError("PowerShell should not be consulted when direct Windows Python is available")
            )
            uc._wsl_path_to_windows = lambda path: "C:\\bridge\\" + os.path.basename(path)

            def fake_run(args, **kwargs):
                captured["args"] = list(args)
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=uc._WINDOWS_BRIDGE_RESULT_PREFIX + json.dumps({"ok": True, "nodes": []}) + "\n",
                    stderr="",
                )

            uc.subprocess.run = fake_run

            result = uc.UEConnection()._run_windows_bridge(
                {"op": "discover", "group": ["239.0.0.1", 6766], "ttl": 1, "timeout": 0.1},
                timeout=1,
            )
        finally:
            uc._windows_bridge_launcher_candidates = original_candidates
            uc._find_powershell_executable = original_powershell
            uc.subprocess.run = original_run
            uc._wsl_path_to_windows = original_wsl_path

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][0], "/mnt/c/Users/me/AppData/Local/Python/bin/python.exe")
        self.assertTrue(captured["args"][1].startswith("C:\\bridge\\"))
        self.assertTrue(captured["args"][2].startswith("C:\\bridge\\"))
        self.assertEqual(result["_bridge_launcher"]["type"], "direct_python")

    def test_run_windows_bridge_retries_next_auto_launcher_after_launcher_failure(self):
        original_candidates = uc._windows_bridge_launcher_candidates
        original_run = uc.subprocess.run
        original_wsl_path = uc._wsl_path_to_windows
        calls = []
        try:
            uc._windows_bridge_launcher_candidates = lambda: ([
                (
                    ["/mnt/c/bad/python.exe"],
                    {"type": "direct_python", "source": "repo .venv-win", "explicit": False},
                ),
                (
                    ["/mnt/c/good/python.exe"],
                    {"type": "direct_python", "source": "Windows user Python", "explicit": False},
                ),
            ],
                {"type": "available"},
            )
            uc._wsl_path_to_windows = lambda path: "C:\\bridge\\" + os.path.basename(path)

            def fake_run(args, **kwargs):
                calls.append(list(args))
                if args[0] == "/mnt/c/bad/python.exe":
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="bad launcher")
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=uc._WINDOWS_BRIDGE_RESULT_PREFIX + json.dumps({"ok": True, "nodes": []}) + "\n",
                    stderr="",
                )

            uc.subprocess.run = fake_run

            result = uc.UEConnection()._run_windows_bridge(
                {"op": "discover", "group": ["239.0.0.1", 6766], "ttl": 1, "timeout": 0.1},
                timeout=1,
            )
        finally:
            uc._windows_bridge_launcher_candidates = original_candidates
            uc.subprocess.run = original_run
            uc._wsl_path_to_windows = original_wsl_path

        self.assertTrue(result["ok"])
        self.assertEqual([call[0] for call in calls], ["/mnt/c/bad/python.exe", "/mnt/c/good/python.exe"])
        self.assertEqual(result["_bridge_launcher"]["source"], "Windows user Python")
        self.assertEqual(result["_bridge_launcher_failures"][0]["source"], "repo .venv-win")

    def test_py_exe_direct_launcher_uses_python3_switch(self):
        original_candidates = uc._candidate_windows_python_paths
        original_exists = uc.os.path.exists
        original_powershell = uc._find_powershell_executable
        try:
            uc._candidate_windows_python_paths = lambda: [("PATH py.exe", "/mnt/c/Windows/py.exe")]
            uc.os.path.exists = lambda path: path == "/mnt/c/Windows/py.exe"
            uc._find_powershell_executable = lambda: None

            candidates, _diagnostics = uc._windows_bridge_launcher_candidates()
        finally:
            uc._candidate_windows_python_paths = original_candidates
            uc.os.path.exists = original_exists
            uc._find_powershell_executable = original_powershell

        self.assertEqual(candidates[0][0], ["/mnt/c/Windows/py.exe", "-3"])

    def test_missing_explicit_windows_python_does_not_fall_back_to_auto_candidates(self):
        original_candidates = uc._candidate_windows_python_paths
        original_exists = uc.os.path.exists
        original_powershell = uc._find_powershell_executable
        try:
            uc._candidate_windows_python_paths = lambda: [
                ("UE_WINDOWS_PYTHON", "/mnt/c/missing/python.exe"),
                ("repo .venv-win", "/mnt/c/auto/python.exe"),
            ]
            uc.os.path.exists = lambda path: path == "/mnt/c/auto/python.exe"
            uc._find_powershell_executable = lambda: "/mnt/c/Windows/System32/powershell.exe"

            candidates, diagnostics = uc._windows_bridge_launcher_candidates()
        finally:
            uc._candidate_windows_python_paths = original_candidates
            uc.os.path.exists = original_exists
            uc._find_powershell_executable = original_powershell

        self.assertEqual(candidates, [])
        self.assertTrue(diagnostics["python"]["explicit_configured"])
        self.assertEqual(diagnostics["powershell_skipped"], "explicit Windows Python configured")

    def test_windows_bridge_supported_short_circuits_before_launcher_probe(self):
        original_enabled = uc.WINDOWS_BRIDGE_ENABLED
        original_is_wsl = uc._is_wsl
        original_launcher = uc._windows_bridge_launcher_candidates
        try:
            uc.WINDOWS_BRIDGE_ENABLED = False
            uc._is_wsl = lambda: (_ for _ in ()).throw(
                AssertionError("_is_wsl should not be probed when bridge is disabled")
            )
            uc._windows_bridge_launcher_candidates = lambda: (_ for _ in ()).throw(
                AssertionError("launcher should not be probed when bridge is disabled")
            )

            self.assertFalse(uc.UEConnection()._windows_bridge_supported())

            uc.WINDOWS_BRIDGE_ENABLED = True
            uc._is_wsl = lambda: False
            self.assertFalse(uc.UEConnection()._windows_bridge_supported())
        finally:
            uc.WINDOWS_BRIDGE_ENABLED = original_enabled
            uc._is_wsl = original_is_wsl
            uc._windows_bridge_launcher_candidates = original_launcher

    def test_status_exposes_package_and_windows_bridge_launcher_diagnostics(self):
        status = uc.UEConnection().get_status()

        self.assertEqual(status["package"]["name"], "ue-ikrig-mcp")
        self.assertIn("version", status["package"])
        self.assertTrue(status["package"]["source_file"].endswith("ue_connection.py"))
        self.assertIn("launcher", status["windows_bridge"])

    def test_preflight_stale_node_does_not_satisfy_fresh_pong_gate(self):
        class StaleNodeConnection(uc.UEConnection):
            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise AssertionError("callback should not be attempted for stale nodes")

            def _windows_bridge_supported(self):
                return False

        conn = StaleNodeConnection()
        conn._running = True
        conn._nodes.update("stale-node", {"project_name": "Old"})

        result = conn.preflight_discovery(timeout=0.1, test_callback=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "NO_PONG_RECEIVED_UNPROVEN")
        self.assertEqual(result["pong_count"], 0)
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["cached_nodes"][0]["node_id"], "stale-node")

    def test_preflight_callback_probe_restores_command_endpoint_state(self):
        class PongConnection(uc.UEConnection):
            def start_discovery(self):
                self._running = True

            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now
                self._nodes.update("node-1", {"project_name": "Example"})
                self._pong_events.append({
                    "timestamp": now,
                    "node_id": "node-1",
                    "source_address": ["127.0.0.1", 6766],
                    "data_keys": ["project_name"],
                })

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                self._active_command_endpoint = ("0.0.0.0", 61234)
                self._fallback_used = True
                self._fallback_reason = "test fallback"
                self._last_callback_request = {
                    "command_ip": "127.0.0.1",
                    "command_port": 61234,
                    "listen_host": "0.0.0.0",
                    "node_id": "node-1",
                }

        conn = PongConnection(command_endpoint=("0.0.0.0", 6777))

        result = conn.preflight_discovery(timeout=0.1, test_callback=True, callback_timeout=0.1)
        status = conn.get_status()

        self.assertTrue(result["ok"])
        self.assertEqual(result["callback_classification"], "CALLBACK_REACHABLE")
        self.assertEqual(result["callback_request"]["command_port"], 61234)
        self.assertEqual(status["active_command_endpoint"], ["0.0.0.0", 6777])
        self.assertFalse(status["fallback_used"])

    def test_preflight_unexpected_callback_error_is_not_reported_as_network_failure(self):
        class BuggyCallbackConnection(uc.UEConnection):
            def start_discovery(self):
                self._running = True

            def _broadcast_ping(self, now):
                self._last_ping = now
                self._last_ping_sent_at = now
                self._nodes.update("node-1", {"project_name": "Example"})
                self._pong_events.append({
                    "timestamp": now,
                    "node_id": "node-1",
                    "source_address": ["127.0.0.1", 6766],
                    "data_keys": ["project_name"],
                })

            def _open_command_channel_with_fallback(self, *args, **kwargs):
                raise RuntimeError("internal bug")

        result = BuggyCallbackConnection().preflight_discovery(
            timeout=0.1,
            test_callback=True,
            callback_timeout=0.1,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "CLIENT_INTERNAL_ERROR")
        self.assertIn("internal bug", result["internal_error"])

    def test_partial_udp_send_failures_are_preserved_in_diagnostics(self):
        class GoodSocket:
            def sendto(self, payload, address):
                return len(payload)

        class BadSocket:
            def sendto(self, payload, address):
                raise OSError("blocked")

        conn = uc.UEConnection()
        conn._broadcast_sockets = [GoodSocket(), BadSocket()]
        conn._active_multicast_sockets = [
            {"bind_address": "0.0.0.0", "interface_address": "0.0.0.0", "membership_address": "0.0.0.0"},
            {"bind_address": "0.0.0.0", "interface_address": "172.30.1.10", "membership_address": "172.30.1.10"},
        ]

        conn._send_broadcast(uc._Message(uc._TYPE_PING, conn._node_id))
        attempts = conn.get_status()["discovery"]["last_ping_send_attempts"]

        self.assertTrue(attempts[0]["success"])
        self.assertFalse(attempts[1]["success"])
        self.assertIn("blocked", attempts[1]["error"])

    def test_open_connection_advertises_actual_ephemeral_port_and_non_wildcard_host(self):
        captured = []

        class CaptureOpenConnection(uc.UEConnection):
            def _send_broadcast(self, msg):
                if msg.type_ == uc._TYPE_OPEN_CONNECTION:
                    captured.append(dict(msg.data))

        conn = CaptureOpenConnection(callback_host="0.0.0.0")

        with self.assertRaises(uc.UEConnectionError):
            conn._open_command_channel(
                "node-1",
                ("127.0.0.1", 0),
                accept_timeout=0.1,
                attempts=1,
            )

        self.assertEqual(captured[0]["command_ip"], "127.0.0.1")
        self.assertGreater(captured[0]["command_port"], 0)

    def test_disconnect_editor_tool_is_registered_and_calls_disconnect(self):
        fake_server = _FakeServer()
        fake_conn = _FakeConnection()
        connection_tools.register(fake_server, connection=fake_conn)

        self.assertIn("disconnect_editor", fake_server.tools)

        result = asyncio.run(fake_server.tools["disconnect_editor"]())
        payload = json.loads(result[0].text)

        self.assertTrue(fake_conn.disconnected)
        self.assertTrue(payload["disconnected"])
        self.assertEqual(payload["previous"]["node_id"], "fake-node")

    def test_connect_to_editor_preserves_multiple_editors_payload(self):
        class AmbiguousConnection:
            def connect(self, node_id=None):
                raise uc.UEMultipleEditorsAmbiguousError([
                    {"node_id": "node-a", "project_name": "A", "_transport": "direct_udp"},
                    {"node_id": "node-b", "project_name": "B", "_transport": "direct_udp"},
                ])

            def get_status(self):
                raise AssertionError("status should not run after ambiguity")

        fake_server = _FakeServer()
        connection_tools.register(fake_server, connection=AmbiguousConnection())

        payload = json.loads(asyncio.run(fake_server.tools["connect_to_editor"]())[0].text)

        self.assertEqual(payload["error_code"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual(payload["classification"], "MULTIPLE_EDITORS_DISCOVERED")
        self.assertEqual([node["node_id"] for node in payload["nodes"]], ["node-a", "node-b"])
        self.assertNotIn("connected", payload)

    def test_preflight_discovery_tool_is_registered_and_forwards_options(self):
        fake_server = _FakeServer()
        fake_conn = _FakeConnection()
        connection_tools.register(fake_server, connection=fake_conn)

        self.assertIn("preflight_discovery", fake_server.tools)

        result = asyncio.run(
            fake_server.tools["preflight_discovery"](
                timeout_seconds=0.25,
                test_callback=False,
                callback_timeout_seconds=0.5,
            )
        )
        payload = json.loads(result[0].text)

        self.assertEqual(payload["classification"], "NO_PONG_RECEIVED_UNPROVEN")
        self.assertEqual(payload["timeout_seconds"], 0.25)
        self.assertFalse(payload["test_callback"])
        self.assertEqual(payload["callback_timeout_seconds"], 0.5)


if __name__ == "__main__":
    unittest.main()

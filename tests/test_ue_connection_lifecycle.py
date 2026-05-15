import asyncio
import json
import socket
import unittest

from ue_ikrig_mcp import ue_connection as uc
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


def _occupy_port(host="127.0.0.1", port=0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((host, port))
    sock.listen(1)
    return sock, sock.getsockname()[1]


class ConnectionLifecycleTests(unittest.TestCase):
    def test_fallback_policy_is_only_for_local_bind_errors(self):
        bind_error = OSError("Address already in use while binding command listener")
        bind_error.errno = getattr(uc.errno, "EADDRINUSE")

        self.assertTrue(uc.is_local_bind_error(bind_error))
        self.assertTrue(uc.should_attempt_command_port_fallback(bind_error, strict=False))
        self.assertFalse(uc.should_attempt_command_port_fallback(bind_error, strict=True))

        timeout_error = uc.UEConnectionError("Unreal Editor did not connect back within timeout.")
        self.assertFalse(uc.is_local_bind_error(timeout_error))
        self.assertFalse(uc.should_attempt_command_port_fallback(timeout_error, strict=False))

    def test_allocator_uses_an_available_local_port(self):
        occupied, occupied_port = _occupy_port()
        self.addCleanup(occupied.close)

        endpoint = uc.allocate_fallback_command_endpoint(("127.0.0.1", occupied_port))

        self.assertEqual(endpoint[0], "127.0.0.1")
        self.assertNotEqual(endpoint[1], occupied_port)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(endpoint)
        finally:
            probe.close()

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


if __name__ == "__main__":
    unittest.main()

import json
from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError, UEConnectionError


def register(server, connection=None):
    """Register connection-related MCP tools on the given server instance."""

    def _conn():
        return connection if connection is not None else get_connection()

    @server.tool()
    async def preflight_discovery(
        timeout_seconds: float = 2.0,
        test_callback: bool = True,
        callback_timeout_seconds: float = 2.0,
    ) -> list[TextContent]:
        """
        Run deterministic UE Remote Execution transport diagnostics.

        This sends the exact UDP ping packet used by Unreal's Python Remote
        Execution protocol, waits for pongs, and only then optionally tests the
        TCP callback/open_connection path. It does not execute Python in Unreal.
        """
        conn = _conn()
        result = conn.preflight_discovery(
            timeout=timeout_seconds,
            test_callback=test_callback,
            callback_timeout=callback_timeout_seconds,
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def discover_editors() -> list[TextContent]:
        """Discover running Unreal Editor instances on the local network."""
        conn = _conn()
        nodes = conn.get_remote_nodes()
        if nodes:
            return [TextContent(type="text", text=json.dumps(nodes, indent=2))]
        if not conn._running:
            conn.start_discovery()
            import asyncio
            await asyncio.sleep(2.0)
        nodes = conn.get_remote_nodes()
        return [TextContent(type="text", text=json.dumps(nodes, indent=2))]

    @server.tool()
    async def connect_to_editor(node_id: str = None) -> list[TextContent]:
        """
        Connect to a running Unreal Editor instance.

        Args:
            node_id: Optional node ID to connect to. If omitted, connects to
                     the first discovered editor.
        """
        conn = _conn()
        try:
            conn.connect(node_id=node_id)
            result = conn.get_status()
        except UENotRunningError as e:
            result = {"connected": False, "error": str(e)}
        except UEConnectionError as e:
            result = {"connected": False, "error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def disconnect_editor() -> list[TextContent]:
        """Close the UE command channel and discovery sockets held by this MCP process."""
        conn = _conn()
        previous = conn.get_status()
        conn.disconnect()
        result = {
            "disconnected": True,
            "previous": previous,
            "current": conn.get_status(),
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def connection_status() -> list[TextContent]:
        """Return the current connection status to Unreal Editor."""
        conn = _conn()
        result = conn.get_status()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

import json
from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError, UEConnectionError


def register(server, connection=None):
    """Register connection-related MCP tools on the given server instance."""

    def _conn():
        return connection if connection is not None else get_connection()

    @server.tool()
    async def discover_editors() -> list[TextContent]:
        """Discover running Unreal Editor instances on the local network."""
        conn = _conn()
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
            result = {
                "connected": True,
                "node_id": conn.get_connected_node_id(),
            }
        except UENotRunningError as e:
            result = {"connected": False, "error": str(e)}
        except UEConnectionError as e:
            result = {"connected": False, "error": str(e)}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    @server.tool()
    async def connection_status() -> list[TextContent]:
        """Return the current connection status to Unreal Editor."""
        conn = _conn()
        if conn.is_connected():
            result = {"connected": True, "node_id": conn.get_connected_node_id()}
        else:
            result = {"connected": False, "node_id": None}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

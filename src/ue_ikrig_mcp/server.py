"""MCP entry point for ue-ikrig server."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("ue-ikrig")

# Import and register tools from each module
from .tools import connection, ik_rig, retargeter, fine_tuning, batch


def register_all_tools():
    connection.register(server)
    ik_rig.register(server)
    retargeter.register(server)
    fine_tuning.register(server)
    batch.register(server)


def main():
    register_all_tools()
    server.run()


if __name__ == "__main__":
    main()

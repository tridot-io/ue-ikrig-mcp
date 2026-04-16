"""MCP entry point for ue-ikrig server."""

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server

server = Server("ue-ikrig")

# Import and register tools from each module
from .tools import connection, ik_rig, retargeter, fine_tuning, batch


def register_all_tools():
    connection.register(server)
    ik_rig.register(server)
    retargeter.register(server)
    fine_tuning.register(server)
    batch.register(server)


async def main_async():
    register_all_tools()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

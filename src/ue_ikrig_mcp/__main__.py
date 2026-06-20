"""Module entrypoint so `pythonw.exe -m ue_ikrig_mcp` launches the MCP server.

Mirrors the console-script entry `ue_ikrig_mcp.server:main`.
"""

from .server import main

if __name__ == "__main__":
    main()

"""MCP entry point for ue-ikrig server."""

import sys

from mcp.server.fastmcp import FastMCP
from .ue_connection import register_process_cleanup, _is_wsl

server = FastMCP("ue-ikrig")

# Import and register tools from each module
from .tools import (
    connection, ik_rig, retargeter, fine_tuning, batch, capture, capture_os, capture_asset_editor,
    retargeter_advanced, ergonomics, validation, op_stack, config_io, preview,
    op_management, fk_chains, animbp_inspect,
    fbik_tuning, phat_diagnostics, bone_diagnostics,
    offset_persistence, retarget_helpers, tapython_bridge,
    fbx_batch, sequencer_export, root_motion_ops, anim_notifies,
    cr_author, batch_ops, guide, script_store, api_catalog,
)


def register_all_tools():
    connection.register(server)
    ik_rig.register(server)
    retargeter.register(server)
    fine_tuning.register(server)
    batch.register(server)
    capture.register(server)
    capture_os.register(server)
    capture_asset_editor.register(server)
    retargeter_advanced.register(server)
    ergonomics.register(server)
    validation.register(server)
    op_stack.register(server)
    config_io.register(server)
    preview.register(server)
    op_management.register(server)
    fk_chains.register(server)
    animbp_inspect.register(server)
    fbik_tuning.register(server)
    phat_diagnostics.register(server)
    bone_diagnostics.register(server)
    offset_persistence.register(server)
    retarget_helpers.register(server)
    tapython_bridge.register(server)
    fbx_batch.register(server)
    sequencer_export.register(server)
    root_motion_ops.register(server)
    anim_notifies.register(server)
    cr_author.register(server)
    batch_ops.register(server)
    guide.register(server)
    script_store.register(server)
    api_catalog.register(server)


def main():
    # The WSL->Windows subprocess bridge was removed: the server now discovers
    # Unreal directly on localhost, which only works when it runs on Windows
    # Python. Warn (don't exit) to STDERR so the operator sees it without
    # corrupting the MCP stdio channel on STDOUT.
    if _is_wsl():
        print(
            "[ue-ikrig-mcp] WARNING: running under WSL. The WSL->Windows bridge "
            "has been removed; UDP discovery of Unreal on the Windows host will "
            "not work from WSL. Launch the server via Windows Python instead, "
            "e.g. .venv-win/Scripts/pythonw.exe -m ue_ikrig_mcp",
            file=sys.stderr,
            flush=True,
        )
    register_process_cleanup()
    register_all_tools()
    server.run()


if __name__ == "__main__":
    main()

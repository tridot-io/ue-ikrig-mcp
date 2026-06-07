"""Batch operation tools for IK retargeting (3 tools)."""

import json
from typing import Optional
from mcp.types import TextContent
from ..ue_connection import get_connection, UENotRunningError, UEConnectionError
from ..ue_scripts import wrap_script, escape_string, build_asset_registry_query


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):

    @server.tool(
        name="batch_retarget",
        description=(
            "Batch retarget a set of animations using a retargeter. "
            "Duplicates and retargets animations from the source mesh to the target mesh. "
            "animation_paths is a comma-separated list of animation asset paths. "
            "Optionally specify a prefix or suffix for the output asset names."
        ),
    )
    async def batch_retarget(
        retargeter_path: str,
        source_mesh_path: str,
        target_mesh_path: str,
        animation_paths: str,
        prefix: str = "",
        suffix: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        smp = escape_string(source_mesh_path)
        tmp = escape_string(target_mesh_path)
        pfx = escape_string(prefix)
        sfx = escape_string(suffix)

        anim_list = [a.strip() for a in animation_paths.split(",") if a.strip()]
        if not anim_list:
            return _err("animation_paths must contain at least one path")

        escaped_anims = ", ".join(f'"{escape_string(a)}"' for a in anim_list)

        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            f'source_mesh = unreal.load_asset("{smp}")\n'
            "if source_mesh is None:\n"
            f'    raise ValueError("Source mesh not found: {smp}")\n'
            f'target_mesh = unreal.load_asset("{tmp}")\n'
            "if target_mesh is None:\n"
            f'    raise ValueError("Target mesh not found: {tmp}")\n'
            f"anim_paths = [{escaped_anims}]\n"
            "anims = []\n"
            "for ap in anim_paths:\n"
            "    a = unreal.load_asset(ap)\n"
            "    if a is None:\n"
            '        raise ValueError(f"Animation not found: {ap}")\n'
            "    anims.append(a)\n"
            "duplicate_info = unreal.IKRetargetBatchOperation.duplicate_and_retarget(\n"
            "    anims,\n"
            "    source_mesh,\n"
            "    target_mesh,\n"
            "    retargeter,\n"
            f'    "{pfx}",\n'
            f'    "{sfx}",\n'
            ")\n"
            "result_paths = [a.get_path_name() for a in duplicate_info] if duplicate_info else []\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "output_assets": result_paths, "count": len(result_paths)}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="execute_python",
        description=(
            "Execute arbitrary Python code in Unreal Engine. "
            "Raw escape hatch for advanced operations not covered by other tools. "
            "The code string is sent directly to UE's Python interpreter. "
            "Auto-connects to the first discovered editor when not yet connected. "
            "Rules for reliable scripts: "
            "(1) to return structured data, end the script with "
            "print('__MCP_RESULT__' + json.dumps(payload)) — it comes back in 'parsed'; "
            "(2) asset paths are object paths like /Game/Folder/Asset (no .uasset extension, "
            "no Content/ prefix); always guard unreal.load_asset() results against None; "
            "(3) prefer editor subsystems (unreal.get_editor_subsystem(...)) over deprecated "
            "EditorLevelLibrary/EditorAssetLibrary calls; "
            "(4) scripts run synchronously on the editor game thread — never call input() or "
            "poll with sleep loops, and pass timeout_seconds for long batch operations; "
            "(5) syntax is checked locally before sending, and failures include actionable "
            "'hints'. Call ue_python_guide for the full scripting guide."
        ),
    )
    async def execute_python(
        code: str,
        mode: str = "ExecuteFile",
        timeout_seconds: Optional[float] = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        # Auto-connect so a fresh session's first execute_python call works
        # without an explicit connect_to_editor round-trip.
        if (
            hasattr(conn, "is_connected")
            and hasattr(conn, "connect")
            and not conn.is_connected()
        ):
            try:
                conn.connect()
            except (UENotRunningError, UEConnectionError) as e:
                return _err(
                    f"Auto-connect to Unreal Editor failed: {e} "
                    "Run preflight_discovery to diagnose the transport."
                )

        try:
            result = conn.execute(code, mode=mode, timeout=timeout_seconds)
        except UEConnectionError as e:
            return _err(str(e))
        return _ok(result if result is not None else {"success": True})

    @server.tool(
        name="list_skeletal_meshes",
        description=(
            "List all SkeletalMesh assets in the project, optionally filtered by path prefix. "
            "Returns asset paths and names."
        ),
    )
    async def list_skeletal_meshes(path_filter: str = "") -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        script = build_asset_registry_query("/Script/Engine.SkeletalMesh", path_filter)
        result = conn.execute(script)
        return _ok(result)

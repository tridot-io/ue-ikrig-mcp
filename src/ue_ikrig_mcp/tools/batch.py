"""Batch operation tools for IK retargeting (3 tools)."""

import json
from typing import Optional
from mcp.types import TextContent
from ..ue_connection import (
    get_connection,
    UENotRunningError,
    UEConnectionError,
    normalize_execution_mode,
    _invalid_execution_mode_result,
    _script_syntax_preflight,
)
from ..ue_scripts import wrap_script, escape_string, build_asset_registry_query
from ..script_exec import (
    add_line_offset_hint,
    ensure_connected,
    prepare_user_code,
    shape_result,
)


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
            "Raw escape hatch when no dedicated tool fits (prefer dedicated tools and "
            "batch_retargeter_ops first; save recurring scripts with save_script and replay "
            "them with run_script). Auto-connects when not yet connected. "
            "In ExecuteFile mode these helpers are pre-defined for you — do NOT rewrite them: "
            "load(path) (guarded unreal.load_asset), mcp_result(payload) (prints the "
            "__MCP_RESULT__ sentinel, unreal types coerce via str), subsys(cls), "
            "asset_registry(), plus json/unreal already imported. End scripts with "
            "mcp_result(...) to return structured data in 'parsed'. "
            "Asset paths are object paths like /Game/Folder/Asset (no .uasset, no Content/). "
            "Scripts run synchronously on the editor game thread — never input()/sleep-poll; "
            "pass timeout_seconds for long operations. Syntax is checked locally and failures "
            "include 'hints'. When parsed data is returned, the raw output echo is omitted "
            "(compact=False to keep it; max_output_chars bounds it, 0 = unlimited). "
            "Call ue_python_guide for the full scripting guide."
        ),
    )
    async def execute_python(
        code: str,
        mode: str = "ExecuteFile",
        timeout_seconds: Optional[float] = None,
        inject_helpers: bool = True,
        compact: bool = True,
        max_output_chars: int = 8000,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        try:
            mode = normalize_execution_mode(mode)
        except ValueError as e:
            return _ok(_invalid_execution_mode_result(mode, e))

        # Preflight the user code alone so SyntaxError line numbers are not
        # shifted by the injected prelude.
        preflight_failure = _script_syntax_preflight(code, mode)
        if preflight_failure is not None:
            return _ok(preflight_failure)

        # Auto-connect so a fresh session's first execute_python call works
        # without an explicit connect_to_editor round-trip.
        connect_error = ensure_connected(conn)
        if connect_error is not None:
            return _err(connect_error)

        try:
            result = conn.execute(
                prepare_user_code(code, mode, inject_helpers),
                mode=mode,
                timeout=timeout_seconds,
            )
        except UEConnectionError as e:
            return _err(str(e))
        if result is None:
            return _ok({"success": True})
        if inject_helpers and mode == "ExecuteFile":
            add_line_offset_hint(result)
        return _ok(shape_result(result, max_output_chars=max_output_chars, compact=compact))

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

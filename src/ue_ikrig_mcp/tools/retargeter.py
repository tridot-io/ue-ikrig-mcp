"""IK Retargeter management tools (7 tools)."""

import json
from mcp.types import TextContent
from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import (
    wrap_script,
    escape_string,
    build_create_retargeter,
    build_save_asset,
    safe_execute,
)


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):

    @server.tool(
        name="create_retargeter",
        description="Create a new IKRetargeter asset at the given package path.",
    )
    async def create_retargeter(
        package_path: str,
        asset_name: str,
        source_ik_rig_path: str = "",
        target_ik_rig_path: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        script = build_create_retargeter(package_path, asset_name)
        # safe_execute returns the payload on success, or {"error": True, ...} on
        # editor-side / transport failure — so this guard now actually fires (a raw
        # transport dict carries failures under .parsed, not a top-level "error").
        result = safe_execute(conn, script)
        if isinstance(result, dict) and result.get("error"):
            return _ok(result)

        # Set source and/or target rigs if provided
        pp = escape_string(package_path)
        an = escape_string(asset_name)
        asset_full_path = f"{package_path}/{asset_name}"

        if source_ik_rig_path or target_ik_rig_path:
            afp = escape_string(asset_full_path)
            lines = [
                "import unreal\n",
                f'retargeter = unreal.load_asset("{afp}")\n',
                "if retargeter is None:\n",
                f'    raise ValueError("Retargeter not found: {afp}")\n',
                "controller = unreal.IKRetargeterController.get_controller(retargeter)\n",
            ]
            if source_ik_rig_path:
                srp = escape_string(source_ik_rig_path)
                lines += [
                    f'source_rig = unreal.load_asset("{srp}")\n',
                    "if source_rig is None:\n",
                    f'    raise ValueError("Source IKRig not found: {srp}")\n',
                    "controller.set_ik_rig(unreal.RetargetSourceOrTarget.SOURCE, source_rig)\n",
                ]
            if target_ik_rig_path:
                trp = escape_string(target_ik_rig_path)
                lines += [
                    f'target_rig = unreal.load_asset("{trp}")\n',
                    "if target_rig is None:\n",
                    f'    raise ValueError("Target IKRig not found: {trp}")\n',
                    "controller.set_ik_rig(unreal.RetargetSourceOrTarget.TARGET, target_rig)\n",
                ]
            lines.append('print("__MCP_RESULT__" + json.dumps({"success": True, "path": retargeter.get_path_name()}))')
            set_rigs_script = wrap_script("".join(lines))
            result2 = safe_execute(conn, set_rigs_script)
            return _ok(result2)

        return _ok(result)

    @server.tool(
        name="inspect_retargeter",
        description="Inspect a retargeter asset: returns source/target rigs and chain mappings.",
    )
    async def inspect_retargeter(asset_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(asset_path)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{p}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {p}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "source_rig = controller.get_ik_rig(unreal.RetargetSourceOrTarget.SOURCE)\n"
            "target_rig = controller.get_ik_rig(unreal.RetargetSourceOrTarget.TARGET)\n"
            "mappings = []\n"
            "for m in controller.get_chain_mappings():\n"
            "    mappings.append({\n"
            '        "source_chain": str(m.source_chain),\n'
            '        "target_chain": str(m.target_chain)\n'
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"source_rig": source_rig.get_path_name() if source_rig else None,'
            '"target_rig": target_rig.get_path_name() if target_rig else None,'
            '"chain_mappings": mappings'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_retargeter_rigs",
        description=(
            "Set the source or target IKRig on a retargeter. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def set_retargeter_rigs(
        retargeter_path: str,
        source_or_target: str,
        ik_rig_path: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if source_or_target not in ("Source", "Target"):
            return _err("source_or_target must be 'Source' or 'Target'")

        rtp = escape_string(retargeter_path)
        irp = escape_string(ik_rig_path)
        enum_val = f"unreal.RetargetSourceOrTarget.{source_or_target.upper()}"
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            f'ik_rig = unreal.load_asset("{irp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {rtp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {irp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            f"controller.set_ik_rig({enum_val}, ik_rig)\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "side": "' + source_or_target + '", "ik_rig": ik_rig.get_path_name()}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="auto_map_chains",
        description="Auto-map chains on a retargeter using Fuzzy or Exact matching.",
    )
    async def auto_map_chains(
        retargeter_path: str,
        map_type: str = "Fuzzy",
        force_remap: bool = True,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        map_enum = f"unreal.AutoMapChainType.{map_type.upper()}"
        force = "True" if force_remap else "False"
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            f"controller.auto_map_chains({map_enum}, {force})\n"
            "mappings = []\n"
            "for m in controller.get_chain_mappings():\n"
            "    mappings.append({\n"
            '        "source_chain": str(m.source_chain),\n'
            '        "target_chain": str(m.target_chain)\n'
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "mappings": mappings}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_chain_mapping",
        description="Set a specific chain mapping on a retargeter (source chain → target chain).",
    )
    async def set_chain_mapping(
        retargeter_path: str,
        source_chain: str,
        target_chain: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        sc = escape_string(source_chain)
        tc = escape_string(target_chain)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            f'controller.set_chain_mapping("{sc}", "{tc}")\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "source_chain": "' + sc + '", "target_chain": "' + tc + '"}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_chain_mappings",
        description="Get all chain mappings on a retargeter.",
    )
    async def get_chain_mappings(retargeter_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "mappings = []\n"
            "for m in controller.get_chain_mappings():\n"
            "    mappings.append({\n"
            '        "source_chain": str(m.source_chain),\n'
            '        "target_chain": str(m.target_chain)\n'
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps(mappings))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="auto_align_all_bones",
        description="Auto-align all bones for the source or target skeleton in a retargeter.",
    )
    async def auto_align_all_bones(
        retargeter_path: str,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if source_or_target not in ("Source", "Target"):
            return _err("source_or_target must be 'Source' or 'Target'")

        rtp = escape_string(retargeter_path)
        enum_val = f"unreal.RetargetSourceOrTarget.{source_or_target.upper()}"
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("Retargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            f"controller.auto_align_all_bones({enum_val})\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "side": "' + source_or_target + '"}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

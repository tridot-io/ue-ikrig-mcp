"""IK Rig management tools (10 tools)."""

import json
from mcp.types import TextContent
from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import (
    wrap_script,
    escape_string,
    build_load_asset,
    build_get_ik_rig_controller,
    build_save_asset,
    build_create_ik_rig,
    build_asset_registry_query,
)


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):

    @server.tool(
        name="create_ik_rig",
        description="Create a new IKRigDefinition asset at the given package path.",
    )
    async def create_ik_rig(
        package_path: str,
        asset_name: str,
        skeletal_mesh_path: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        script = build_create_ik_rig(package_path, asset_name)

        if skeletal_mesh_path:
            pp = escape_string(package_path)
            an = escape_string(asset_name)
            smp = escape_string(skeletal_mesh_path)
            set_mesh = wrap_script(
                "import unreal\n"
                f'ik_rig = unreal.load_asset("{pp}/{an}")\n'
                f'skm = unreal.load_asset("{smp}")\n'
                "if ik_rig is None:\n"
                f'    raise ValueError("IKRig not found: {pp}/{an}")\n'
                "if skm is None:\n"
                f'    raise ValueError("SkeletalMesh not found: {smp}")\n'
                "controller = unreal.IKRigController.get_controller(ik_rig)\n"
                "controller.set_skeletal_mesh(skm)\n"
                'print("__MCP_RESULT__" + json.dumps({"set_mesh": True}))'
            )
            result = conn.execute(script)
            if isinstance(result, dict) and result.get("error"):
                return _ok(result)
            result2 = conn.execute(set_mesh)
            return _ok(result2)

        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="inspect_ik_rig",
        description="Inspect an IKRig asset: returns mesh, root bone, retarget chains, solvers, and goals.",
    )
    async def inspect_ik_rig(asset_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(asset_path)
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{p}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig asset not found: {p}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            "skm = controller.get_skeletal_mesh()\n"
            "mesh_path = skm.get_path_name() if skm else None\n"
            "root_bone = controller.get_retarget_root()\n"
            "chains = []\n"
            "def _bone_ref_name(br):\n"
            "    return str(br.get_editor_property('bone_name')) if br is not None else ''\n"
            "for chain in controller.get_retarget_chains():\n"
            "    chains.append({\n"
            '        "chain_name": str(chain.chain_name),\n'
            "        \"start_bone\": _bone_ref_name(chain.get_editor_property('start_bone')),\n"
            "        \"end_bone\": _bone_ref_name(chain.get_editor_property('end_bone')),\n"
            '        "goal": str(chain.ik_goal_name) if chain.ik_goal_name else ""\n'
            "    })\n"
            "solvers = []\n"
            "for i in range(controller.get_num_solvers()):\n"
            "    s = controller.get_solver_controller(i)\n"
            '    solvers.append({"index": i, "type": s.get_class().get_name(), "enabled": controller.get_solver_enabled(i)})\n'
            "goals = []\n"
            "for g in controller.get_all_goals():\n"
            '    goals.append({"goal_name": str(g.goal_name), "bone_name": str(g.bone_name)})\n'
            'print("__MCP_RESULT__" + json.dumps({"mesh": mesh_path, "root_bone": str(root_bone) if root_bone else None, "chains": chains, "solvers": solvers, "goals": goals}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="set_ik_rig_mesh",
        description="Set the skeletal mesh on an IKRig asset.",
    )
    async def set_ik_rig_mesh(rig_path: str, skeletal_mesh_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        smp = escape_string(skeletal_mesh_path)
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{rp}")\n'
            f'skm = unreal.load_asset("{smp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "if skm is None:\n"
            f'    raise ValueError("SkeletalMesh not found: {smp}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            "controller.set_skeletal_mesh(skm)\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "rig": ik_rig.get_path_name(), "mesh": skm.get_path_name()}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="set_retarget_root",
        description="Set the retarget root bone on an IKRig asset.",
    )
    async def set_retarget_root(rig_path: str, bone_name: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        bn = escape_string(bone_name)
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{rp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            f'controller.set_retarget_root("{bn}")\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "root_bone": "' + bn + '"}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="add_retarget_chain",
        description="Add a retarget chain to an IKRig asset.",
    )
    async def add_retarget_chain(
        rig_path: str,
        chain_name: str,
        start_bone: str,
        end_bone: str,
        goal_name: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        cn = escape_string(chain_name)
        sb = escape_string(start_bone)
        eb = escape_string(end_bone)
        gn = escape_string(goal_name)
        goal_arg = f'"{gn}"' if goal_name else '""'
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{rp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            f'result = controller.add_retarget_chain("{cn}", "{sb}", "{eb}", {goal_arg})\n'
            'print("__MCP_RESULT__" + json.dumps({"success": bool(result), "chain_name": "' + cn + '"}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="remove_retarget_chain",
        description="Remove a retarget chain from an IKRig asset.",
    )
    async def remove_retarget_chain(rig_path: str, chain_name: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        cn = escape_string(chain_name)
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{rp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            f'result = controller.remove_retarget_chain("{cn}")\n'
            'print("__MCP_RESULT__" + json.dumps({"success": bool(result), "chain_name": "' + cn + '"}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="get_retarget_chains",
        description="Get all retarget chains defined on an IKRig asset.",
    )
    async def get_retarget_chains(rig_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        script = wrap_script(
            "import unreal\n"
            f'ik_rig = unreal.load_asset("{rp}")\n'
            "if ik_rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "controller = unreal.IKRigController.get_controller(ik_rig)\n"
            "chains = []\n"
            "def _bone_ref_name(br):\n"
            "    return str(br.get_editor_property('bone_name')) if br is not None else ''\n"
            "for chain in controller.get_retarget_chains():\n"
            "    chains.append({\n"
            '        "chain_name": str(chain.chain_name),\n'
            "        \"start_bone\": _bone_ref_name(chain.get_editor_property('start_bone')),\n"
            "        \"end_bone\": _bone_ref_name(chain.get_editor_property('end_bone')),\n"
            '        "goal": str(chain.ik_goal_name) if chain.ik_goal_name else ""\n'
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps(chains))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="list_bones",
        description="List the bone hierarchy for a given skeletal mesh.",
    )
    async def list_bones(skeletal_mesh_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        smp = escape_string(skeletal_mesh_path)
        script = wrap_script(
            "import unreal\n"
            f'skm = unreal.load_asset("{smp}")\n'
            "if skm is None:\n"
            f'    raise ValueError("SkeletalMesh not found: {smp}")\n'
            "ref_pose = skm.skeleton.get_reference_pose()\n"
            "bone_names = [str(b) for b in ref_pose.get_bone_names()]\n"
            'print("__MCP_RESULT__" + json.dumps({"bones": bone_names, "count": len(bone_names)}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="list_ik_assets",
        description="Find IKRigDefinition and IKRetargeter assets in the project.",
    )
    async def list_ik_assets(path_filter: str = "") -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        # Single editor script queries both IKRig asset types (UE 5.5+ API: class_paths + TopLevelAssetPath).
        pf = escape_string(path_filter)
        combined_script = wrap_script(
            "import unreal\n"
            "ar = unreal.AssetRegistryHelpers.get_asset_registry()\n"
            "results = {}\n"
            "for class_path in ['/Script/IKRig.IKRigDefinition', '/Script/IKRig.IKRetargeter']:\n"
            "    module, klass = class_path.rsplit('.', 1)\n"
            "    ar_filter = unreal.ARFilter()\n"
            "    ar_filter.class_paths = [unreal.TopLevelAssetPath(module, klass)]\n"
            "    ar_filter.recursive_paths = True\n"
            + (f'    ar_filter.package_paths = ["{pf}"]\n' if path_filter else "")
            + "    assets = ar.get_assets(ar_filter)\n"
            '    results[klass] = [{"path": str(a.package_name), "name": str(a.asset_name)} for a in assets]\n'
            'print("__MCP_RESULT__" + json.dumps(results))'
        )
        result = conn.execute(combined_script)
        return _ok(result)

    @server.tool(
        name="save_asset",
        description="Save an asset to disk.",
    )
    async def save_asset(asset_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        script = build_save_asset(asset_path)
        result = conn.execute(script)
        return _ok(result)

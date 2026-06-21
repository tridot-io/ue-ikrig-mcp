"""Fine-tuning tools for interactive IK retarget adjustment (10 tools)."""

import json
from mcp.types import TextContent
from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, build_get_retargeter_controller, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):

    @server.tool(
        name="get_bone_rotation_offset",
        description=(
            "Get the rotation offset for a bone in the retarget pose. "
            "Returns the quaternion {x,y,z,w} and euler {pitch,yaw,roll}. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def get_bone_rotation_offset(
        retargeter_path: str,
        bone_name: str,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        bn = escape_string(bone_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f'q = controller.get_rotation_offset_for_retarget_pose_bone("{bn}", sot)\n'
            "rot = q.rotator()\n"
            'print("__MCP_RESULT__" + json.dumps({"rotation": {"x": q.x, "y": q.y, "z": q.z, "w": q.w}, "euler": {"pitch": rot.pitch, "yaw": rot.yaw, "roll": rot.roll}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_bone_rotation_offset",
        description=(
            "Set the rotation offset for a bone in the retarget pose using a quaternion. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def set_bone_rotation_offset(
        retargeter_path: str,
        bone_name: str,
        x: float,
        y: float,
        z: float,
        w: float,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        bn = escape_string(bone_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f"new_quat = unreal.Quat({x}, {y}, {z}, {w})\n"
            f'controller.set_rotation_offset_for_retarget_pose_bone("{bn}", new_quat, sot)\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "bone": "' + bn + '", "rotation": {"x": ' + str(x) + ', "y": ' + str(y) + ', "z": ' + str(z) + ', "w": ' + str(w) + '}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="adjust_bone_rotation",
        description=(
            "Adjust a bone's rotation offset by adding euler angle deltas (pitch/yaw/roll in degrees). "
            "Reads the current quaternion offset, converts to euler, adds deltas, converts back, and writes. "
            "This is the primary tool for conversational IK retarget fine-tuning. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def adjust_bone_rotation(
        retargeter_path: str,
        bone_name: str,
        delta_pitch: float = 0,
        delta_yaw: float = 0,
        delta_roll: float = 0,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        bn = escape_string(bone_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f'current_quat = controller.get_rotation_offset_for_retarget_pose_bone("{bn}", sot)\n'
            "current_rot = current_quat.rotator()\n"
            f"new_rot = unreal.Rotator(current_rot.pitch + {delta_pitch}, current_rot.yaw + {delta_yaw}, current_rot.roll + {delta_roll})\n"
            "new_quat = new_rot.quaternion()\n"
            f'controller.set_rotation_offset_for_retarget_pose_bone("{bn}", new_quat, sot)\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "bone": "' + bn + '", '
            '"new_euler": {"pitch": new_rot.pitch, "yaw": new_rot.yaw, "roll": new_rot.roll}, '
            '"new_rotation": {"x": new_quat.x, "y": new_quat.y, "z": new_quat.z, "w": new_quat.w}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_root_offset",
        description=(
            "Set the root bone translation offset in the retarget pose. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def set_root_offset(
        retargeter_path: str,
        x: float,
        y: float,
        z: float,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f"offset = unreal.Vector({x}, {y}, {z})\n"
            "controller.set_root_offset_in_retarget_pose(offset, sot)\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True, "offset": {"x": ' + str(x) + ', "y": ' + str(y) + ', "z": ' + str(z) + '}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_chain_settings",
        description="Get the FK/IK retarget settings for a target chain on a retargeter.",
    )
    async def get_chain_settings(
        retargeter_path: str,
        target_chain_name: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        cn = escape_string(target_chain_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'settings = controller.get_retarget_chain_settings("{cn}")\n'
            "if settings is None:\n"
            f'    raise ValueError("Chain not found: {cn}")\n'
            "fk = settings.fk\n"
            "ik = settings.ik\n"
            'print("__MCP_RESULT__" + json.dumps({"chain": "' + cn + '", '
            '"fk": {"enable_fk": fk.enable_fk, "rotation_alpha": fk.rotation_alpha, "translation_alpha": fk.translation_alpha}, '
            '"ik": {"enable_ik": ik.enable_ik, "blend_to_source": ik.blend_to_source}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_chain_settings",
        description=(
            "Set FK/IK retarget settings for a target chain on a retargeter. "
            "Only provided (non-None) values are modified."
        ),
    )
    async def set_chain_settings(
        retargeter_path: str,
        target_chain_name: str,
        enable_fk: bool = None,
        rotation_alpha: float = None,
        translation_alpha: float = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        cn = escape_string(target_chain_name)

        modifications = []
        if enable_fk is not None:
            modifications.append(f"settings.fk.enable_fk = {'True' if enable_fk else 'False'}\n")
        if rotation_alpha is not None:
            modifications.append(f"settings.fk.rotation_alpha = {rotation_alpha}\n")
        if translation_alpha is not None:
            modifications.append(f"settings.fk.translation_alpha = {translation_alpha}\n")

        mod_block = "".join(modifications) if modifications else "pass\n"

        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'settings = controller.get_retarget_chain_settings("{cn}")\n'
            "if settings is None:\n"
            f'    raise ValueError("Chain not found: {cn}")\n'
            + mod_block
            + f'controller.set_retarget_chain_settings("{cn}", settings)\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "chain": "' + cn + '"}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_global_settings",
        description="Get the global retarget settings for a retargeter (scale, etc.).",
    )
    async def get_global_settings(retargeter_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            "settings = controller.get_global_settings()\n"
            'print("__MCP_RESULT__" + json.dumps({"scale_horizontal": settings.scale_horizontal, "scale_vertical": settings.scale_vertical}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_global_settings",
        description=(
            "Set global retarget settings on a retargeter. "
            "Only provided (non-None) values are modified."
        ),
    )
    async def set_global_settings(
        retargeter_path: str,
        scale_horizontal: float = None,
        scale_vertical: float = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)

        modifications = []
        if scale_horizontal is not None:
            modifications.append(f"settings.scale_horizontal = {scale_horizontal}\n")
        if scale_vertical is not None:
            modifications.append(f"settings.scale_vertical = {scale_vertical}\n")

        mod_block = "".join(modifications) if modifications else "pass\n"

        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            "settings = controller.get_global_settings()\n"
            + mod_block
            + "controller.set_global_settings(settings)\n"
            'print("__MCP_RESULT__" + json.dumps({"success": True}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="create_retarget_pose",
        description=(
            "Create a new named retarget pose on a retargeter. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def create_retarget_pose(
        retargeter_path: str,
        pose_name: str,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        pn = escape_string(pose_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f'controller.create_retarget_pose("{pn}", sot)\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "pose_name": "' + pn + '", "side": "' + source_or_target + '"}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_current_pose",
        description=(
            "Set the active retarget pose on a retargeter by name. "
            "source_or_target must be 'Source' or 'Target'."
        ),
    )
    async def set_current_pose(
        retargeter_path: str,
        pose_name: str,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        pn = escape_string(pose_name)
        script = wrap_script(
            "import unreal\n"
            f'retargeter = unreal.load_asset("{rtp}")\n'
            "if retargeter is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
            "if controller is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            f'sot = unreal.RetargetSourceOrTarget.SOURCE if "{source_or_target}" == "Source" else unreal.RetargetSourceOrTarget.TARGET\n'
            f'controller.set_current_retarget_pose("{pn}", sot)\n'
            'print("__MCP_RESULT__" + json.dumps({"success": True, "pose_name": "' + pn + '", "side": "' + source_or_target + '"}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="auto_align_mapped_bones",
        description=(
            "Crash-safe auto-align that only touches bones in currently-mapped retarget chains. "
            "UE 5.6's auto_align_all_bones asserts and crashes the editor if any chain on the "
            "iterated side lacks a source mapping (e.g. MetaHuman twist / metacarpal chains on a "
            "Mixamo-sourced retargeter). This tool walks the mapped chains, collects every bone "
            "between each chain's start_bone and end_bone, and calls auto_align_bones on just "
            "that curated list. Returns per-bone before/after euler offsets in degrees. "
            "method: 'ChainToChain' | 'LocalRotationAxes' | 'GlobalRotationAxes' | 'MeshToMesh'. "
            "source_or_target: 'Source' | 'Target' (default 'Target')."
        ),
    )
    async def auto_align_mapped_bones(
        retargeter_path: str,
        method: str = "ChainToChain",
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        _method_map = {
            "chaintochain": "CHAIN_TO_CHAIN",
            "chain_to_chain": "CHAIN_TO_CHAIN",
            "localrotationaxes": "LOCAL_ROTATION_AXES",
            "local_rotation_axes": "LOCAL_ROTATION_AXES",
            "globalrotationaxes": "GLOBAL_ROTATION_AXES",
            "global_rotation_axes": "GLOBAL_ROTATION_AXES",
            "meshtomesh": "MESH_TO_MESH",
            "mesh_to_mesh": "MESH_TO_MESH",
        }
        method_key = _method_map.get(method.lower().replace(" ", ""))
        if method_key is None:
            return _err(f"Unknown method: {method!r}. Use ChainToChain, LocalRotationAxes, GlobalRotationAxes, or MeshToMesh.")

        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"

        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"sot = unreal.RetargetSourceOrTarget.{side}\n"
            "side_rig = ctrl.get_ik_rig(sot)\n"
            "if side_rig is None:\n"
            f'    raise ValueError("{side} IKRig not assigned on retargeter")\n'
            "side_ctrl = unreal.IKRigController.get_controller(side_rig)\n"
            "side_mesh = side_ctrl.get_skeletal_mesh()\n"
            "side_skel = side_mesh.skeleton if side_mesh else None\n"
            "if side_skel is None:\n"
            '    raise ValueError("Skeleton not available for selected side")\n'
            "# Figure out which target chains are mapped to a source chain\n"
            "chains = side_ctrl.get_retarget_chains()\n"
            "mapped_target_chains = []\n"
            "for ch in chains:\n"
            "    t_name = str(ch.chain_name)\n"
            "    if sot == unreal.RetargetSourceOrTarget.TARGET:\n"
            "        src_chain = str(ctrl.get_source_chain(t_name))\n"
            "        if src_chain and src_chain != 'None':\n"
            "            mapped_target_chains.append(ch)\n"
            "    else:\n"
            "        # For source side: include every chain; there's no per-source unmapped concept\n"
            "        mapped_target_chains.append(ch)\n"
            "# Enumerate bones between start_bone and end_bone (walk parents)\n"
            "def _bn(br):\n"
            "    return str(br.get_editor_property('bone_name')) if br is not None else ''\n"
            "def _collect(ch_obj):\n"
            "    sb = _bn(ch_obj.get_editor_property('start_bone'))\n"
            "    eb = _bn(ch_obj.get_editor_property('end_bone'))\n"
            "    if not sb:\n"
            "        return []\n"
            "    if sb == eb:\n"
            "        return [sb]\n"
            "    out, cursor = [], eb\n"
            "    for _ in range(512):\n"
            "        out.append(cursor)\n"
            "        if cursor == sb:\n"
            "            break\n"
            "        ref = side_skel.get_reference_skeleton() if hasattr(side_skel, 'get_reference_skeleton') else None\n"
            "        if ref is not None:\n"
            "            parent_idx = ref.get_parent_index(ref.get_bone_index(cursor))\n"
            "            if parent_idx == -1:\n"
            "                break\n"
            "            cursor = str(ref.get_bone_name(parent_idx))\n"
            "        else:\n"
            "            # Fallback: walk by find/get on USkeleton directly\n"
            "            idx = side_skel.find_bone_index(cursor) if hasattr(side_skel, 'find_bone_index') else -1\n"
            "            if idx == -1:\n"
            "                break\n"
            "            parent_idx = side_skel.get_parent_index(idx) if hasattr(side_skel, 'get_parent_index') else -1\n"
            "            if parent_idx == -1:\n"
            "                break\n"
            "            cursor = str(side_skel.get_bone_name(parent_idx))\n"
            "    out.reverse()\n"
            "    return out\n"
            "bones_set = set()\n"
            "for ch in mapped_target_chains:\n"
            "    for b in _collect(ch):\n"
            "        if b:\n"
            "            bones_set.add(b)\n"
            "bones = sorted(bones_set)\n"
            "def _to_deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "before = {b: _to_deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, sot)) for b in bones}\n"
            f"ctrl.auto_align_bones(bones, unreal.RetargetAutoAlignMethod.{method_key}, sot)\n"
            "after = {b: _to_deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, sot)) for b in bones}\n"
            "changed = [b for b in bones if before[b] != after[b]]\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'side': '" + side + "',\n"
            f"    'method': '{method_key}',\n"
            "    'mapped_chain_count': len(mapped_target_chains),\n"
            "    'bone_count': len(bones),\n"
            "    'changed_count': len(changed),\n"
            "    'before': before,\n"
            "    'after': after,\n"
            "}))"
        )
        result = safe_execute(conn, script)
        return _ok(result)

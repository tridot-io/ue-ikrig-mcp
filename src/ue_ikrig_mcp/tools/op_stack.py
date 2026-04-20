"""Op-stack operations for IK Retargeters.

UE 5.6's IK Retargeter is built on an op stack (IKRetargetOpBase subclasses
chained together: Pelvis Motion -> FK Chains -> IK Goals -> Run IK Rig ->
Root Motion -> Remap Curves -> Scale Source by default). Mutating this stack
is essential for real retargeter authoring — adding Speed Planting for foot
plant behavior, Stride Warping for locomotion scaling, or reconfiguring
Pelvis Motion translation alpha for proportional retargeting.

Tools here provide typed wrappers over the IKRetargeterController op-stack
API so callers don't have to walk ChainsToRetarget arrays or call
get_settings / mutate / set_settings by hand.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):
    @server.tool(
        name="add_speed_planting_op",
        description=(
            "Add a Speed Planting op to a retargeter's op stack. Speed "
            "Planting detects low-velocity frames in the source animation "
            "(foot plant moments) and pins target foot IK goals to their "
            "last position, eliminating the 'feet sliding on slow frames' "
            "artifact that Mixamo->MH retargets often show. "
            "target_chains defaults to ['LeftLeg','RightLeg']. "
            "speed_threshold is the cm/s below which planting engages "
            "(default 15). stiffness / critical_damping tune the unplant "
            "spring. If a Speed Planting op already exists, the existing "
            "settings are updated instead of adding a second one."
        ),
    )
    async def add_speed_planting_op(
        retargeter_path: str,
        target_chains: list = None,
        speed_threshold: float = 15.0,
        stiffness: float = 250.0,
        critical_damping: float = 1.0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        chains = target_chains if target_chains else ["LeftLeg", "RightLeg"]
        chains_json = json.dumps([str(c) for c in chains])
        rtp = escape_string(retargeter_path)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"_chains = {chains_json}\n"
            # Look for an existing SpeedPlanting op
            "existing_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetSpeedPlantingController':\n"
            "        existing_idx = i\n"
            "        break\n"
            "if existing_idx < 0:\n"
            "    # Add new op using add_retarget_op(op_class)\n"
            "    added_idx = ctrl.add_retarget_op(unreal.IKRetargetSpeedPlantingOp)\n"
            "else:\n"
            "    added_idx = existing_idx\n"
            "# Configure settings\n"
            "oc = ctrl.get_op_controller(added_idx)\n"
            "settings = oc.get_settings()\n"
            # ChainsToSpeedPlant is an array of FName
            "name_arr = [unreal.Name(c) for c in _chains]\n"
            "settings.set_editor_property('chains_to_speed_plant', name_arr)\n"
            f"settings.set_editor_property('speed_threshold', {float(speed_threshold)})\n"
            f"settings.set_editor_property('stiffness', {float(stiffness)})\n"
            f"settings.set_editor_property('critical_damping', {float(critical_damping)})\n"
            "oc.set_settings(settings)\n"
            "# Verify\n"
            "verify = oc.get_settings().export_text()\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'op_index': added_idx,\n"
            "    'was_existing': existing_idx >= 0,\n"
            "    'applied_settings': verify,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="add_stride_warping_op",
        description=(
            "Add a Stride Warping op to a retargeter's op stack. Stride "
            "Warping scales the forward translation of locomotion without "
            "distorting the FK/IK pose (keeps feet planted at scaled-down "
            "positions). Useful when retarget source and target have "
            "different stride lengths due to height mismatch. If an op of "
            "this type already exists, its settings are updated in place. "
            "direction_source: 'Goals' (default) or 'Chain'. forward_axis: "
            "'X' / 'Y' (default) / 'Z'."
        ),
    )
    async def add_stride_warping_op(
        retargeter_path: str,
        direction_source: str = "Goals",
        forward_axis: str = "Y",
        warp_forwards: float = 1.0,
        sideways_offset: float = 0.0,
        warp_splay: float = 1.0,
        direction_chain: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if direction_source not in ("Goals", "Chain"):
            return _err("direction_source must be 'Goals' or 'Chain'")
        if forward_axis not in ("X", "Y", "Z"):
            return _err("forward_axis must be 'X', 'Y', or 'Z'")

        rtp = escape_string(retargeter_path)
        dc = escape_string(direction_chain)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "existing_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetStrideWarpingController':\n"
            "        existing_idx = i\n"
            "        break\n"
            "if existing_idx < 0:\n"
            "    added_idx = ctrl.add_retarget_op(unreal.IKRetargetStrideWarpingOp)\n"
            "else:\n"
            "    added_idx = existing_idx\n"
            "oc = ctrl.get_op_controller(added_idx)\n"
            "settings = oc.get_settings()\n"
            # direction_source -> Enum(Goals=0, Chain=1)  — serialized via Python enum values
            f'settings.set_editor_property("direction_source", 0 if "{direction_source}" == "Goals" else 1)\n'
            # forward_axis -> Enum X=0, Y=1, Z=2\n
            f'settings.set_editor_property("forward_axis", {{"X":0, "Y":1, "Z":2}}["{forward_axis}"])\n'
            f"settings.set_editor_property('warp_forwards', {float(warp_forwards)})\n"
            f"settings.set_editor_property('sideways_offset', {float(sideways_offset)})\n"
            f"settings.set_editor_property('warp_splay', {float(warp_splay)})\n"
            f'settings.set_editor_property("direction_chain", unreal.Name("{dc}"))\n'
            "oc.set_settings(settings)\n"
            "verify = oc.get_settings().export_text()\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'op_index': added_idx,\n"
            "    'was_existing': existing_idx >= 0,\n"
            "    'applied_settings': verify,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="configure_pelvis_motion",
        description=(
            "Typed setter for the Pelvis Motion op's settings. All parameters "
            "are optional — only provided fields are updated, others are "
            "preserved. If no Pelvis Motion op exists, one is added. "
            "translation_alpha/rotation_alpha: 0..1 copy-through. "
            "scale_horizontal/scale_vertical: proportional scaling of pelvis "
            "motion along respective axes (1.0 = identity). "
            "affect_ik_horizontal/affect_ik_vertical: 0..1 — whether pelvis "
            "translation drives IK goals (typical setup: 1.0 horizontal, 0.0 "
            "vertical so feet don't float during vertical pelvis motion). "
            "translation_offset/rotation_offset: 3-float static offsets "
            "(rotation in degrees)."
        ),
    )
    async def configure_pelvis_motion(
        retargeter_path: str,
        translation_alpha: float = None,
        rotation_alpha: float = None,
        scale_horizontal: float = None,
        scale_vertical: float = None,
        affect_ik_horizontal: float = None,
        affect_ik_vertical: float = None,
        blend_to_source_translation: float = None,
        translation_offset: list = None,
        rotation_offset: list = None,
        source_pelvis_bone: str = None,
        target_pelvis_bone: str = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)

        # Build a dict of fields-to-set, serialize as JSON
        updates = {}
        for k, v in (
            ("translation_alpha", translation_alpha),
            ("rotation_alpha", rotation_alpha),
            ("scale_horizontal", scale_horizontal),
            ("scale_vertical", scale_vertical),
            ("affect_ik_horizontal", affect_ik_horizontal),
            ("affect_ik_vertical", affect_ik_vertical),
            ("blend_to_source_translation", blend_to_source_translation),
        ):
            if v is not None:
                try:
                    updates[k] = float(v)
                except (TypeError, ValueError):
                    return _err(f"{k} must be numeric, got {v!r}")

        translation_offset_v = None
        if translation_offset is not None:
            if not (isinstance(translation_offset, (list, tuple)) and len(translation_offset) == 3):
                return _err("translation_offset must be [x, y, z]")
            try:
                translation_offset_v = [float(x) for x in translation_offset]
            except (TypeError, ValueError) as e:
                return _err(f"translation_offset contains non-numeric: {e}")

        rotation_offset_v = None
        if rotation_offset is not None:
            if not (isinstance(rotation_offset, (list, tuple)) and len(rotation_offset) == 3):
                return _err("rotation_offset must be [roll, pitch, yaw] in degrees")
            try:
                rotation_offset_v = [float(x) for x in rotation_offset]
            except (TypeError, ValueError) as e:
                return _err(f"rotation_offset contains non-numeric: {e}")

        payload = json.dumps({
            "scalar_updates": updates,
            "translation_offset": translation_offset_v,
            "rotation_offset": rotation_offset_v,
            "source_pelvis_bone": source_pelvis_bone,
            "target_pelvis_bone": target_pelvis_bone,
        })

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"_payload = {payload}\n"
            "pelvis_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetPelvisMotionController':\n"
            "        pelvis_idx = i\n"
            "        break\n"
            "was_existing = pelvis_idx >= 0\n"
            "if pelvis_idx < 0:\n"
            "    pelvis_idx = ctrl.add_retarget_op(unreal.IKRetargetPelvisMotionOp)\n"
            "oc = ctrl.get_op_controller(pelvis_idx)\n"
            "s = oc.get_settings()\n"
            "for k, v in _payload['scalar_updates'].items():\n"
            "    s.set_editor_property(k, v)\n"
            "if _payload['translation_offset'] is not None:\n"
            "    t = _payload['translation_offset']\n"
            "    s.set_editor_property('translation_offset', unreal.Vector(t[0], t[1], t[2]))\n"
            "if _payload['rotation_offset'] is not None:\n"
            "    r = _payload['rotation_offset']\n"
            "    s.set_editor_property('rotation_offset', unreal.Rotator(roll=r[0], pitch=r[1], yaw=r[2]))\n"
            "oc.set_settings(s)\n"
            "if _payload['source_pelvis_bone']:\n"
            "    oc.set_source_pelvis_bone(unreal.Name(_payload['source_pelvis_bone']))\n"
            "if _payload['target_pelvis_bone']:\n"
            "    oc.set_target_pelvis_bone(unreal.Name(_payload['target_pelvis_bone']))\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'op_index': pelvis_idx,\n"
            "    'was_existing': was_existing,\n"
            "    'applied_settings': oc.get_settings().export_text(),\n"
            "    'source_pelvis_bone': str(oc.get_source_pelvis_bone()),\n"
            "    'target_pelvis_bone': str(oc.get_target_pelvis_bone()),\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="remove_op",
        description=(
            "Remove an op from a retargeter's op stack. identifier can be the "
            "op's display name (e.g. 'Pelvis Motion', 'Speed Planting') or its "
            "controller/op class type name (e.g. 'IKRetargetSpeedPlantingOp', "
            "'IKRetargetPelvisMotionController'). If multiple ops match, only "
            "the first is removed."
        ),
    )
    async def remove_op(
        retargeter_path: str,
        identifier: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not identifier:
            return _err("identifier must be a non-empty op name or type name")

        rtp = escape_string(retargeter_path)
        idn = escape_string(identifier)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f'ident = "{idn}"\n'
            "found_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    type_name = type(oc).__name__\n"
            "    display = str(ctrl.get_op_name(i))\n"
            "    if ident == type_name or ident == display:\n"
            "        found_idx = i\n"
            "        break\n"
            "    # Fuzzy: match against type name without IKRetarget/Controller/Op\n"
            "    short = type_name.replace('IKRetarget', '').replace('Controller', '').replace('Op', '')\n"
            "    if ident == short:\n"
            "        found_idx = i\n"
            "        break\n"
            "if found_idx < 0:\n"
            "    raise ValueError('No op in stack matches identifier: ' + ident)\n"
            "removed_name = str(ctrl.get_op_name(found_idx))\n"
            "removed_type = type(ctrl.get_op_controller(found_idx)).__name__\n"
            "ctrl.remove_retarget_op(found_idx)\n"
            "remaining = []\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    remaining.append({'index': i, 'name': str(ctrl.get_op_name(i)), 'type': type(ctrl.get_op_controller(i)).__name__})\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'removed_index': found_idx,\n"
            "    'removed_name': removed_name,\n"
            "    'removed_type': removed_type,\n"
            "    'remaining_ops': remaining,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="move_op",
        description=(
            "Reorder an op in the retargeter's op stack. identifier matches "
            "the op by name or type (same lookup rules as remove_op). "
            "new_index is 0-based; if out of range it is clamped."
        ),
    )
    async def move_op(
        retargeter_path: str,
        identifier: str,
        new_index: int,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not identifier:
            return _err("identifier must be a non-empty op name or type name")

        rtp = escape_string(retargeter_path)
        idn = escape_string(identifier)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f'ident = "{idn}"\n'
            f"target_idx = {int(new_index)}\n"
            "found_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    type_name = type(oc).__name__\n"
            "    display = str(ctrl.get_op_name(i))\n"
            "    if ident == type_name or ident == display:\n"
            "        found_idx = i\n"
            "        break\n"
            "    short = type_name.replace('IKRetarget', '').replace('Controller', '').replace('Op', '')\n"
            "    if ident == short:\n"
            "        found_idx = i\n"
            "        break\n"
            "if found_idx < 0:\n"
            "    raise ValueError('No op matches identifier: ' + ident)\n"
            "n = ctrl.get_num_retarget_ops()\n"
            "clamped = max(0, min(target_idx, n - 1))\n"
            "ctrl.move_retarget_op_in_stack(found_idx, clamped)\n"
            "final_ops = []\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    final_ops.append({'index': i, 'name': str(ctrl.get_op_name(i)), 'type': type(ctrl.get_op_controller(i)).__name__})\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'moved_from': found_idx,\n"
            "    'moved_to': clamped,\n"
            "    'requested_index': target_idx,\n"
            "    'ops': final_ops,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)
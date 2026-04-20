"""Retargeter tuning ergonomics tools.

Promoted from per-iteration friction observed during real fine-tuning. These
are small, independent utilities that add up to a much tighter tune loop:

- ``mirror_bone_offsets``: mirror L/R rotation offsets in one call. Quaternion
  mirror formula depends on skeleton convention so we expose four canonical
  methods and let the caller pick. Auto-detects L/R bone pairs by name suffix
  when ``bones`` is omitted.
- ``batch_set_bone_rotation_offset``: set rotation offsets for N bones in one
  UE round-trip, given ``{bone: [roll, pitch, yaw]}`` in degrees.
- ``duplicate_retarget_pose``: snapshot the current retarget pose under a new
  name so experimental tweaks don't clobber a known-good state. Optionally
  switches to the new pose so subsequent edits don't retroactively modify the
  snapshot.
- ``reset_bones_to_reference``: zero out offsets on a curated list of bones
  without touching the rest of the pose. Useful for reverting a tuning branch.
"""

import json
import re

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


# Common L/R suffix/prefix patterns for auto-pairing.
_PAIR_PATTERNS = [
    # MetaHuman / UE mannequin: hand_l, hand_r, upperarm_twist_01_l, ...
    (re.compile(r"^(.+)_l$"), r"\1_r"),
    (re.compile(r"^(.+)_l_([0-9]+)$"), r"\1_r_\2"),
    # Mixamo: LeftArm, LeftHand, LeftUpLeg, LeftHandIndex1
    (re.compile(r"^Left(.+)$"), r"Right\1"),
    (re.compile(r"^left(.+)$"), r"right\1"),
]


def _derive_right_name(left_name: str) -> str | None:
    for rx, repl in _PAIR_PATTERNS:
        if rx.match(left_name):
            return rx.sub(repl, left_name)
    return None


def register(server):
    @server.tool(
        name="mirror_bone_offsets",
        description=(
            "Mirror retarget-pose rotation offsets from one side of a humanoid "
            "rig to the other. Auto-detects L/R bone pairs by name suffix ('_l' "
            "to '_r') or prefix ('Left' to 'Right') when bones is omitted. "
            "method picks the quaternion mirror formula: 'copy' (useful when "
            "L/R bones already have mirrored local frames, e.g. most UE/MH "
            "rigs); 'negate_yz' (flip Y and Z components of the quaternion - "
            "standard sagittal mirror); 'negate_xz' or 'negate_xy' (for rigs "
            "with non-standard bone-space orientation). Returns before/after "
            "euler offsets for every affected bone. "
            "direction: 'LtoR' (default) or 'RtoL'."
        ),
    )
    async def mirror_bone_offsets(
        retargeter_path: str,
        direction: str = "LtoR",
        method: str = "copy",
        bones: list = None,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if direction not in ("LtoR", "RtoL"):
            return _err(f"direction must be 'LtoR' or 'RtoL', got {direction!r}")
        if method not in ("copy", "negate_yz", "negate_xz", "negate_xy", "negate_w_yz"):
            return _err(
                f"method must be one of copy | negate_yz | negate_xz | negate_xy | negate_w_yz, got {method!r}"
            )

        # Build pairs on the MCP side when possible — auto-detection needs a
        # Python regex, and UE's Python lacks a convenient pair-matcher.
        pairs: list[tuple[str, str]] = []
        if bones:
            # Caller supplied the L side; derive R names.
            for b in bones:
                other = _derive_right_name(b)
                if other is None:
                    return _err(f"Cannot derive R pair for bone {b!r}. Use a '_l' suffix or 'Left*' prefix.")
                pairs.append((b, other))
        else:
            # Caller wants auto-detection against the current target skeleton's bone list.
            # Do that server-side since we don't have the bone list here.
            pairs = []

        # Flip the pair direction for RtoL before sending.
        if direction == "RtoL":
            pairs = [(r, l) for l, r in pairs]

        pairs_json = json.dumps(pairs)
        rtp = escape_string(retargeter_path)
        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"

        script = wrap_script(
            "import unreal\n"
            "import re as _re\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"SIDE = unreal.RetargetSourceOrTarget.{side}\n"
            f"direction = '{direction}'\n"
            f"method = '{method}'\n"
            f"_pairs = {pairs_json}\n"
            # If empty, auto-detect from the target skeleton's full bone list.
            "if not _pairs:\n"
            "    rig = ctrl.get_ik_rig(SIDE)\n"
            "    rig_ctrl = unreal.IKRigController.get_controller(rig) if rig else None\n"
            "    mesh = rig_ctrl.get_skeletal_mesh() if rig_ctrl else None\n"
            "    bones = []\n"
            "    if mesh is not None:\n"
            "        actor = unreal.EditorLevelLibrary.spawn_actor_from_class(\n"
            "            unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "        try:\n"
            "            smc = actor.get_component_by_class(unreal.SkeletalMeshComponent)\n"
            "            smc.set_skeletal_mesh_asset(mesh)\n"
            "            bones = [str(smc.get_bone_name(i)) for i in range(smc.get_num_bones())]\n"
            "        finally:\n"
            "            unreal.EditorLevelLibrary.destroy_actor(actor)\n"
            "    _patterns = [\n"
            "        (_re.compile(r'^(.+)_l$'), r'\\1_r'),\n"
            "        (_re.compile(r'^(.+)_l_([0-9]+)$'), r'\\1_r_\\2'),\n"
            "        (_re.compile(r'^Left(.+)$'), r'Right\\1'),\n"
            "        (_re.compile(r'^left(.+)$'), r'right\\1'),\n"
            "    ]\n"
            "    bone_set = set(bones)\n"
            "    for b in bones:\n"
            "        for rx, repl in _patterns:\n"
            "            if rx.match(b):\n"
            "                other = rx.sub(repl, b)\n"
            "                if other in bone_set and other != b:\n"
            "                    _pairs.append([b, other])\n"
            "                break\n"
            "    if direction == 'RtoL':\n"
            "        _pairs = [[r, l] for l, r in _pairs]\n"
            "# Apply mirror to each pair\n"
            "def _mirror(q):\n"
            "    if method == 'copy':       return unreal.Quat(q.x, q.y, q.z, q.w)\n"
            "    if method == 'negate_yz':  return unreal.Quat(q.x, -q.y, -q.z, q.w)\n"
            "    if method == 'negate_xz':  return unreal.Quat(-q.x, q.y, -q.z, q.w)\n"
            "    if method == 'negate_xy':  return unreal.Quat(-q.x, -q.y, q.z, q.w)\n"
            "    if method == 'negate_w_yz':return unreal.Quat(q.x, -q.y, -q.z, -q.w)\n"
            "    return unreal.Quat(q.x, q.y, q.z, q.w)\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "result = []\n"
            "for src_bone, dst_bone in _pairs:\n"
            "    try:\n"
            "        src_q = ctrl.get_rotation_offset_for_retarget_pose_bone(src_bone, SIDE)\n"
            "        before = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(dst_bone, SIDE))\n"
            "        mirrored = _mirror(src_q)\n"
            "        ctrl.set_rotation_offset_for_retarget_pose_bone(dst_bone, mirrored, SIDE)\n"
            "        after = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(dst_bone, SIDE))\n"
            "        result.append({'src': src_bone, 'dst': dst_bone, 'src_deg': _deg(src_q), 'before_deg': before, 'after_deg': after})\n"
            "    except Exception as _e:\n"
            "        result.append({'src': src_bone, 'dst': dst_bone, 'error': str(_e)})\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'direction': direction,\n"
            "    'method': method,\n"
            "    'pair_count': len(_pairs),\n"
            "    'applied': result,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="batch_set_bone_rotation_offset",
        description=(
            "Set rotation offsets for multiple bones in one call. offsets is "
            "a dict mapping bone_name to [roll, pitch, yaw] in degrees. One "
            "UE round-trip instead of N. Returns before/after for each bone. "
            "source_or_target: 'Source' or 'Target' (default 'Target')."
        ),
    )
    async def batch_set_bone_rotation_offset(
        retargeter_path: str,
        offsets: dict,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not isinstance(offsets, dict) or not offsets:
            return _err("offsets must be a non-empty dict of {bone_name: [roll, pitch, yaw]}")

        # Validate each entry is a 3-tuple of numbers
        clean = {}
        for bone, rpy in offsets.items():
            if not (isinstance(rpy, (list, tuple)) and len(rpy) == 3):
                return _err(f"Bone {bone!r} offset must be [roll, pitch, yaw] (3 floats), got {rpy!r}")
            try:
                clean[str(bone)] = [float(rpy[0]), float(rpy[1]), float(rpy[2])]
            except (TypeError, ValueError) as e:
                return _err(f"Bone {bone!r} offset contains non-numeric values: {e}")

        rtp = escape_string(retargeter_path)
        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"
        offsets_json = json.dumps(clean)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"SIDE = unreal.RetargetSourceOrTarget.{side}\n"
            f"_offsets = {offsets_json}\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "result = []\n"
            "for bone, rpy in _offsets.items():\n"
            "    try:\n"
            "        before = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(bone, SIDE))\n"
            "        q = unreal.Rotator(roll=rpy[0], pitch=rpy[1], yaw=rpy[2]).quaternion()\n"
            "        ctrl.set_rotation_offset_for_retarget_pose_bone(bone, q, SIDE)\n"
            "        after = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(bone, SIDE))\n"
            "        result.append({'bone': bone, 'before_deg': before, 'after_deg': after})\n"
            "    except Exception as _e:\n"
            "        result.append({'bone': bone, 'error': str(_e)})\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'count': len(_offsets),\n"
            "    'applied': result,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="duplicate_retarget_pose",
        description=(
            "Duplicate the current retarget pose under a new name so experimental "
            "tweaks don't clobber a known-good state. When set_current=True the "
            "retargeter switches to the new pose so subsequent edits modify the "
            "duplicate. source_or_target: 'Source' or 'Target' (default 'Target')."
        ),
    )
    async def duplicate_retarget_pose(
        retargeter_path: str,
        new_pose_name: str,
        source_or_target: str = "Target",
        set_current: bool = False,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not new_pose_name or not new_pose_name.strip():
            return _err("new_pose_name must be a non-empty string")

        rtp = escape_string(retargeter_path)
        npn = escape_string(new_pose_name)
        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"
        make_current = "True" if set_current else "False"

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"SIDE = unreal.RetargetSourceOrTarget.{side}\n"
            "current = str(ctrl.get_current_retarget_pose_name(SIDE))\n"
            f'new_name = unreal.Name("{npn}")\n'
            "ctrl.duplicate_retarget_pose(unreal.Name(current), new_name, SIDE)\n"
            f"if {make_current}:\n"
            "    ctrl.set_current_retarget_pose(new_name, SIDE)\n"
            "pose_names = [str(n) for n in ctrl.get_retarget_poses(SIDE).keys()]\n"
            "now_current = str(ctrl.get_current_retarget_pose_name(SIDE))\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'duplicated_from': current,\n"
            f'    "duplicated_to": "{npn}",\n'
            "    'current_pose': now_current,\n"
            "    'all_poses': pose_names,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="reset_bones_to_reference",
        description=(
            "Zero out retarget-pose rotation offsets on a list of bones, "
            "restoring them to their reference-pose orientation. The rest of "
            "the pose is untouched. Useful for reverting a tuning branch "
            "without a global reset. source_or_target: 'Source' or 'Target'."
        ),
    )
    async def reset_bones_to_reference(
        retargeter_path: str,
        bones: list,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not isinstance(bones, list) or not bones:
            return _err("bones must be a non-empty list of bone names")

        rtp = escape_string(retargeter_path)
        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"
        bones_json = json.dumps([str(b) for b in bones])

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"SIDE = unreal.RetargetSourceOrTarget.{side}\n"
            f"_bones = {bones_json}\n"
            "zero = unreal.Quat(0.0, 0.0, 0.0, 1.0)\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "result = []\n"
            "for b in _bones:\n"
            "    try:\n"
            "        before = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, SIDE))\n"
            "        ctrl.set_rotation_offset_for_retarget_pose_bone(b, zero, SIDE)\n"
            "        after = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, SIDE))\n"
            "        result.append({'bone': b, 'before_deg': before, 'after_deg': after})\n"
            "    except Exception as _e:\n"
            "        result.append({'bone': b, 'error': str(_e)})\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'count': len(_bones),\n"
            "    'reset': result,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)
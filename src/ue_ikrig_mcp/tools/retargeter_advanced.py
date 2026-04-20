"""Advanced retargeter tuning tools.

Promoted from recurring execute_python patterns during real fine-tuning sessions:

- ``inspect_retargeter_full``: one-shot full snapshot of a retargeter's state
  (rigs, poses, chain mappings including unmapped, all ops with settings, sample
  bone rotation offsets in degrees, per-chain IK static offsets). Replaces the
  dozens of lines of manual probing needed to understand "what is this
  retargeter currently doing?"

- ``set_ik_chain_static_offset``: per-chain IK goal translation offset on the
  Retarget IK Goals op's ``ChainsToRetarget`` array. In UE 5.6 the path via
  ``TargetChainSettings.IK.StaticOffset`` is deprecated and silently writes to a
  dead struct. This tool goes through the op-stack path that actually affects
  runtime. Typical use: shift leg IK goals down by a few cm when the source
  skeleton's toe bone sits higher than the target's, so feet plant on the floor.

- ``measure_bone_ref_pose_delta``: world-space ref-pose position delta between a
  bone on the source skeletal mesh and a bone on the target. Used to compute
  the exact number of centimeters to feed to ``set_ik_chain_static_offset``.
  Implemented by spawning a transient ``SkeletalMeshActor`` at origin,
  reading ``get_socket_location(bone)`` (bones are queryable as sockets), then
  destroying the actor.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_INSPECT_SCRIPT = r"""
import unreal
import json as _json

rtg = unreal.load_asset("{rtp}")
if rtg is None:
    raise ValueError("IKRetargeter not found: {rtp}")
ctrl = unreal.IKRetargeterController.get_controller(rtg)

SRC = unreal.RetargetSourceOrTarget.SOURCE
TGT = unreal.RetargetSourceOrTarget.TARGET

def _deg(q):
    r = q.rotator()
    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]

src_rig = ctrl.get_ik_rig(SRC)
tgt_rig = ctrl.get_ik_rig(TGT)
src_rig_path = src_rig.get_path_name() if src_rig else None
tgt_rig_path = tgt_rig.get_path_name() if tgt_rig else None
src_rig_ctrl = unreal.IKRigController.get_controller(src_rig) if src_rig else None
tgt_rig_ctrl = unreal.IKRigController.get_controller(tgt_rig) if tgt_rig else None

# Chain mappings: walk target chains; source can be "None"
target_chains = [str(ch.chain_name) for ch in tgt_rig_ctrl.get_retarget_chains()] if tgt_rig_ctrl else []
mapped_pairs = []
unmapped_target = []
for tc in target_chains:
    sc = str(ctrl.get_source_chain(tc))
    if sc and sc != "None":
        mapped_pairs.append([sc, tc])
    else:
        unmapped_target.append(tc)

# Ops
ops = []
for i in range(ctrl.get_num_retarget_ops()):
    op_ctrl = ctrl.get_op_controller(i)
    settings = op_ctrl.get_settings() if hasattr(op_ctrl, 'get_settings') else None
    ops.append({{
        "index": i,
        "name": str(ctrl.get_op_name(i)),
        "type": type(op_ctrl).__name__,
        "enabled": ctrl.get_retarget_op_enabled(i),
        "settings": settings.export_text() if settings else "",
    }})

# Per-chain IK static offsets (if Retarget IK Goals op exists)
ik_static = {{}}
for op_info in ops:
    op_ctrl = ctrl.get_op_controller(op_info["index"])
    if type(op_ctrl).__name__ == "IKRetargetIKChainsController":
        s = op_ctrl.get_settings()
        arr = s.get_editor_property("chains_to_retarget") if s else []
        for ch in (arr or []):
            tcn = str(ch.get_editor_property("target_chain_name"))
            so = ch.get_editor_property("static_offset")
            slo = ch.get_editor_property("static_local_offset")
            ik_static[tcn] = {{
                "static_offset": [so.x, so.y, so.z],
                "static_local_offset": [slo.x, slo.y, slo.z],
                "enable_ik": bool(ch.get_editor_property("enable_ik")),
                "blend_to_source": float(ch.get_editor_property("blend_to_source")),
                "blend_to_source_translation": float(ch.get_editor_property("blend_to_source_translation")),
                "blend_to_source_rotation": float(ch.get_editor_property("blend_to_source_rotation")),
                "scale_vertical": float(ch.get_editor_property("scale_vertical")),
            }}
        break

# Current retarget poses
src_pose = str(ctrl.get_current_retarget_pose_name(SRC))
tgt_pose = str(ctrl.get_current_retarget_pose_name(TGT))
src_poses = [str(n) for n in ctrl.get_retarget_poses(SRC).keys()]
tgt_poses = [str(n) for n in ctrl.get_retarget_poses(TGT).keys()]

# Sample bone offsets (degrees) for common humanoid bones on target
sample_bones = {sample_bones}
tgt_offsets = {{}}
for b in sample_bones:
    try:
        tgt_offsets[b] = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, TGT))
    except Exception as _e:
        tgt_offsets[b] = "ERR " + str(_e)

# Root offsets in retarget pose
try:
    src_root = ctrl.get_root_offset_in_retarget_pose(SRC)
    src_root_list = [src_root.x, src_root.y, src_root.z]
except Exception:
    src_root_list = None
try:
    tgt_root = ctrl.get_root_offset_in_retarget_pose(TGT)
    tgt_root_list = [tgt_root.x, tgt_root.y, tgt_root.z]
except Exception:
    tgt_root_list = None

result = {{
    "source_rig": src_rig_path,
    "target_rig": tgt_rig_path,
    "source_pose": src_pose,
    "target_pose": tgt_pose,
    "source_poses": src_poses,
    "target_poses": tgt_poses,
    "mapped_count": len(mapped_pairs),
    "mapped_pairs": mapped_pairs,
    "unmapped_target_chains": unmapped_target,
    "op_count": len(ops),
    "ops": ops,
    "ik_chain_settings": ik_static,
    "sample_target_offsets_deg": tgt_offsets,
    "source_root_offset": src_root_list,
    "target_root_offset": tgt_root_list,
}}
print("__MCP_RESULT__" + _json.dumps(result))
"""


_DEFAULT_SAMPLE_BONES = [
    "pelvis", "spine_01", "spine_03", "spine_05", "neck_01", "head",
    "clavicle_l", "upperarm_l", "lowerarm_l", "hand_l",
    "clavicle_r", "upperarm_r", "lowerarm_r", "hand_r",
    "thigh_l", "calf_l", "foot_l", "ball_l",
    "thigh_r", "calf_r", "foot_r", "ball_r",
]


def register(server):
    @server.tool(
        name="inspect_retargeter_full",
        description=(
            "One-shot full snapshot of an IK Retargeter: source/target rigs, "
            "both current retarget poses and all pose names, all chain mappings "
            "with explicit list of target chains that have no source mapping "
            "(the gotcha for auto_align_all_bones crashing), every op in the "
            "stack with its serialized settings, per-chain IK static offsets "
            "from the Retarget IK Goals op, and sample target bone rotation "
            "offsets in degrees for a standard humanoid bone set. Replaces "
            "the manual Python probes needed to understand a retargeter's state."
        ),
    )
    async def inspect_retargeter_full(
        retargeter_path: str,
        sample_bones: list = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        bones = sample_bones if sample_bones else list(_DEFAULT_SAMPLE_BONES)
        bones_literal = json.dumps(bones)  # safe Python list literal
        rtp = escape_string(retargeter_path)

        script = _INSPECT_SCRIPT.format(rtp=rtp, sample_bones=bones_literal)
        # wrap_script prepends the json import + indents; our script is already
        # top-level, so pass it through its own wrapping path used by other tools.
        script = wrap_script(script)
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="set_ik_chain_static_offset",
        description=(
            "Set per-chain IK goal translation offset on an IK Retargeter's "
            "Retarget IK Goals op (the UE 5.6 op-stack path; the legacy "
            "TargetChainSettings.IK.StaticOffset path is deprecated and writes "
            "to a dead struct). Typical use: shift LeftLeg/RightLeg IK goals "
            "down by a few centimeters so feet plant on the floor when the "
            "source rig's toe bone sits higher than the target's. "
            "When local=True, the offset is applied in bone-local space "
            "(StaticLocalOffset); otherwise world/component space (StaticOffset)."
        ),
    )
    async def set_ik_chain_static_offset(
        retargeter_path: str,
        target_chain_name: str,
        x: float = 0.0,
        y: float = 0.0,
        z: float = 0.0,
        local: bool = False,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        tcn = escape_string(target_chain_name)
        prop = "static_local_offset" if local else "static_offset"

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "ik_op = None\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    c = ctrl.get_op_controller(i)\n"
            "    if type(c).__name__ == 'IKRetargetIKChainsController':\n"
            "        ik_op = c\n"
            "        break\n"
            "if ik_op is None:\n"
            "    raise ValueError('No IKRetargetIKChainsController (Retarget IK Goals op) on this retargeter')\n"
            "settings = ik_op.get_settings()\n"
            "arr = settings.get_editor_property('chains_to_retarget')\n"
            "found = False\n"
            "new_arr = []\n"
            "for ch in arr:\n"
            "    name = str(ch.get_editor_property('target_chain_name'))\n"
            f'    if name == "{tcn}":\n'
            f'        ch.set_editor_property("{prop}", unreal.Vector({float(x)}, {float(y)}, {float(z)}))\n'
            "        found = True\n"
            "    new_arr.append(ch)\n"
            "if not found:\n"
            f'    raise ValueError("Target chain not in ChainsToRetarget: {tcn}. The op only tracks chains that have IK goals on the target IK Rig.")\n'
            "settings.set_editor_property('chains_to_retarget', new_arr)\n"
            "ik_op.set_settings(settings)\n"
            "# Verify\n"
            "verify = None\n"
            "for ch in ik_op.get_settings().get_editor_property('chains_to_retarget'):\n"
            f'    if str(ch.get_editor_property("target_chain_name")) == "{tcn}":\n'
            f'        v = ch.get_editor_property("{prop}")\n'
            "        verify = [v.x, v.y, v.z]\n"
            "        break\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            f'    "chain": "{tcn}",\n'
            f'    "offset_kind": "{prop}",\n'
            "    \"applied\": verify,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="measure_bone_ref_pose_delta",
        description=(
            "Measure the world-space ref-pose position delta between a bone on "
            "the source skeletal mesh and a bone on the target. Spawns transient "
            "SkeletalMeshActors at world origin, reads get_socket_location(bone) "
            "(bones are queryable as sockets in UE), destroys the actors. "
            "Use this to compute the exact number of cm to feed to "
            "set_ik_chain_static_offset — e.g. Delta Z between source "
            "LeftToeBase and target ball_l tells you how much to shift the "
            "LeftLeg IK goal so feet plant on the ground."
        ),
    )
    async def measure_bone_ref_pose_delta(
        source_rig_path: str,
        source_bone: str,
        target_rig_path: str,
        target_bone: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        srp = escape_string(source_rig_path)
        sb = escape_string(source_bone)
        trp = escape_string(target_rig_path)
        tb = escape_string(target_bone)

        script = wrap_script(
            "import unreal\n"
            "def _probe(rig_path, bone):\n"
            "    rig = unreal.load_asset(rig_path)\n"
            "    if rig is None:\n"
            "        return None, 'IKRig not found: ' + rig_path\n"
            "    mesh = unreal.IKRigController.get_controller(rig).get_skeletal_mesh()\n"
            "    if mesh is None:\n"
            "        return None, 'IKRig has no skeletal mesh: ' + rig_path\n"
            "    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(\n"
            "        unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        smc = actor.get_component_by_class(unreal.SkeletalMeshComponent)\n"
            "        smc.set_skeletal_mesh_asset(mesh)\n"
            "        loc = smc.get_socket_location(bone)\n"
            "        return [loc.x, loc.y, loc.z], None\n"
            "    finally:\n"
            "        unreal.EditorLevelLibrary.destroy_actor(actor)\n"
            f'src_pos, src_err = _probe("{srp}", "{sb}")\n'
            f'tgt_pos, tgt_err = _probe("{trp}", "{tb}")\n'
            "delta = None\n"
            "if src_pos is not None and tgt_pos is not None:\n"
            "    delta = [round(src_pos[0]-tgt_pos[0], 3), round(src_pos[1]-tgt_pos[1], 3), round(src_pos[2]-tgt_pos[2], 3)]\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            f'    "source_rig": "{srp}",\n'
            f'    "source_bone": "{sb}",\n'
            f'    "target_rig": "{trp}",\n'
            f'    "target_bone": "{tb}",\n'
            "    \"source_world_pos\": [round(src_pos[0],3), round(src_pos[1],3), round(src_pos[2],3)] if src_pos else None,\n"
            "    \"target_world_pos\": [round(tgt_pos[0],3), round(tgt_pos[1],3), round(tgt_pos[2],3)] if tgt_pos else None,\n"
            "    \"delta_src_minus_tgt\": delta,\n"
            "    \"source_error\": src_err,\n"
            "    \"target_error\": tgt_err,\n"
            "    \"hint\": \"For feet-on-floor: pass -delta.z as z to set_ik_chain_static_offset on the matching leg chain.\",\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)
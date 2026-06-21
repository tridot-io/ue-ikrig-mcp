"""Export/import retargeter bone rotation offsets and IK chain static offsets as JSON.

Workaround for UE bug UE-195858: world-space offsets on a retargeter reset to
zero on editor restart. Exporting them to JSON and re-importing on demand (or
via a startup hook) preserves rigger work across sessions.

Captures:
  * Per-bone retarget pose rotation offsets (source and/or target)
  * Per-chain IK static_offset + static_local_offset on the Retarget IK Goals op

Semantics on re-import: SET (not ADD). A re-imported JSON produces the exact
same offsets as when exported; it does not stack on top of existing state.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_SIDE_OPTIONS = {"Source", "Target", "Both"}


def register(server):
    @server.tool(
        name="export_bone_offsets_json",
        description=(
            "Export retargeter state to a JSON file on disk for durable storage "
            "across editor restarts (workaround for UE-195858 world-space offset "
            "reset bug). Captures per-bone rotation offsets on Source and/or "
            "Target retarget poses, plus per-chain IK static_offset / "
            "static_local_offset on the Retarget IK Goals op."
        ),
    )
    async def export_bone_offsets_json(
        retargeter_path: str,
        out_path: str,
        include_source: bool = True,
        include_target: bool = True,
        include_ik_chain_offsets: bool = True,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        op = escape_string(out_path)
        inc_s = "True" if include_source else "False"
        inc_t = "True" if include_target else "False"
        inc_ik = "True" if include_ik_chain_offsets else "False"

        script = wrap_script(
            "import unreal\n"
            "import json as _json\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"inc_s = {inc_s}; inc_t = {inc_t}; inc_ik = {inc_ik}\n"
            # Helper: iterate bones from an IK rig's preview mesh via spawned actor
            "def _bones_on_side(side):\n"
            "    rig = ctrl.get_ik_rig(side)\n"
            "    if rig is None: return []\n"
            "    mesh = rig.get_editor_property('preview_skeletal_mesh')\n"
            "    if mesh is None: return []\n"
            "    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "    actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    names = []\n"
            "    try:\n"
            "        comp = actor.skeletal_mesh_component\n"
            "        comp.set_skeletal_mesh_asset(mesh)\n"
            "        n = comp.get_num_bones()\n"
            "        for i in range(n):\n"
            "            names.append(str(comp.get_bone_name(i)))\n"
            "    finally:\n"
            "        eas.destroy_actor(actor)\n"
            "    return names\n"
            "def _capture_offsets(side):\n"
            "    entries = {}\n"
            "    bones = _bones_on_side(side)\n"
            "    for b in bones:\n"
            "        try:\n"
            "            q = ctrl.get_rotation_offset_for_retarget_pose_bone(b, side)\n"
            "            if q is None: continue\n"
            "            # Skip identity quaternions\n"
            "            if abs(q.x) < 1e-6 and abs(q.y) < 1e-6 and abs(q.z) < 1e-6 and abs(q.w - 1.0) < 1e-6:\n"
            "                continue\n"
            "            entries[b] = {'x': float(q.x), 'y': float(q.y), 'z': float(q.z), 'w': float(q.w)}\n"
            "        except Exception:\n"
            "            continue\n"
            "    return entries\n"
            "def _capture_ik_chain_offsets():\n"
            "    result = {}\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ != 'IKRetargetIKChainsController':\n"
            "            continue\n"
            "        stg = oc.get_settings()\n"
            "        chains = stg.get_editor_property('chains_to_retarget')\n"
            "        for c in chains:\n"
            "            name = str(c.get_editor_property('target_chain_name'))\n"
            "            so = c.get_editor_property('static_offset')\n"
            "            sol = c.get_editor_property('static_local_offset')\n"
            "            so_vec = [float(so.x), float(so.y), float(so.z)]\n"
            "            sol_vec = [float(sol.x), float(sol.y), float(sol.z)]\n"
            "            if any(abs(v) > 1e-6 for v in so_vec + sol_vec):\n"
            "                result[name] = {'static_offset': so_vec, 'static_local_offset': sol_vec}\n"
            "        break\n"
            "    return result\n"
            "payload = {'retargeter_path': rtg.get_path_name(), 'version': 1}\n"
            "if inc_s:\n"
            "    payload['source_bone_offsets'] = _capture_offsets(unreal.RetargetSourceOrTarget.SOURCE)\n"
            "if inc_t:\n"
            "    payload['target_bone_offsets'] = _capture_offsets(unreal.RetargetSourceOrTarget.TARGET)\n"
            "if inc_ik:\n"
            "    payload['ik_chain_offsets'] = _capture_ik_chain_offsets()\n"
            f'_out = r"{escape_string(out_path)}"\n'
            "import os as _os\n"
            "_os.makedirs(_os.path.dirname(_out) or '.', exist_ok=True)\n"
            "with open(_out, 'w', encoding='utf-8') as _f:\n"
            "    _json.dump(payload, _f, indent=2, ensure_ascii=False)\n"
            "summary = {\n"
            "    'out_path': _out,\n"
            "    'source_bone_count': len(payload.get('source_bone_offsets', {})),\n"
            "    'target_bone_count': len(payload.get('target_bone_offsets', {})),\n"
            "    'ik_chain_count': len(payload.get('ik_chain_offsets', {})),\n"
            "}\n"
            'print("__MCP_RESULT__" + json.dumps(summary))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="import_bone_offsets_json",
        description=(
            "Restore retargeter state from a JSON file previously produced by "
            "export_bone_offsets_json. Uses SET semantics (not ADD): re-importing "
            "reproduces the exact exported state regardless of current offsets. "
            "source_or_target restricts which side gets restored: 'Source', "
            "'Target', or 'Both' (default). IK chain static offsets restored "
            "unconditionally when present in the JSON."
        ),
    )
    async def import_bone_offsets_json(
        retargeter_path: str,
        in_path: str,
        source_or_target: str = "Both",
    ) -> list[TextContent]:
        if source_or_target not in _SIDE_OPTIONS:
            return _err(f"source_or_target must be one of {sorted(_SIDE_OPTIONS)}")
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        ip = escape_string(in_path)
        side_arg = f'"{source_or_target}"'

        script = wrap_script(
            "import unreal\n"
            "import json as _json\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f'with open(r"{ip}", "r", encoding="utf-8") as _f:\n'
            "    payload = _json.load(_f)\n"
            f"_side = {side_arg}\n"
            "applied = {'source_bone_offsets': 0, 'target_bone_offsets': 0, 'ik_chain_offsets': 0}\n"
            "skipped = {'source_bone_offsets': [], 'target_bone_offsets': [], 'ik_chain_offsets': []}\n"
            "def _apply_bone_offsets(key, side_enum):\n"
            "    entries = payload.get(key, {})\n"
            "    for bone, q in entries.items():\n"
            "        try:\n"
            "            quat = unreal.Quat(float(q['x']), float(q['y']), float(q['z']), float(q['w']))\n"
            "            ctrl.set_rotation_offset_for_retarget_pose_bone(bone, quat, side_enum)\n"
            "            applied[key] += 1\n"
            "        except Exception as e:\n"
            "            skipped[key].append(f'{bone}: {e}')\n"
            "if _side in ('Source', 'Both'):\n"
            "    _apply_bone_offsets('source_bone_offsets', unreal.RetargetSourceOrTarget.SOURCE)\n"
            "if _side in ('Target', 'Both'):\n"
            "    _apply_bone_offsets('target_bone_offsets', unreal.RetargetSourceOrTarget.TARGET)\n"
            "ik_offsets = payload.get('ik_chain_offsets', {})\n"
            "if ik_offsets:\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ != 'IKRetargetIKChainsController': continue\n"
            "        stg = oc.get_settings()\n"
            "        chains = stg.get_editor_property('chains_to_retarget')\n"
            "        new_chains = []\n"
            "        for c in chains:\n"
            "            name = str(c.get_editor_property('target_chain_name'))\n"
            "            ns = unreal.RetargetIKChainSettings()\n"
            "            for p in ['target_chain_name', 'enable_ik', 'blend_to_source',\n"
            "                      'blend_to_source_translation', 'blend_to_source_rotation',\n"
            "                      'scale_vertical', 'static_offset', 'static_local_offset']:\n"
            "                try:\n"
            "                    ns.set_editor_property(p, c.get_editor_property(p))\n"
            "                except Exception: pass\n"
            "            if name in ik_offsets:\n"
            "                spec = ik_offsets[name]\n"
            "                so = spec.get('static_offset', [0, 0, 0])\n"
            "                sol = spec.get('static_local_offset', [0, 0, 0])\n"
            "                ns.set_editor_property('static_offset', unreal.Vector(float(so[0]), float(so[1]), float(so[2])))\n"
            "                ns.set_editor_property('static_local_offset', unreal.Vector(float(sol[0]), float(sol[1]), float(sol[2])))\n"
            "                applied['ik_chain_offsets'] += 1\n"
            "            new_chains.append(ns)\n"
            "        stg.set_editor_property('chains_to_retarget', new_chains)\n"
            "        oc.set_settings(stg)\n"
            "        break\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({"applied": applied, "skipped": skipped, "saved": saved}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

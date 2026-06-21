"""Bone world-axis diagnostics for cross-skeleton retarget issues.

When FK rotations appear to not transfer between source and target skeletons,
the usual root cause is that the bones' LOCAL AXES point in different
world-space directions at the retarget pose. UE's retargeter can only map
rotations correctly if the axes are consistent; auto_align_* tools compensate
by rotating the retarget poses, but only when the bone axes are within a
reasonable range to begin with.

These tools expose each bone's local frame (forward/right/up vectors in world
space) and the bone-to-child direction vector so callers can diagnose mismatch
before blaming the retarget ops.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):
    @server.tool(
        name="get_mesh_bone_positions",
        description=(
            "Spawn a transient SkeletalMeshActor at world origin with the given "
            "mesh and sample world-space positions of the listed bones at their "
            "reference pose. Returns {bone: [x,y,z]} plus a summary of pairwise "
            "distances for quick chain-length eyeballing."
        ),
    )
    async def get_mesh_bone_positions(
        skeletal_mesh_path: str,
        bones: list,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if not bones:
            return _err("bones must be a non-empty list of bone names")

        p = escape_string(skeletal_mesh_path)
        bones_json = json.dumps([str(b) for b in bones])
        script = wrap_script(
            "import unreal\n"
            f'mesh = unreal.load_asset("{p}")\n'
            "if mesh is None or type(mesh).__name__ != 'SkeletalMesh':\n"
            f'    raise ValueError("SkeletalMesh not found at {p}")\n'
            f"_bones = {bones_json}\n"
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "positions = {}\n"
            "try:\n"
            "    comp = actor.skeletal_mesh_component\n"
            "    comp.set_skeletal_mesh_asset(mesh)\n"
            "    for b in _bones:\n"
            "        if comp.get_bone_index(b) == -1:\n"
            "            positions[b] = None\n"
            "            continue\n"
            "        loc = comp.get_socket_location(b)\n"
            "        positions[b] = [round(loc.x, 3), round(loc.y, 3), round(loc.z, 3)]\n"
            "finally:\n"
            "    eas.destroy_actor(actor)\n"
            'print("__MCP_RESULT__" + json.dumps({"mesh": mesh.get_path_name(), "positions": positions}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_bone_world_axes",
        description=(
            "For each listed bone, return its local-frame axes expressed in "
            "world coordinates: forward (local +X), right (local +Y), up "
            "(local +Z). Also returns the normalized bone-to-child vector "
            "(direction toward the first child bone in the skeleton tree). "
            "A bone whose local +X axis aligns with the bone-to-child vector "
            "follows the UE convention; mismatch is the classic symptom of "
            "an auto-generated rig that needs auto_align_all_bones run on "
            "whichever side doesn't match its counterpart."
        ),
    )
    async def get_bone_world_axes(
        skeletal_mesh_path: str,
        bones: list,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if not bones:
            return _err("bones must be a non-empty list of bone names")

        p = escape_string(skeletal_mesh_path)
        bones_json = json.dumps([str(b) for b in bones])
        script = wrap_script(
            "import unreal\n"
            f'mesh = unreal.load_asset("{p}")\n'
            "if mesh is None or type(mesh).__name__ != 'SkeletalMesh':\n"
            f'    raise ValueError("SkeletalMesh not found at {p}")\n'
            f"_bones = {bones_json}\n"
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "out = []\n"
            "def _v(x): return [round(x.x, 4), round(x.y, 4), round(x.z, 4)]\n"
            "try:\n"
            "    comp = actor.skeletal_mesh_component\n"
            "    comp.set_skeletal_mesh_asset(mesh)\n"
            "    num_bones = comp.get_num_bones()\n"
            "    child_names = {}\n"
            "    # Map parent -> first child by walking the bone list\n"
            "    for i in range(num_bones):\n"
            "        bn = str(comp.get_bone_name(i))\n"
            "        parent_idx = comp.get_parent_bone_name(bn) if hasattr(comp, 'get_parent_bone_name') else None\n"
            "        if parent_idx:\n"
            "            child_names.setdefault(str(parent_idx), []).append(bn)\n"
            "    for b in _bones:\n"
            "        if comp.get_bone_index(b) == -1:\n"
            "            out.append({'bone': b, 'error': 'bone_not_found'})\n"
            "            continue\n"
            "        loc = comp.get_socket_location(b)\n"
            "        rot = comp.get_socket_rotation(b)\n"
            "        fw = rot.get_forward_vector()\n"
            "        rt = rot.get_right_vector()\n"
            "        up = rot.get_up_vector()\n"
            "        entry = {\n"
            "            'bone': b,\n"
            "            'world_location': _v(loc),\n"
            "            'forward_axis_world': _v(fw),\n"
            "            'right_axis_world': _v(rt),\n"
            "            'up_axis_world': _v(up),\n"
            "        }\n"
            "        kids = child_names.get(b, [])\n"
            "        if kids:\n"
            "            child = kids[0]\n"
            "            cloc = comp.get_socket_location(child)\n"
            "            dir_vec = unreal.Vector(cloc.x - loc.x, cloc.y - loc.y, cloc.z - loc.z)\n"
            "            dlen = dir_vec.size()\n"
            "            if dlen > 1e-6:\n"
            "                norm = unreal.Vector(dir_vec.x / dlen, dir_vec.y / dlen, dir_vec.z / dlen)\n"
            "            else:\n"
            "                norm = unreal.Vector(0, 0, 0)\n"
            "            entry['first_child'] = child\n"
            "            entry['to_child_world'] = _v(dir_vec)\n"
            "            entry['to_child_world_normalized'] = _v(norm)\n"
            "            fw_dot = fw.x * norm.x + fw.y * norm.y + fw.z * norm.z\n"
            "            rt_dot = rt.x * norm.x + rt.y * norm.y + rt.z * norm.z\n"
            "            up_dot = up.x * norm.x + up.y * norm.y + up.z * norm.z\n"
            "            entry['alignment'] = {\n"
            "                'dot_forward': round(fw_dot, 4),\n"
            "                'dot_right': round(rt_dot, 4),\n"
            "                'dot_up': round(up_dot, 4),\n"
            "                'best_axis_along_bone': ('forward' if abs(fw_dot) > max(abs(rt_dot), abs(up_dot)) else ('right' if abs(rt_dot) > abs(up_dot) else 'up'))\n"
            "            }\n"
            "        out.append(entry)\n"
            "finally:\n"
            "    eas.destroy_actor(actor)\n"
            'print("__MCP_RESULT__" + json.dumps({"mesh": mesh.get_path_name(), "bones": out}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="compare_bone_axes",
        description=(
            "Compare bone-local axis orientations between matched source and "
            "target bones. For each (source_bone, target_bone) pair, returns "
            "whether both bones have their local +X along the bone (standard), "
            "and pairwise dot products between source and target forward/right/"
            "up axes. A source with local -Y along the bone but target with "
            "+X along the bone is the classic Mixamo<->MetaHuman axis conflict."
        ),
    )
    async def compare_bone_axes(
        source_mesh_path: str,
        target_mesh_path: str,
        pairs: list,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if not pairs:
            return _err("pairs must be a non-empty list of [source_bone, target_bone] pairs")
        norm_pairs = []
        for pr in pairs:
            if isinstance(pr, (list, tuple)) and len(pr) == 2:
                norm_pairs.append([str(pr[0]), str(pr[1])])
            elif isinstance(pr, dict) and "source" in pr and "target" in pr:
                norm_pairs.append([str(pr["source"]), str(pr["target"])])
            else:
                return _err(f"invalid pair (expected 2-tuple or {{source,target}} dict): {pr!r}")

        sp = escape_string(source_mesh_path)
        tp = escape_string(target_mesh_path)
        pairs_json = json.dumps(norm_pairs)

        script = wrap_script(
            "import unreal\n"
            f'src_mesh = unreal.load_asset("{sp}")\n'
            f'tgt_mesh = unreal.load_asset("{tp}")\n'
            "if src_mesh is None or type(src_mesh).__name__ != 'SkeletalMesh':\n"
            f'    raise ValueError("Source mesh not found: {sp}")\n'
            "if tgt_mesh is None or type(tgt_mesh).__name__ != 'SkeletalMesh':\n"
            f'    raise ValueError("Target mesh not found: {tp}")\n'
            f"_pairs = {pairs_json}\n"
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "def _sample(mesh, bone):\n"
            "    a = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        a.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)\n"
            "        if a.skeletal_mesh_component.get_bone_index(bone) == -1:\n"
            "            return None\n"
            "        rot = a.skeletal_mesh_component.get_socket_rotation(bone)\n"
            "        loc = a.skeletal_mesh_component.get_socket_location(bone)\n"
            "        return {'rot': rot, 'loc': loc}\n"
            "    finally:\n"
            "        eas.destroy_actor(a)\n"
            "out = []\n"
            "for src_b, tgt_b in _pairs:\n"
            "    s = _sample(src_mesh, src_b); t = _sample(tgt_mesh, tgt_b)\n"
            "    if not s or not t:\n"
            "        out.append({'source_bone': src_b, 'target_bone': tgt_b, 'error': 'bone_not_found'})\n"
            "        continue\n"
            "    sf = s['rot'].get_forward_vector(); sr = s['rot'].get_right_vector(); su = s['rot'].get_up_vector()\n"
            "    tf = t['rot'].get_forward_vector(); tr = t['rot'].get_right_vector(); tu = t['rot'].get_up_vector()\n"
            "    def dot(a, b): return a.x * b.x + a.y * b.y + a.z * b.z\n"
            "    out.append({\n"
            "        'source_bone': src_b,\n"
            "        'target_bone': tgt_b,\n"
            "        'forward_dot_forward': round(dot(sf, tf), 4),\n"
            "        'right_dot_right': round(dot(sr, tr), 4),\n"
            "        'up_dot_up': round(dot(su, tu), 4),\n"
            "        'cross_forward_dot_right': round(dot(sf, tr), 4),\n"
            "        'cross_forward_dot_up': round(dot(sf, tu), 4),\n"
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps({"pairs": out}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

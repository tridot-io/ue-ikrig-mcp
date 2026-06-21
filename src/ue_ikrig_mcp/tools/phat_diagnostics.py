"""PhysicsAsset / PhAT body diagnostics.

UPrimitiveComponent::GetClosestPointOnCollision, which retargeter-adjacent
components often use to detect hand-torso proximity, silently returns false
for bones that don't have a PhAT body. When the whitelist of candidate torso
bones doesn't match the PhAT's actual bodies, the caller sees "no detection"
with no error — a common source of silent failures in arm push-out rigs.

These tools surface which bones in a skeletal mesh's PhAT asset actually have
physics bodies, so callers can build the correct whitelist.
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
        name="list_phat_bodies",
        description=(
            "List every bone that has a physics body in the PhAT associated "
            "with a skeletal mesh. Accepts either a SkeletalMesh path "
            "(auto-resolves its physics_asset) or a PhysicsAsset path directly. "
            "Spawns a transient actor to query body instances, then destroys it."
        ),
    )
    async def list_phat_bodies(asset_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(asset_path)
        script = wrap_script(
            "import unreal\n"
            f'asset = unreal.load_asset("{p}")\n'
            "if asset is None:\n"
            f'    raise ValueError("Asset not found: {p}")\n'
            "mesh = None\n"
            "phat = None\n"
            "atype = type(asset).__name__\n"
            "if atype == 'SkeletalMesh':\n"
            "    mesh = asset\n"
            "    phat = mesh.get_editor_property('physics_asset')\n"
            "elif atype == 'PhysicsAsset':\n"
            "    phat = asset\n"
            "    # Need a mesh with this PhAT to query via body_instance API\n"
            "    preview = phat.get_editor_property('preview_skeletal_mesh') if hasattr(phat, 'get_editor_property') else None\n"
            "    mesh = preview\n"
            "else:\n"
            f'    raise ValueError(f"Asset must be SkeletalMesh or PhysicsAsset, got {{atype}}")\n'
            "if phat is None:\n"
            '    raise ValueError("No PhysicsAsset associated with this mesh")\n'
            "info = {'mesh': mesh.get_path_name() if mesh else None,\n"
            "        'phat': phat.get_path_name(),\n"
            "        'bodies_with_body': [], 'all_skeleton_bones': []}\n"
            "if mesh is not None:\n"
            "    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "    actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        comp = actor.skeletal_mesh_component\n"
            "        comp.set_skeletal_mesh_asset(mesh)\n"
            "        comp.set_collision_enabled(unreal.CollisionEnabled.QUERY_ONLY)\n"
            "        bone_names = [str(n) for n in comp.get_all_socket_names()]\n"
            "        bodies = []\n"
            "        for b in bone_names:\n"
            "            bi = comp.get_body_instance(b)\n"
            "            if bi is not None:\n"
            "                bodies.append(b)\n"
            "        info['bodies_with_body'] = bodies\n"
            "        info['all_skeleton_bones'] = bone_names\n"
            "    finally:\n"
            "        eas.destroy_actor(actor)\n"
            'print("__MCP_RESULT__" + json.dumps(info))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="check_phat_bodies_for_bones",
        description=(
            "Check whether each bone in a list has a physics body in the mesh's "
            "PhAT. Returns a map of bone -> has_body. Use this to diagnose why "
            "GetClosestPointOnCollision silently fails (no body = no detection) "
            "when building a torso whitelist for arm-pushout-style components."
        ),
    )
    async def check_phat_bodies_for_bones(
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
            "phat = mesh.get_editor_property('physics_asset')\n"
            f"_bones = {bones_json}\n"
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "result = {}\n"
            "try:\n"
            "    comp = actor.skeletal_mesh_component\n"
            "    comp.set_skeletal_mesh_asset(mesh)\n"
            "    comp.set_collision_enabled(unreal.CollisionEnabled.QUERY_ONLY)\n"
            "    for b in _bones:\n"
            "        bone_exists = comp.get_bone_index(b) != -1\n"
            "        bi = comp.get_body_instance(b) if bone_exists else None\n"
            "        result[b] = {'bone_exists': bone_exists, 'has_body': bi is not None}\n"
            "finally:\n"
            "    eas.destroy_actor(actor)\n"
            "summary = {\n"
            "    'phat': phat.get_path_name() if phat else None,\n"
            "    'bodies_found': [k for k, v in result.items() if v['has_body']],\n"
            "    'bones_missing_body': [k for k, v in result.items() if v['bone_exists'] and not v['has_body']],\n"
            "    'bones_not_in_skeleton': [k for k, v in result.items() if not v['bone_exists']],\n"
            "    'detail': result,\n"
            "}\n"
            'print("__MCP_RESULT__" + json.dumps(summary))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

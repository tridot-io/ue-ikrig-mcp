"""AnimBlueprint inspection & targeted TwoBoneIK node editing.

Downstream animation blueprints that consume retargeter output often wire
Two Bone IK nodes for post-correction (arm push-out, foot planting, etc.).
When things go wrong the usual culprit is a misconfigured pin:
 - EffectorTarget on the wrong bone (common: middle bone instead of IK bone)
 - EffectorLocationSpace mismatched with how the calling code computed the vector
 - JointTarget default-initialized to NAME_None silently making UE fall back to
   component space when bone-space was intended

These tools let a caller audit and fix those fields without opening the AnimBP
editor. They cannot create or wire NEW nodes (UE 5.6 has no Python API for
AnimGraph node creation), only read and update existing ones.
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
        name="list_animbp_twobone_ik_nodes",
        description=(
            "List all Two Bone IK nodes across every AnimGraph in the given "
            "AnimBlueprint, returning their configuration for audit. Per node: "
            "graph name, node name, ik_bone, effector_target bone, "
            "joint_target bone, effector/joint location spaces, alpha default. "
            "Use this to verify that EffectorTarget matches IKBone (common bug: "
            "EffectorTarget left as the middle bone)."
        ),
    )
    async def list_animbp_twobone_ik_nodes(anim_bp_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(anim_bp_path)
        script = wrap_script(
            "import unreal\n"
            f'abp = unreal.load_asset("{p}")\n'
            "if abp is None:\n"
            f'    raise ValueError("AnimBlueprint not found: {p}")\n'
            "if type(abp).__name__ != 'AnimBlueprint':\n"
            '    raise ValueError(f"Asset is not an AnimBlueprint: {type(abp).__name__}")\n'
            "graphs = abp.get_animation_graphs()\n"
            "out = []\n"
            "for g in graphs:\n"
            "    nodes = g.get_graph_nodes_of_class(unreal.AnimGraphNode_TwoBoneIK)\n"
            "    for n in nodes:\n"
            "        inner = n.get_editor_property('node')\n"
            "        if inner is None: continue\n"
            "        def _bonename(bst):\n"
            "            try:\n"
            "                br = bst.get_editor_property('bone_reference')\n"
            "                return str(br.get_editor_property('bone_name'))\n"
            "            except Exception:\n"
            "                return None\n"
            "        ik = inner.get_editor_property('ik_bone')\n"
            "        ik_name = str(ik.get_editor_property('bone_name')) if ik else None\n"
            "        et = inner.get_editor_property('effector_target')\n"
            "        jt = inner.get_editor_property('joint_target')\n"
            "        out.append({\n"
            "            'graph': g.get_name(),\n"
            "            'node': n.get_name(),\n"
            "            'ik_bone': ik_name,\n"
            "            'effector_target_bone': _bonename(et) if et else None,\n"
            "            'joint_target_bone': _bonename(jt) if jt else None,\n"
            "            'effector_location_space': str(inner.get_editor_property('effector_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "            'joint_target_location_space': str(inner.get_editor_property('joint_target_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "            'alpha': float(inner.get_editor_property('alpha')),\n"
            "        })\n"
            'print("__MCP_RESULT__" + json.dumps({"node_count": len(out), "nodes": out}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="set_twobone_ik_node_bones",
        description=(
            "Update bone references on a single Two Bone IK node in an "
            "AnimBlueprint. Locate the node by ik_bone name (more stable than "
            "auto-generated node_name like 'AnimGraphNode_TwoBoneIK_1'). Only "
            "supplied fields are changed. Spaces use UE's enum names: "
            "BCS_ComponentSpace, BCS_ParentBoneSpace, BCS_BoneSpace, BCS_WorldSpace."
        ),
    )
    async def set_twobone_ik_node_bones(
        anim_bp_path: str,
        find_by_ik_bone: str,
        effector_target_bone: str = None,
        joint_target_bone: str = None,
        effector_location_space: str = None,
        joint_target_location_space: str = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        valid_spaces = {"BCS_COMPONENTSPACE", "BCS_PARENTBONESPACE", "BCS_BONESPACE", "BCS_WORLDSPACE"}
        if effector_location_space is not None and effector_location_space.upper() not in valid_spaces:
            return _err(f"effector_location_space must be one of {sorted(valid_spaces)}")
        if joint_target_location_space is not None and joint_target_location_space.upper() not in valid_spaces:
            return _err(f"joint_target_location_space must be one of {sorted(valid_spaces)}")

        p = escape_string(anim_bp_path)
        ik = escape_string(find_by_ik_bone)
        et = f'"{escape_string(effector_target_bone)}"' if effector_target_bone is not None else "None"
        jt = f'"{escape_string(joint_target_bone)}"' if joint_target_bone is not None else "None"
        els = f'"{effector_location_space.upper()}"' if effector_location_space is not None else "None"
        jls = f'"{joint_target_location_space.upper()}"' if joint_target_location_space is not None else "None"

        script = wrap_script(
            "import unreal\n"
            f'abp = unreal.load_asset("{p}")\n'
            "if abp is None:\n"
            f'    raise ValueError("AnimBlueprint not found: {p}")\n'
            "if type(abp).__name__ != 'AnimBlueprint':\n"
            '    raise ValueError(f"Asset is not an AnimBlueprint: {type(abp).__name__}")\n'
            "graphs = abp.get_animation_graphs()\n"
            f'_find_ik = "{ik}"\n'
            f"_et = {et}\n"
            f"_jt = {jt}\n"
            f"_els = {els}\n"
            f"_jls = {jls}\n"
            "target_node = None\n"
            "target_graph = None\n"
            "for g in graphs:\n"
            "    for n in g.get_graph_nodes_of_class(unreal.AnimGraphNode_TwoBoneIK):\n"
            "        inner = n.get_editor_property('node')\n"
            "        if inner is None: continue\n"
            "        ik_br = inner.get_editor_property('ik_bone')\n"
            "        if ik_br and str(ik_br.get_editor_property('bone_name')) == _find_ik:\n"
            "            target_node = n\n"
            "            target_graph = g\n"
            "            break\n"
            "    if target_node: break\n"
            "if target_node is None:\n"
            f'    raise ValueError(f"No TwoBoneIK node with ik_bone={{_find_ik!r}} found")\n'
            "inner = target_node.get_editor_property('node')\n"
            "before = {\n"
            "    'effector_target_bone': None,\n"
            "    'joint_target_bone': None,\n"
            "    'effector_location_space': str(inner.get_editor_property('effector_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "    'joint_target_location_space': str(inner.get_editor_property('joint_target_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "}\n"
            "def _set_bonesockettarget(bst, bone_name):\n"
            "    br = bst.get_editor_property('bone_reference')\n"
            "    br.set_editor_property('bone_name', unreal.Name(bone_name))\n"
            "    bst.set_editor_property('bone_reference', br)\n"
            "    return bst\n"
            "if _et is not None:\n"
            "    et_s = inner.get_editor_property('effector_target')\n"
            "    try:\n"
            "        before['effector_target_bone'] = str(et_s.get_editor_property('bone_reference').get_editor_property('bone_name'))\n"
            "    except Exception:\n"
            "        pass\n"
            "    et_s = _set_bonesockettarget(et_s, _et)\n"
            "    inner.set_editor_property('effector_target', et_s)\n"
            "if _jt is not None:\n"
            "    jt_s = inner.get_editor_property('joint_target')\n"
            "    try:\n"
            "        before['joint_target_bone'] = str(jt_s.get_editor_property('bone_reference').get_editor_property('bone_name'))\n"
            "    except Exception:\n"
            "        pass\n"
            "    jt_s = _set_bonesockettarget(jt_s, _jt)\n"
            "    inner.set_editor_property('joint_target', jt_s)\n"
            "if _els is not None:\n"
            "    inner.set_editor_property('effector_location_space', getattr(unreal.BoneControlSpace, _els))\n"
            "if _jls is not None:\n"
            "    inner.set_editor_property('joint_target_location_space', getattr(unreal.BoneControlSpace, _jls))\n"
            "target_node.set_editor_property('node', inner)\n"
            "target_node.modify()\n"
            "abp.modify()\n"
            "# Re-read to verify\n"
            "inner2 = target_node.get_editor_property('node')\n"
            "after = {\n"
            "    'effector_target_bone': None,\n"
            "    'joint_target_bone': None,\n"
            "    'effector_location_space': str(inner2.get_editor_property('effector_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "    'joint_target_location_space': str(inner2.get_editor_property('joint_target_location_space')).split('.')[-1].split(':')[0].strip(),\n"
            "}\n"
            "try:\n"
            "    after['effector_target_bone'] = str(inner2.get_editor_property('effector_target').get_editor_property('bone_reference').get_editor_property('bone_name'))\n"
            "except Exception: pass\n"
            "try:\n"
            "    after['joint_target_bone'] = str(inner2.get_editor_property('joint_target').get_editor_property('bone_reference').get_editor_property('bone_name'))\n"
            "except Exception: pass\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{p}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({'
            '"graph": target_graph.get_name(),'
            '"node": target_node.get_name(),'
            '"before": before, "after": after, "saved": saved,'
            '"note": "AnimBP saved; Live Coding / recompile may be needed for runtime effect."'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

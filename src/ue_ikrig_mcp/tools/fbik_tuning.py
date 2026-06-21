"""Full-Body IK (FBIK / PBIK) solver tuning for IK Rigs.

MetaHuman and other character IK Rigs typically have a single FBIK solver
(UIKRigFBIKSolver, Python: IKRigFBIKController) with root-behavior, pre-pull,
goal-strength and per-bone stiffness settings. The canonical access path is:
    IKRigController.get_controller(rig)
      .get_solver_controller(i)              -> IKRigFBIKController
      .get_solver_settings()                 -> IKRigFBIKSettings (root_behavior,
                                                pre_pull_root_settings, iterations)
      .get_bone_settings(bone)               -> IKRigFBIKBoneSettings (rotation/
                                                position_stiffness)
      .get_goal_settings(goal)               -> IKRigFBIKGoalSettings
                                                (pull_chain_alpha, strength_alpha,
                                                 pin_rotation, chain_depth)

Tools here wrap those with targeted get/set surfaces so callers don't need to
re-walk the controller hierarchy for each tweak.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_ROOT_BEHAVIOR = {"PRE_PULL", "PIN_TO_INPUT", "FREE"}


def register(server):
    @server.tool(
        name="get_fbik_solver_settings",
        description=(
            "Read FBIK solver settings: root_bone, root_behavior (PRE_PULL | "
            "PIN_TO_INPUT | FREE), iterations, and the pre_pull_root_settings "
            "(position_alpha_x/y/z, rotation_alpha). solver_index defaults to 0 "
            "(typical character has one solver)."
        ),
    )
    async def get_fbik_solver_settings(
        ik_rig_path: str,
        solver_index: int = 0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(ik_rig_path)
        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "tc = unreal.IKRigController.get_controller(rig)\n"
            f"idx = int({int(solver_index)})\n"
            "if idx < 0 or idx >= tc.get_num_solvers():\n"
            f'    raise ValueError(f"solver_index out of range (num_solvers={{tc.get_num_solvers()}})")\n'
            "sc = tc.get_solver_controller(idx)\n"
            "if type(sc).__name__ != 'IKRigFBIKController':\n"
            f'    raise ValueError(f"solver {{idx}} is not FBIK; got {{type(sc).__name__}}")\n'
            "stg = sc.get_solver_settings()\n"
            "pre = stg.get_editor_property('pre_pull_root_settings')\n"
            "info = {\n"
            "    'solver_index': idx,\n"
            "    'solver_type': type(sc).__name__,\n"
            "    'root_bone': str(stg.get_editor_property('root_bone')),\n"
            "    'root_behavior': str(stg.get_editor_property('root_behavior')).split('.')[-1].split(':')[0].strip(),\n"
            "    'iterations': int(stg.get_editor_property('iterations')),\n"
            "    'pre_pull_root': {\n"
            "        'position_alpha': float(pre.get_editor_property('position_alpha')),\n"
            "        'position_alpha_x': float(pre.get_editor_property('position_alpha_x')),\n"
            "        'position_alpha_y': float(pre.get_editor_property('position_alpha_y')),\n"
            "        'position_alpha_z': float(pre.get_editor_property('position_alpha_z')),\n"
            "        'rotation_alpha': float(pre.get_editor_property('rotation_alpha')),\n"
            "    },\n"
            "}\n"
            'print("__MCP_RESULT__" + json.dumps(info))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_fbik_solver_settings",
        description=(
            "Update FBIK solver settings. Only provided fields are changed. "
            "root_behavior: PRE_PULL | PIN_TO_INPUT | FREE (PIN_TO_INPUT keeps "
            "the pelvis wherever Pelvis Motion put it — recommended for "
            "retargeting workflows to avoid FBIK dragging the root). "
            "pre_pull_position_alpha_z=0 kills vertical pelvis drift while "
            "keeping horizontal balance."
        ),
    )
    async def set_fbik_solver_settings(
        ik_rig_path: str,
        solver_index: int = 0,
        root_behavior: str = None,
        iterations: int = None,
        pre_pull_position_alpha: float = None,
        pre_pull_position_alpha_x: float = None,
        pre_pull_position_alpha_y: float = None,
        pre_pull_position_alpha_z: float = None,
        pre_pull_rotation_alpha: float = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if root_behavior is not None and root_behavior.upper() not in _VALID_ROOT_BEHAVIOR:
            return _err(f"root_behavior must be one of {sorted(_VALID_ROOT_BEHAVIOR)}")

        rp = escape_string(ik_rig_path)
        rb = f'"{root_behavior.upper()}"' if root_behavior is not None else "None"
        it = str(int(iterations)) if iterations is not None else "None"
        pa = str(float(pre_pull_position_alpha)) if pre_pull_position_alpha is not None else "None"
        px = str(float(pre_pull_position_alpha_x)) if pre_pull_position_alpha_x is not None else "None"
        py = str(float(pre_pull_position_alpha_y)) if pre_pull_position_alpha_y is not None else "None"
        pz = str(float(pre_pull_position_alpha_z)) if pre_pull_position_alpha_z is not None else "None"
        pr = str(float(pre_pull_rotation_alpha)) if pre_pull_rotation_alpha is not None else "None"

        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "tc = unreal.IKRigController.get_controller(rig)\n"
            f"idx = int({int(solver_index)})\n"
            "sc = tc.get_solver_controller(idx)\n"
            "if type(sc).__name__ != 'IKRigFBIKController':\n"
            f'    raise ValueError(f"solver {{idx}} is not FBIK")\n'
            "stg = sc.get_solver_settings()\n"
            "pre = stg.get_editor_property('pre_pull_root_settings')\n"
            f"rb_s = {rb}\n"
            f"it_v = {it}\n"
            f"pa_v = {pa}; px_v = {px}; py_v = {py}; pz_v = {pz}; pr_v = {pr}\n"
            "if rb_s is not None:\n"
            "    stg.set_editor_property('root_behavior', getattr(unreal.PBIKRootBehavior, rb_s))\n"
            "if it_v is not None:\n"
            "    stg.set_editor_property('iterations', int(it_v))\n"
            "if pa_v is not None: pre.set_editor_property('position_alpha', float(pa_v))\n"
            "if px_v is not None: pre.set_editor_property('position_alpha_x', float(px_v))\n"
            "if py_v is not None: pre.set_editor_property('position_alpha_y', float(py_v))\n"
            "if pz_v is not None: pre.set_editor_property('position_alpha_z', float(pz_v))\n"
            "if pr_v is not None: pre.set_editor_property('rotation_alpha', float(pr_v))\n"
            "stg.set_editor_property('pre_pull_root_settings', pre)\n"
            "sc.set_solver_settings(stg)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rp}", only_if_is_dirty=False))\n'
            "# verify\n"
            "stg2 = sc.get_solver_settings()\n"
            "pre2 = stg2.get_editor_property('pre_pull_root_settings')\n"
            "after = {\n"
            "    'root_behavior': str(stg2.get_editor_property('root_behavior')).split('.')[-1].split(':')[0].strip(),\n"
            "    'iterations': int(stg2.get_editor_property('iterations')),\n"
            "    'pre_pull_root': {\n"
            "        'position_alpha': float(pre2.get_editor_property('position_alpha')),\n"
            "        'position_alpha_x': float(pre2.get_editor_property('position_alpha_x')),\n"
            "        'position_alpha_y': float(pre2.get_editor_property('position_alpha_y')),\n"
            "        'position_alpha_z': float(pre2.get_editor_property('position_alpha_z')),\n"
            "        'rotation_alpha': float(pre2.get_editor_property('rotation_alpha')),\n"
            "    }\n"
            "}\n"
            'print("__MCP_RESULT__" + json.dumps({"saved": saved, "after": after}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_fbik_goal_settings",
        description=(
            "Read FBIK goal settings for one or all goals. Per goal: bone_name, "
            "chain_depth, pin_rotation, pull_chain_alpha (0 = FBIK rotates "
            "shoulder only to aim the straight limb; 1 = actively bends the "
            "middle joint to reach the goal), strength_alpha (0 = goal ignored, "
            "1 = fully respected)."
        ),
    )
    async def get_fbik_goal_settings(
        ik_rig_path: str,
        goal_name: str = "",
        solver_index: int = 0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(ik_rig_path)
        gn = escape_string(goal_name or "")
        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "tc = unreal.IKRigController.get_controller(rig)\n"
            f"idx = int({int(solver_index)})\n"
            "sc = tc.get_solver_controller(idx)\n"
            "all_goals = tc.get_all_goals()\n"
            f'_filter = "{gn}"\n'
            "out = []\n"
            "for g in all_goals:\n"
            "    name = str(g.goal_name)\n"
            "    if _filter and name != _filter: continue\n"
            "    try:\n"
            "        gs = sc.get_goal_settings(unreal.Name(name))\n"
            "        if gs is None: continue\n"
            "        out.append({\n"
            "            'goal_name': name,\n"
            "            'bone_name': str(gs.get_editor_property('bone_name')),\n"
            "            'chain_depth': int(gs.get_editor_property('chain_depth')),\n"
            "            'pin_rotation': bool(gs.get_editor_property('pin_rotation')),\n"
            "            'pull_chain_alpha': float(gs.get_editor_property('pull_chain_alpha')),\n"
            "            'strength_alpha': float(gs.get_editor_property('strength_alpha')),\n"
            "        })\n"
            "    except Exception as _e:\n"
            "        out.append({'goal_name': name, 'err': str(_e)[:100]})\n"
            'print("__MCP_RESULT__" + json.dumps({"goals": out}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_fbik_goal_settings",
        description=(
            "Update FBIK goal settings. Only provided fields are changed. "
            "pull_chain_alpha=0 keeps the chain mostly straight (IK aims the "
            "chain at the goal but doesn't bend). pull_chain_alpha=1 lets FBIK "
            "actively bend the middle joint. pin_rotation=True pins end-effector "
            "rotation to the goal's. strength_alpha ramps goal influence."
        ),
    )
    async def set_fbik_goal_settings(
        ik_rig_path: str,
        goal_name: str,
        pull_chain_alpha: float = None,
        strength_alpha: float = None,
        pin_rotation: bool = None,
        chain_depth: int = None,
        solver_index: int = 0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(ik_rig_path)
        gn = escape_string(goal_name)
        pc = str(float(pull_chain_alpha)) if pull_chain_alpha is not None else "None"
        sa = str(float(strength_alpha)) if strength_alpha is not None else "None"
        pr = "True" if pin_rotation is True else ("False" if pin_rotation is False else "None")
        cd = str(int(chain_depth)) if chain_depth is not None else "None"

        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "tc = unreal.IKRigController.get_controller(rig)\n"
            f"idx = int({int(solver_index)})\n"
            "sc = tc.get_solver_controller(idx)\n"
            f'_gn = unreal.Name("{gn}")\n'
            "gs = sc.get_goal_settings(_gn)\n"
            "if gs is None:\n"
            f'    raise ValueError(f"No goal settings for goal_name={{_gn}} on solver {{idx}}")\n'
            f"pc_v = {pc}; sa_v = {sa}; pr_v = {pr}; cd_v = {cd}\n"
            "if pc_v is not None: gs.set_editor_property('pull_chain_alpha', float(pc_v))\n"
            "if sa_v is not None: gs.set_editor_property('strength_alpha', float(sa_v))\n"
            "if pr_v is not None: gs.set_editor_property('pin_rotation', bool(pr_v))\n"
            "if cd_v is not None: gs.set_editor_property('chain_depth', int(cd_v))\n"
            "sc.set_goal_settings(_gn, gs)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rp}", only_if_is_dirty=False))\n'
            "gs2 = sc.get_goal_settings(_gn)\n"
            "after = {\n"
            "    'pull_chain_alpha': float(gs2.get_editor_property('pull_chain_alpha')),\n"
            "    'strength_alpha': float(gs2.get_editor_property('strength_alpha')),\n"
            "    'pin_rotation': bool(gs2.get_editor_property('pin_rotation')),\n"
            "    'chain_depth': int(gs2.get_editor_property('chain_depth')),\n"
            "}\n"
            f'print("__MCP_RESULT__" + json.dumps({{"goal": "{gn}", "saved": saved, "after": after}}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_fbik_bone_stiffness",
        description=(
            "Set rotation/position stiffness on one or more bones in the FBIK "
            "solver. Stiffness 1.0 locks the bone (FBIK cannot move/rotate it); "
            "0.0 fully flexible. Typical use: stiffen spine_01..05 to 1.0 so "
            "FBIK cannot twist the spine to reach hand goals. Accepts a single "
            "bone_name or a list via the bones parameter."
        ),
    )
    async def set_fbik_bone_stiffness(
        ik_rig_path: str,
        bones: list,
        rotation_stiffness: float = None,
        position_stiffness: float = None,
        solver_index: int = 0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if not bones:
            return _err("bones must be a non-empty list of bone names")
        if rotation_stiffness is None and position_stiffness is None:
            return _err("at least one of rotation_stiffness or position_stiffness must be provided")

        rp = escape_string(ik_rig_path)
        bones_json = json.dumps([str(b) for b in bones])
        rs = str(float(rotation_stiffness)) if rotation_stiffness is not None else "None"
        ps = str(float(position_stiffness)) if position_stiffness is not None else "None"

        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "tc = unreal.IKRigController.get_controller(rig)\n"
            f"idx = int({int(solver_index)})\n"
            "sc = tc.get_solver_controller(idx)\n"
            f"rs_v = {rs}; ps_v = {ps}\n"
            f"_bones = {bones_json}\n"
            "results = []\n"
            "for b in _bones:\n"
            "    try:\n"
            "        bs = sc.get_bone_settings(b)\n"
            "        if bs is None:\n"
            "            results.append({'bone': b, 'err': 'no_bone_settings'})\n"
            "            continue\n"
            "        row = {'bone': b,\n"
            "               'before': {'rot': float(bs.get_editor_property('rotation_stiffness')),\n"
            "                          'pos': float(bs.get_editor_property('position_stiffness'))}}\n"
            "        if rs_v is not None: bs.set_editor_property('rotation_stiffness', float(rs_v))\n"
            "        if ps_v is not None: bs.set_editor_property('position_stiffness', float(ps_v))\n"
            "        sc.set_bone_settings(b, bs)\n"
            "        bs2 = sc.get_bone_settings(b)\n"
            "        row['after'] = {'rot': float(bs2.get_editor_property('rotation_stiffness')),\n"
            "                        'pos': float(bs2.get_editor_property('position_stiffness'))}\n"
            "        results.append(row)\n"
            "    except Exception as _e:\n"
            "        results.append({'bone': b, 'err': str(_e)[:100]})\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rp}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({"saved": saved, "results": results}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

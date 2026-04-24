"""Targeted helpers for common retargeter-tuning chores.

  * set_root_motion_flags_bulk     — batch AnimSequence root-motion flag setter
  * auto_correct_arm_ratio         — proportion-mismatch corrective offset
  * auto_align_skipping_twists     — auto-align wrapper that protects twist bones
  * bulk_set_translation_retarget_mode — per-chain translation-mode setter

Each addresses a concrete pain point from the rigger/animator industry research
(sciomc 2026-04-21) that had no dedicated tool in v0.10.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_LOCKS = {"ZERO", "REF_POSE", "ANIM_FIRST_FRAME"}
_VALID_TRANS_MODES = {"ANIMATION", "SKELETON", "ANIMATION_SCALED", "ANIMATION_RELATIVE", "ORIENT_AND_SCALE"}
_DEFAULT_TWIST_PATTERNS = [
    "_twist_",
    "twist_01",
    "twist_02",
    "twist_03",
]


def register(server):
    @server.tool(
        name="set_root_motion_flags_bulk",
        description=(
            "Batch-set enable_root_motion, root_motion_root_lock, and force_root_lock "
            "on every AnimSequence in a folder. Mocap imports typically need these "
            "set per-asset with no batch UI available in the editor. "
            "root_motion_root_lock: 'Zero' | 'RefPose' | 'AnimFirstFrame'. "
            "Pass None to leave a flag untouched."
        ),
    )
    async def set_root_motion_flags_bulk(
        folder_path: str,
        enable_root_motion: bool = None,
        root_motion_root_lock: str = None,
        force_root_lock: bool = None,
        recursive: bool = True,
    ) -> list[TextContent]:
        if enable_root_motion is None and root_motion_root_lock is None and force_root_lock is None:
            return _err("at least one of enable_root_motion / root_motion_root_lock / force_root_lock must be provided")
        if root_motion_root_lock is not None:
            norm = root_motion_root_lock.replace(" ", "_").upper()
            if norm not in _VALID_LOCKS:
                return _err(f"root_motion_root_lock must be one of ['Zero', 'RefPose', 'AnimFirstFrame']; got {root_motion_root_lock!r}")
            lock_enum = norm
        else:
            lock_enum = None

        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        fp = escape_string(folder_path)
        er = "True" if enable_root_motion is True else ("False" if enable_root_motion is False else "None")
        lk = f'"{lock_enum}"' if lock_enum is not None else "None"
        frl = "True" if force_root_lock is True else ("False" if force_root_lock is False else "None")
        rec = "True" if recursive else "False"

        script = wrap_script(
            "import unreal\n"
            f'folder = "{fp}"\n'
            f"er_v = {er}; lk_s = {lk}; frl_v = {frl}; rec_v = {rec}\n"
            "eal = unreal.EditorAssetLibrary\n"
            "paths = eal.list_assets(folder, recursive=rec_v, include_folder=False)\n"
            "succeeded = []\nfailed = []\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            "for p in paths:\n"
            "    data = eal.find_asset_data(p)\n"
            "    try:\n"
            "        cls = str(data.asset_class_path.asset_name)\n"
            "    except Exception:\n"
            "        cls = str(data.asset_class)\n"
            "    if cls != 'AnimSequence': continue\n"
            "    try:\n"
            "        anim = unreal.load_asset(p)\n"
            "        if anim is None: failed.append({'path': p, 'err': 'load_asset returned None'}); continue\n"
            "        if er_v is not None:\n"
            "            anim.set_editor_property('enable_root_motion', bool(er_v))\n"
            "        if lk_s is not None:\n"
            "            anim.set_editor_property('root_motion_root_lock', getattr(unreal.RootMotionRootLock, lk_s))\n"
            "        if frl_v is not None:\n"
            "            anim.set_editor_property('force_root_lock', bool(frl_v))\n"
            "        ok = bool(ed.save_asset(p, only_if_is_dirty=False))\n"
            "        succeeded.append(p if ok else {'path': p, 'err': 'save returned False'})\n"
            "    except Exception as e:\n"
            "        failed.append({'path': p, 'err': str(e)[:200]})\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"total_anims": len([1 for _p in paths if str(eal.find_asset_data(_p).asset_class) == "AnimSequence"]),'
            '"succeeded": len(succeeded), "failed": failed'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="auto_correct_arm_ratio",
        description=(
            "Measure source vs target limb length, compute a corrective rotation "
            "offset to compensate for proportion mismatch (e.g. Mixamo→MetaHuman "
            "arms 1.185× source length), and optionally apply it to the target's "
            "upper bone via the retarget pose. Returns the ratio and proposed "
            "offset when apply=False. limb: 'LeftArm' | 'RightArm' | 'LeftLeg' | "
            "'RightLeg'."
        ),
    )
    async def auto_correct_arm_ratio(
        retargeter_path: str,
        limb: str = "LeftArm",
        apply: bool = False,
    ) -> list[TextContent]:
        _LIMBS = {
            "LeftArm":  ("upperarm_l", "hand_l", "LeftArm", "LeftHand"),
            "RightArm": ("upperarm_r", "hand_r", "RightArm", "RightHand"),
            "LeftLeg":  ("thigh_l",    "foot_l", "LeftUpLeg", "LeftFoot"),
            "RightLeg": ("thigh_r",    "foot_r", "RightUpLeg", "RightFoot"),
        }
        if limb not in _LIMBS:
            return _err(f"limb must be one of {sorted(_LIMBS.keys())}")
        tgt_start, tgt_end, src_start, src_end = _LIMBS[limb]
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        ap = "True" if apply else "False"

        script = wrap_script(
            "import unreal\n"
            "import math as _math\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "src_rig = ctrl.get_ik_rig(unreal.RetargetSourceOrTarget.SOURCE)\n"
            "tgt_rig = ctrl.get_ik_rig(unreal.RetargetSourceOrTarget.TARGET)\n"
            "src_mesh = src_rig.get_editor_property('preview_skeletal_mesh') if src_rig else None\n"
            "tgt_mesh = tgt_rig.get_editor_property('preview_skeletal_mesh') if tgt_rig else None\n"
            "if src_mesh is None or tgt_mesh is None:\n"
            '    raise ValueError("preview mesh missing on source or target rig")\n'
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "def _sample(mesh, start, end):\n"
            "    a = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        a.skeletal_mesh_component.set_skeletal_mesh_asset(mesh)\n"
            "        s_i = a.skeletal_mesh_component.get_bone_index(start)\n"
            "        e_i = a.skeletal_mesh_component.get_bone_index(end)\n"
            "        if s_i == -1 or e_i == -1: return None, None\n"
            "        s = a.skeletal_mesh_component.get_socket_location(start)\n"
            "        e = a.skeletal_mesh_component.get_socket_location(end)\n"
            "        return s, e\n"
            "    finally:\n"
            "        eas.destroy_actor(a)\n"
            f'ss, se = _sample(src_mesh, "{src_start}", "{src_end}")\n'
            f'ts, te = _sample(tgt_mesh, "{tgt_start}", "{tgt_end}")\n'
            "if ss is None:\n"
            f'    raise ValueError("source bones not found: {src_start}/{src_end}")\n'
            "if ts is None:\n"
            f'    raise ValueError("target bones not found: {tgt_start}/{tgt_end}")\n'
            "def _dist(a, b): return _math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2 + (a.z-b.z)**2)\n"
            "src_len = _dist(ss, se)\n"
            "tgt_len = _dist(ts, te)\n"
            "ratio = (src_len / tgt_len) if tgt_len > 1e-4 else float('nan')\n"
            # Correction strategy: the arm length ratio (src/tgt) drives how much the
            # source animation over-extends target's shorter arm. We compensate by
            # recommending a SourceScaleFactor, NOT by rotating upperarm (which would
            # dislocate the shoulder). If apply=True, we also leave the user a hint.
            "recommended_scale = (tgt_len / src_len) if src_len > 1e-4 else 1.0\n"
            "result = {\n"
            f'    "limb": "{limb}",\n'
            f'    "source_bones": ("{src_start}", "{src_end}"),\n'
            f'    "target_bones": ("{tgt_start}", "{tgt_end}"),\n'
            "    'source_length_cm': round(src_len, 3),\n"
            "    'target_length_cm': round(tgt_len, 3),\n"
            "    'ratio_src_over_tgt': round(ratio, 4),\n"
            "    'recommended_source_scale_factor': round(recommended_scale, 4),\n"
            "    'applied': False,\n"
            "}\n"
            f"if {ap}:\n"
            "    # Apply by setting Scale Source op's source_scale_factor to the recommended value.\n"
            "    ss_idx = -1\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ == 'IKRetargetScaleSourceController':\n"
            "            ss_idx = i; break\n"
            "    if ss_idx >= 0:\n"
            "        oc = ctrl.get_op_controller(ss_idx)\n"
            "        stg = oc.get_settings()\n"
            "        stg.set_editor_property('source_scale_factor', float(recommended_scale))\n"
            "        oc.set_settings(stg)\n"
            "        ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'        ed.save_asset("{rtp}", only_if_is_dirty=False)\n'
            "        result['applied'] = True\n"
            "        result['applied_to_op'] = 'Scale Source'\n"
            "    else:\n"
            "        result['applied'] = False\n"
            "        result['applied_to_op'] = None\n"
            "        result['warning'] = 'No Scale Source op found; cannot auto-apply'\n"
            'print("__MCP_RESULT__" + json.dumps(result))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="auto_align_skipping_twists",
        description=(
            "Wrapper around auto_align_all_bones that preserves twist-bone "
            "sub-chain rotations (the default auto-align often garbles them). "
            "Runs auto_align, then resets rotation offsets on any bone matching "
            "the twist patterns back to zero. Default patterns catch "
            "upperarm/lowerarm/thigh/calf twist_01..03 on MetaHuman-style "
            "skeletons; override via exclude_patterns."
        ),
    )
    async def auto_align_skipping_twists(
        retargeter_path: str,
        source_or_target: str = "Target",
        exclude_patterns: list = None,
    ) -> list[TextContent]:
        if source_or_target not in ("Source", "Target"):
            return _err("source_or_target must be 'Source' or 'Target'")
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        patterns = exclude_patterns if exclude_patterns else _DEFAULT_TWIST_PATTERNS
        patterns_json = json.dumps([str(p).lower() for p in patterns])
        rtp = escape_string(retargeter_path)
        side_enum = f"unreal.RetargetSourceOrTarget.{source_or_target.upper()}"

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"side = {side_enum}\n"
            f"patterns = {patterns_json}\n"
            # Snapshot twist-bone offsets BEFORE auto-align so we can restore them.
            "rig = ctrl.get_ik_rig(side)\n"
            "mesh = rig.get_editor_property('preview_skeletal_mesh') if rig else None\n"
            "eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "twist_bones = []\n"
            "before_q = {}\n"
            "if mesh:\n"
            "    actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        comp = actor.skeletal_mesh_component\n"
            "        comp.set_skeletal_mesh_asset(mesh)\n"
            "        n = comp.get_num_bones()\n"
            "        for i in range(n):\n"
            "            bn = str(comp.get_bone_name(i))\n"
            "            if any(p in bn.lower() for p in patterns):\n"
            "                twist_bones.append(bn)\n"
            "    finally:\n"
            "        eas.destroy_actor(actor)\n"
            "    for b in twist_bones:\n"
            "        try:\n"
            "            q = ctrl.get_rotation_offset_for_retarget_pose_bone(b, side)\n"
            "            before_q[b] = q\n"
            "        except Exception:\n"
            "            pass\n"
            # Run auto-align on everything.
            "ctrl.auto_align_all_bones(side)\n"
            # Restore twist-bone offsets.
            "restored = []\n"
            "for b, q in before_q.items():\n"
            "    try:\n"
            "        ctrl.set_rotation_offset_for_retarget_pose_bone(b, q, side)\n"
            "        restored.append(b)\n"
            "    except Exception:\n"
            "        pass\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({'
            '"side": str(side).split(".")[-1].split(":")[0].strip(),'
            '"twist_bones_found": twist_bones,'
            '"twist_bones_restored": restored,'
            '"saved": saved'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="bulk_set_translation_retarget_mode",
        description=(
            "Set the translation retargeting mode on multiple bones at once via "
            "the Skeleton's retargeting settings. mode: 'Animation' | 'Skeleton' "
            "| 'AnimationScaled' | 'AnimationRelative' | 'OrientAndScale'. "
            "Applied to the target IK rig's Skeleton. Pass bone_names=[] to "
            "apply to all bones on the target skeleton."
        ),
    )
    async def bulk_set_translation_retarget_mode(
        retargeter_path: str,
        bone_names: list,
        mode: str,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        norm = (mode or "").replace(" ", "_").upper()
        if norm not in _VALID_TRANS_MODES:
            return _err(f"mode must be one of ['Animation', 'Skeleton', 'AnimationScaled', 'AnimationRelative', 'OrientAndScale']; got {mode!r}")
        if source_or_target not in ("Source", "Target"):
            return _err("source_or_target must be 'Source' or 'Target'")
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        bones_json = json.dumps([str(b) for b in (bone_names or [])])
        side_enum = f"unreal.RetargetSourceOrTarget.{source_or_target.upper()}"
        mode_s = f'"{norm}"'

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"side = {side_enum}\n"
            "rig = ctrl.get_ik_rig(side)\n"
            "if rig is None:\n"
            "    raise ValueError('IK rig missing on requested side')\n"
            "skel_asset = rig.get_editor_property('preview_skeletal_mesh').skeleton\n"
            "if skel_asset is None:\n"
            "    raise ValueError('skeleton asset not found on preview mesh')\n"
            f"target_bones = {bones_json}\n"
            f"mode_enum = getattr(unreal.BoneTranslationRetargetingMode, {mode_s})\n"
            "applied = []\n"
            "failed = []\n"
            "bones_to_set = target_bones\n"
            "if not bones_to_set:\n"
            # Enumerate all bones on the skeleton via the preview mesh
            "    mesh = rig.get_editor_property('preview_skeletal_mesh')\n"
            "    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)\n"
            "    actor = eas.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        comp = actor.skeletal_mesh_component\n"
            "        comp.set_skeletal_mesh_asset(mesh)\n"
            "        bones_to_set = [str(comp.get_bone_name(i)) for i in range(comp.get_num_bones())]\n"
            "    finally:\n"
            "        eas.destroy_actor(actor)\n"
            "for b in bones_to_set:\n"
            "    try:\n"
            "        skel_asset.set_bone_translation_retargeting_mode(b, mode_enum)\n"
            "        applied.append(b)\n"
            "    except Exception as e:\n"
            "        failed.append({'bone': b, 'err': str(e)[:100]})\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            "saved = bool(ed.save_asset(skel_asset.get_path_name(), only_if_is_dirty=False))\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"mode": ' + mode_s + ','
            '"skeleton": skel_asset.get_path_name(),'
            '"applied_count": len(applied), "applied_sample": applied[:10],'
            '"failed": failed[:10], "saved": saved'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

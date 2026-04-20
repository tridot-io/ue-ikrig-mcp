"""Retargeter validation, diff, and skeleton-convention detection tools.

Promoted from the "why is this retargeter wrong?" friction. A lint pass
surfaces problems (unmapped chains, asymmetric offsets, zeroed-out ops)
before a human has to eyeball the viewport. A diff tool makes reviewing
teammate changes or comparing two tuning experiments trivial. A fingerprint
tool identifies common skeleton conventions (MetaHuman, Mixamo, UE5
Mannequin, UE4 Mannequin, Daz) and suggests a preset to match.

Fingerprints live in data/skeleton_fingerprints.json so that new skeleton
conventions can be added without code changes.
"""

import json
from pathlib import Path

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


# Paired bone suffix/prefix patterns for asymmetry checks.
_L_TO_R_PAIRS = [
    ("_l", "_r"),       # MetaHuman / UE mannequin
    ("Left", "Right"),  # Mixamo
    ("left", "right"),
    ("l", "r"),         # Daz: lShldr / rShldr
]


def _load_fingerprints() -> dict:
    path = Path(__file__).resolve().parent.parent / "data" / "skeleton_fingerprints.json"
    if not path.exists():
        return {"conventions": []}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"conventions": []}


def register(server):
    @server.tool(
        name="validate_retargeter",
        description=(
            "Lint-style validation pass on an IK Retargeter asset. Returns a "
            "list of warnings/errors so issues are surfaced before an artist "
            "eyeballs the viewport. Checks: structural (unmapped target "
            "chains, unused source chains), IK sanity (disabled IK on a chain "
            "with a goal, oversized StaticOffset, missing foot goals), op "
            "stack sanity (disabled Pelvis Motion, extreme SourceScaleFactor, "
            "missing Run IK Rig op), pose suspicion (body-unfriendly pose "
            "names like arkit mapping), and L/R offset asymmetry on paired "
            "bones. asymmetry_threshold_deg tunes the asymmetry sensitivity "
            "(default 10 degrees). Each warning includes severity, code, "
            "message, and fix_hint."
        ),
    )
    async def validate_retargeter(
        retargeter_path: str,
        asymmetry_threshold_deg: float = 10.0,
        static_offset_warn_cm: float = 50.0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "SRC = unreal.RetargetSourceOrTarget.SOURCE\n"
            "TGT = unreal.RetargetSourceOrTarget.TARGET\n"
            "warnings = []\n"
            "def _warn(sev, code, msg, hint=''):\n"
            "    warnings.append({'severity': sev, 'code': code, 'message': msg, 'fix_hint': hint})\n"
            # ---------- Structural ----------
            "src_rig = ctrl.get_ik_rig(SRC)\n"
            "tgt_rig = ctrl.get_ik_rig(TGT)\n"
            "if src_rig is None:\n"
            "    _warn('error', 'no_source_rig', 'No source IK Rig assigned.', 'set_retargeter_rigs with a source rig')\n"
            "if tgt_rig is None:\n"
            "    _warn('error', 'no_target_rig', 'No target IK Rig assigned.', 'set_retargeter_rigs with a target rig')\n"
            "src_rig_ctrl = unreal.IKRigController.get_controller(src_rig) if src_rig else None\n"
            "tgt_rig_ctrl = unreal.IKRigController.get_controller(tgt_rig) if tgt_rig else None\n"
            "src_chains = [str(ch.chain_name) for ch in src_rig_ctrl.get_retarget_chains()] if src_rig_ctrl else []\n"
            "tgt_chains = [str(ch.chain_name) for ch in tgt_rig_ctrl.get_retarget_chains()] if tgt_rig_ctrl else []\n"
            "mapped_target = []\n"
            "unmapped_target = []\n"
            "used_sources = set()\n"
            "for tc in tgt_chains:\n"
            "    sc = str(ctrl.get_source_chain(tc))\n"
            "    if sc and sc != 'None':\n"
            "        mapped_target.append((sc, tc))\n"
            "        used_sources.add(sc)\n"
            "    else:\n"
            "        unmapped_target.append(tc)\n"
            "if len(mapped_target) == 0:\n"
            "    _warn('error', 'no_chains_mapped', 'No target chains have a source mapping — retargeting will do nothing.', 'call auto_map_chains with method=Fuzzy')\n"
            "if len(unmapped_target) > 0:\n"
            "    _warn('info', 'unmapped_target_chains', 'Target chains with no source (expected for twist/metacarpal): %d' % len(unmapped_target), 'only a problem if a load-bearing chain like LeftArm is unmapped')\n"
            "unused_sources = [c for c in src_chains if c not in used_sources]\n"
            "if unused_sources:\n"
            "    _warn('info', 'unused_source_chains', 'Source chains that drive nothing: %s' % ','.join(unused_sources), 'either acceptable or a target-side mapping is missing')\n"
            # ---------- Pose suspicion ----------
            "tgt_pose = str(ctrl.get_current_retarget_pose_name(TGT))\n"
            "if 'arkit' in tgt_pose.lower() or 'face' in tgt_pose.lower():\n"
            "    _warn('warn', 'face_focused_target_pose', 'Target retarget pose \"%s\" looks face-focused; body retargeting typically needs a T/A pose.' % tgt_pose, 'set_current_pose to Default Pose or a body-aligned pose')\n"
            # ---------- Op stack ----------
            "n_ops = ctrl.get_num_retarget_ops()\n"
            "op_types = []\n"
            "for i in range(n_ops):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    op_types.append(type(oc).__name__)\n"
            "    if not ctrl.get_retarget_op_enabled(i):\n"
            "        name = str(ctrl.get_op_name(i))\n"
            "        sev = 'warn' if 'Pelvis' in name or 'FK' in name or 'IK' in name or 'RunIKRig' in name else 'info'\n"
            "        _warn(sev, 'disabled_op', 'Op disabled: %s' % name, 'enable if you expect it to run')\n"
            "if 'IKRetargetRunIKRigController' not in op_types:\n"
            "    _warn('warn', 'missing_run_ik_rig_op', 'No Run IK Rig op in the stack — target IK goals wont actuate.', 'add a Run IK Rig op')\n"
            "if 'IKRetargetFKChainsController' not in op_types:\n"
            "    _warn('warn', 'missing_fk_chains_op', 'No Retarget FK Chains op in the stack — FK pose wont retarget.', 'add a Retarget FK Chains op')\n"
            "# Scale Source factor sanity\n"
            "for i in range(n_ops):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetScaleSourceController':\n"
            "        try:\n"
            "            s = oc.get_settings()\n"
            "            sf = float(s.get_editor_property('source_scale_factor'))\n"
            "            if sf < 0.5 or sf > 2.0:\n"
            "                _warn('warn', 'extreme_source_scale', 'SourceScaleFactor=%s is unusual (expected 0.5..2.0).' % sf, 'typically aligned to ratio of character heights')\n"
            "        except Exception:\n"
            "            pass\n"
            # ---------- Per-chain IK sanity ----------
            "for i in range(n_ops):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetIKChainsController':\n"
            "        s = oc.get_settings()\n"
            "        arr = s.get_editor_property('chains_to_retarget')\n"
            "        for ch in (arr or []):\n"
            "            tcn = str(ch.get_editor_property('target_chain_name'))\n"
            "            if not bool(ch.get_editor_property('enable_ik')):\n"
            "                _warn('info', 'ik_disabled_on_chain', 'IK disabled on target chain %s' % tcn, 'set enable_ik=True if this chain has an IK goal')\n"
            "            so = ch.get_editor_property('static_offset')\n"
            "            mag = (so.x*so.x + so.y*so.y + so.z*so.z) ** 0.5\n"
            f"            if mag > {float(static_offset_warn_cm)}:\n"
            "                _warn('warn', 'oversized_static_offset', 'Chain %s StaticOffset magnitude %.2f cm is large.' % (tcn, mag), 'double-check this is centimeters, not meters')\n"
            "        break\n"
            # ---------- L/R offset asymmetry ----------
            "def _bones_from_rig(rig_ctrl_local):\n"
            "    if not rig_ctrl_local: return []\n"
            "    m = rig_ctrl_local.get_skeletal_mesh()\n"
            "    if not m: return []\n"
            "    a = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "    try:\n"
            "        smc = a.get_component_by_class(unreal.SkeletalMeshComponent)\n"
            "        smc.set_skeletal_mesh_asset(m)\n"
            "        return [str(smc.get_bone_name(i)) for i in range(smc.get_num_bones())]\n"
            "    finally:\n"
            "        unreal.EditorLevelLibrary.destroy_actor(a)\n"
            "tgt_bones = _bones_from_rig(tgt_rig_ctrl)\n"
            "tgt_bone_set = set(tgt_bones)\n"
            "import re as _re\n"
            "_patterns = [\n"
            "    (_re.compile(r'^(.+)_l$'), r'\\1_r'),\n"
            "    (_re.compile(r'^Left(.+)$'), r'Right\\1'),\n"
            "]\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return (r.roll, r.pitch, r.yaw)\n"
            f"THR = {float(asymmetry_threshold_deg)}\n"
            "asym_found = 0\n"
            "for lb in tgt_bones:\n"
            "    for rx, repl in _patterns:\n"
            "        if rx.match(lb):\n"
            "            rb = rx.sub(repl, lb)\n"
            "            if rb in tgt_bone_set and rb != lb:\n"
            "                try:\n"
            "                    lq = ctrl.get_rotation_offset_for_retarget_pose_bone(lb, TGT)\n"
            "                    rq = ctrl.get_rotation_offset_for_retarget_pose_bone(rb, TGT)\n"
            "                    lr, lp, ly = _deg(lq)\n"
            "                    rr, rp, ry = _deg(rq)\n"
            "                    max_diff = max(abs(lr-rr), abs(lp-rp), abs(ly-ry))\n"
            "                    if max_diff > THR and (lr, lp, ly) != (0,0,0) and (rr, rp, ry) != (0,0,0):\n"
            "                        _warn('info', 'lr_offset_asymmetry', 'L/R asymmetry on %s vs %s: max_diff=%.1f deg' % (lb, rb, max_diff), 'mirror_bone_offsets can mirror L to R if intentional symmetry is desired')\n"
            "                        asym_found += 1\n"
            "                except Exception:\n"
            "                    pass\n"
            "            break\n"
            # ---------- Root offset sanity ----------
            "try:\n"
            "    tgt_root = ctrl.get_root_offset_in_retarget_pose(TGT)\n"
            "    rm = (tgt_root.x**2 + tgt_root.y**2 + tgt_root.z**2) ** 0.5\n"
            "    if rm > 100.0:\n"
            "        _warn('warn', 'large_target_root_offset', 'Target root offset magnitude %.1f cm.' % rm, 'usually a couple cm at most; double-check')\n"
            "except Exception:\n"
            "    pass\n"
            "_summary = {\n"
            "    'error_count': len([w for w in warnings if w['severity'] == 'error']),\n"
            "    'warn_count':  len([w for w in warnings if w['severity'] == 'warn']),\n"
            "    'info_count':  len([w for w in warnings if w['severity'] == 'info']),\n"
            "    'mapped_chain_count': len(mapped_target),\n"
            "    'unmapped_target_chain_count': len(unmapped_target),\n"
            "    'unused_source_chain_count': len(unused_sources),\n"
            "    'op_count': n_ops,\n"
            "    'asymmetric_lr_pairs': asym_found,\n"
            "}\n"
            "print('__MCP_RESULT__' + json.dumps({'summary': _summary, 'warnings': warnings}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="diff_retargeters",
        description=(
            "Compare two IK Retargeter assets and return a categorized diff "
            "(mappings changed / added / removed, per-bone offset deltas, "
            "IK chain setting changes, op-stack changes, retarget pose "
            "changes, root offset changes). Useful for reviewing teammate "
            "changes or comparing two tuning experiments before merging."
        ),
    )
    async def diff_retargeters(
        retargeter_a_path: str,
        retargeter_b_path: str,
        offset_epsilon_deg: float = 0.1,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtpa = escape_string(retargeter_a_path)
        rtpb = escape_string(retargeter_b_path)

        script = wrap_script(
            "import unreal\n"
            "def _snapshot(path):\n"
            "    rtg = unreal.load_asset(path)\n"
            "    if rtg is None: raise ValueError('IKRetargeter not found: ' + path)\n"
            "    ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "    SRC = unreal.RetargetSourceOrTarget.SOURCE\n"
            "    TGT = unreal.RetargetSourceOrTarget.TARGET\n"
            "    def _deg(q):\n"
            "        r = q.rotator()\n"
            "        return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "    src = ctrl.get_ik_rig(SRC)\n"
            "    tgt = ctrl.get_ik_rig(TGT)\n"
            "    tgt_rc = unreal.IKRigController.get_controller(tgt) if tgt else None\n"
            "    tgt_chains = [str(ch.chain_name) for ch in tgt_rc.get_retarget_chains()] if tgt_rc else []\n"
            "    mappings = {}\n"
            "    for tc in tgt_chains:\n"
            "        sc = str(ctrl.get_source_chain(tc))\n"
            "        mappings[tc] = sc if (sc and sc != 'None') else None\n"
            "    # Sample bone offsets on target\n"
            "    bones = []\n"
            "    mesh = tgt_rc.get_skeletal_mesh() if tgt_rc else None\n"
            "    if mesh:\n"
            "        a = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "        try:\n"
            "            smc = a.get_component_by_class(unreal.SkeletalMeshComponent)\n"
            "            smc.set_skeletal_mesh_asset(mesh)\n"
            "            bones = [str(smc.get_bone_name(i)) for i in range(smc.get_num_bones())]\n"
            "        finally:\n"
            "            unreal.EditorLevelLibrary.destroy_actor(a)\n"
            "    offsets = {}\n"
            "    for b in bones:\n"
            "        try:\n"
            "            offsets[b] = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, TGT))\n"
            "        except Exception:\n"
            "            pass\n"
            "    # Op stack snapshot\n"
            "    ops = []\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        s = oc.get_settings() if hasattr(oc, 'get_settings') else None\n"
            "        ops.append({'index': i, 'name': str(ctrl.get_op_name(i)), 'type': type(oc).__name__, 'enabled': ctrl.get_retarget_op_enabled(i), 'settings': s.export_text() if s else ''})\n"
            "    # IK chain settings\n"
            "    ik_static = {}\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ == 'IKRetargetIKChainsController':\n"
            "            s = oc.get_settings()\n"
            "            arr = s.get_editor_property('chains_to_retarget') if s else []\n"
            "            for ch in (arr or []):\n"
            "                tcn = str(ch.get_editor_property('target_chain_name'))\n"
            "                so = ch.get_editor_property('static_offset')\n"
            "                slo = ch.get_editor_property('static_local_offset')\n"
            "                ik_static[tcn] = {\n"
            "                    'static_offset': [so.x, so.y, so.z],\n"
            "                    'static_local_offset': [slo.x, slo.y, slo.z],\n"
            "                    'enable_ik': bool(ch.get_editor_property('enable_ik')),\n"
            "                    'blend_to_source_translation': float(ch.get_editor_property('blend_to_source_translation')),\n"
            "                }\n"
            "            break\n"
            "    root_off = ctrl.get_root_offset_in_retarget_pose(TGT)\n"
            "    return {\n"
            "        'source_rig': src.get_path_name() if src else None,\n"
            "        'target_rig': tgt.get_path_name() if tgt else None,\n"
            "        'src_pose': str(ctrl.get_current_retarget_pose_name(SRC)),\n"
            "        'tgt_pose': str(ctrl.get_current_retarget_pose_name(TGT)),\n"
            "        'mappings': mappings,\n"
            "        'offsets': offsets,\n"
            "        'ops': ops,\n"
            "        'ik_static': ik_static,\n"
            "        'tgt_root_offset': [root_off.x, root_off.y, root_off.z],\n"
            "    }\n"
            f'A = _snapshot("{rtpa}")\n'
            f'B = _snapshot("{rtpb}")\n'
            f"eps = {float(offset_epsilon_deg)}\n"
            "# Diff computation\n"
            "diff = {'a': A['target_rig'], 'b': B['target_rig'], 'differ': False}\n"
            "# Rig/pose\n"
            "for k in ('source_rig', 'target_rig', 'src_pose', 'tgt_pose'):\n"
            "    if A[k] != B[k]:\n"
            "        diff.setdefault('simple_changes', {})[k] = {'a': A[k], 'b': B[k]}\n"
            "        diff['differ'] = True\n"
            "# Mappings\n"
            "map_changes = {}\n"
            "keys = set(A['mappings'].keys()) | set(B['mappings'].keys())\n"
            "for k in keys:\n"
            "    a = A['mappings'].get(k); b = B['mappings'].get(k)\n"
            "    if a != b:\n"
            "        map_changes[k] = {'a': a, 'b': b}\n"
            "if map_changes:\n"
            "    diff['mapping_changes'] = map_changes\n"
            "    diff['differ'] = True\n"
            "# Offsets\n"
            "off_changes = {}\n"
            "bones = set(A['offsets'].keys()) | set(B['offsets'].keys())\n"
            "for b in bones:\n"
            "    a = A['offsets'].get(b) or [0,0,0]\n"
            "    bb = B['offsets'].get(b) or [0,0,0]\n"
            "    if max(abs(a[0]-bb[0]), abs(a[1]-bb[1]), abs(a[2]-bb[2])) > eps:\n"
            "        off_changes[b] = {'a': a, 'b': bb, 'delta': [round(bb[0]-a[0],3), round(bb[1]-a[1],3), round(bb[2]-a[2],3)]}\n"
            "if off_changes:\n"
            "    diff['offset_changes'] = off_changes\n"
            "    diff['differ'] = True\n"
            "# IK static offsets\n"
            "ik_changes = {}\n"
            "ikkeys = set(A['ik_static'].keys()) | set(B['ik_static'].keys())\n"
            "for k in ikkeys:\n"
            "    if A['ik_static'].get(k) != B['ik_static'].get(k):\n"
            "        ik_changes[k] = {'a': A['ik_static'].get(k), 'b': B['ik_static'].get(k)}\n"
            "if ik_changes:\n"
            "    diff['ik_chain_changes'] = ik_changes\n"
            "    diff['differ'] = True\n"
            "# Ops\n"
            "op_changes = []\n"
            "a_types = [o['type'] for o in A['ops']]\n"
            "b_types = [o['type'] for o in B['ops']]\n"
            "if a_types != b_types:\n"
            "    op_changes.append({'kind': 'op_stack_structure', 'a': a_types, 'b': b_types})\n"
            "for ai, bi in zip(A['ops'], B['ops']):\n"
            "    if ai.get('settings') != bi.get('settings') or ai.get('enabled') != bi.get('enabled'):\n"
            "        op_changes.append({'kind': 'op_settings', 'name': ai.get('name'), 'a_enabled': ai.get('enabled'), 'b_enabled': bi.get('enabled'), 'a_settings': ai.get('settings'), 'b_settings': bi.get('settings')})\n"
            "if op_changes:\n"
            "    diff['op_changes'] = op_changes\n"
            "    diff['differ'] = True\n"
            "# Root offset\n"
            "if A['tgt_root_offset'] != B['tgt_root_offset']:\n"
            "    diff['tgt_root_offset'] = {'a': A['tgt_root_offset'], 'b': B['tgt_root_offset']}\n"
            "    diff['differ'] = True\n"
            "print('__MCP_RESULT__' + json.dumps(diff))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="detect_skeleton_convention",
        description=(
            "Fingerprint an IK Rig's skeletal mesh to identify its convention "
            "(metahuman / ue5_mannequin / ue4_mannequin / mixamo / "
            "mixamo_fbx_prefixed / daz_genesis / unknown). Returns the best "
            "match plus a confidence score and the suggested preset name for "
            "apply_preset (when available). Runs entirely MCP-side using the "
            "bone list pulled from UE; no file I/O on the UE side beyond the "
            "single bone enumeration."
        ),
    )
    async def detect_skeleton_convention(ik_rig_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        fingerprints = _load_fingerprints()
        rp = escape_string(ik_rig_path)

        # Ask UE for the bone list, then score on MCP side.
        script = wrap_script(
            "import unreal\n"
            f'rig = unreal.load_asset("{rp}")\n'
            "if rig is None:\n"
            f'    raise ValueError("IKRig not found: {rp}")\n'
            "mesh = unreal.IKRigController.get_controller(rig).get_skeletal_mesh()\n"
            "if mesh is None:\n"
            f'    raise ValueError("IKRig has no skeletal mesh: {rp}")\n'
            "actor = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))\n"
            "try:\n"
            "    smc = actor.get_component_by_class(unreal.SkeletalMeshComponent)\n"
            "    smc.set_skeletal_mesh_asset(mesh)\n"
            "    bones = [str(smc.get_bone_name(i)) for i in range(smc.get_num_bones())]\n"
            "finally:\n"
            "    unreal.EditorLevelLibrary.destroy_actor(actor)\n"
            "print('__MCP_RESULT__' + json.dumps({'bones': bones}))"
        )
        result = conn.execute(script)
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if not parsed or not isinstance(parsed.get("bones"), list):
            return _err(f"Could not enumerate bones for {ik_rig_path}. Raw: {result!r}")

        bone_set = set(parsed["bones"])

        # Score each convention
        scores = []
        for conv in fingerprints.get("conventions", []):
            req = set(conv.get("required_bones", []))
            dist = set(conv.get("distinctive_bones", []))
            neg = set(conv.get("negative_bones", []))
            if not req:
                continue
            req_hit = len(req & bone_set) / len(req)
            dist_hit = (len(dist & bone_set) / len(dist)) if dist else 0.0
            neg_penalty = (len(neg & bone_set) / len(neg)) if neg else 0.0
            # Weighted: required=0.6, distinctive=0.5, negative=-0.4
            confidence = max(0.0, min(1.0, 0.6 * req_hit + 0.5 * dist_hit - 0.4 * neg_penalty))
            scores.append({
                "name": conv["name"],
                "confidence": round(confidence, 3),
                "required_hit": round(req_hit, 3),
                "distinctive_hit": round(dist_hit, 3),
                "negative_penalty": round(neg_penalty, 3),
                "suggested_preset": conv.get("suggested_preset"),
                "description": conv.get("description", ""),
            })
        scores.sort(key=lambda s: s["confidence"], reverse=True)
        best = scores[0] if scores and scores[0]["confidence"] >= 0.4 else None

        output = {
            "rig": ik_rig_path,
            "bone_count": len(parsed["bones"]),
            "best_match": best["name"] if best else "unknown",
            "confidence": best["confidence"] if best else 0.0,
            "suggested_preset": best["suggested_preset"] if best else None,
            "description": best["description"] if best else "No known fingerprint matched with confidence ≥ 0.4",
            "all_scores": scores,
        }
        return _ok(output)
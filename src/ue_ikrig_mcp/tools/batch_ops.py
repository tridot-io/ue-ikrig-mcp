"""Composite op-dispatch tools that collapse N single-call workflows into 1 UE round-trip.

Every retargeter-scoped tool pays the same preamble cost: load the asset, fetch
the controller, send the script over TCP to UE, receive the result. A typical
tuning workflow (adjust-bone x8, set-chain-settings x4, save) chains 12+ of
those round-trips even though they all target one asset.

`batch_retargeter_ops` takes a list of op descriptors and generates ONE script
that shares the asset/controller fetch across every op, executes them in
order, and returns per-op results. `bulk_adjust_bone_rotation` and
`bulk_set_chain_settings` cover the two highest-value bulk patterns whose
single-target siblings exist but whose batch variants didn't.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_FK_ROT_MODES = {"INTERPOLATED", "ONE_TO_ONE", "ONE_TO_ONE_REVERSED"}
_VALID_FK_TRANS_MODES = {"NONE", "COPY_FK_TRANSLATION"}

# Op kinds accepted by batch_retargeter_ops. Kept small on purpose — covers the
# mutations that show up in real tuning loops. Anything more exotic should keep
# using its dedicated tool.
_BATCH_OP_KINDS = {
    "adjust_bone",        # {bone, dp?, dy?, dr?, side?}
    "set_bone_offset_deg", # {bone, roll, pitch, yaw, side?}
    "set_root_offset",     # {x, y, z, side?}
    "set_chain_settings",  # {chain, enable_fk?, rotation_alpha?, translation_alpha?}
    "set_global",          # {scale_horizontal?, scale_vertical?}
    "create_pose",         # {name, side?}
    "set_current_pose",    # {name, side?}
    "save",                # {}
}


def _validate_ops(ops):
    """Light structural validation. UE-side errors per-op are captured, not raised."""
    if not isinstance(ops, list) or not ops:
        return "ops must be a non-empty list of op descriptors"
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            return f"ops[{i}] must be a dict"
        kind = op.get("op")
        if kind not in _BATCH_OP_KINDS:
            return f"ops[{i}] has unknown op kind {kind!r}; valid: {sorted(_BATCH_OP_KINDS)}"
    return None


def register(server):

    @server.tool(
        name="batch_retargeter_ops",
        description=(
            "Run multiple retargeter mutations in ONE UE round-trip. Use this "
            "instead of chaining adjust_bone_rotation / set_chain_settings / "
            "set_root_offset / save_asset calls against the same retargeter. "
            "Each op is a dict with an 'op' key plus fields:\n"
            "  adjust_bone: {op, bone, dp?, dy?, dr?, side?}\n"
            "  set_bone_offset_deg: {op, bone, roll, pitch, yaw, side?}\n"
            "  set_root_offset: {op, x, y, z, side?}\n"
            "  set_chain_settings: {op, chain, enable_fk?, rotation_alpha?, translation_alpha?}\n"
            "  set_global: {op, scale_horizontal?, scale_vertical?}\n"
            "  create_pose: {op, name, side?}\n"
            "  set_current_pose: {op, name, side?}\n"
            "  save: {op}\n"
            "side defaults to 'Target'. Per-op failures are captured and don't "
            "abort the batch. Returns {count, ok_count, err_count, results}."
        ),
    )
    async def batch_retargeter_ops(
        retargeter_path: str,
        ops: list,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        err = _validate_ops(ops)
        if err:
            return _err(err)

        rtp = escape_string(retargeter_path)
        ops_json = json.dumps(ops)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "if ctrl is None:\n"
            f'    raise ValueError("Could not get controller for: {rtp}")\n'
            "SRC = unreal.RetargetSourceOrTarget.SOURCE\n"
            "TGT = unreal.RetargetSourceOrTarget.TARGET\n"
            "def _side(s):\n"
            "    return SRC if str(s or 'Target').lower() == 'source' else TGT\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            f"_OPS = {ops_json}\n"
            "results = []\n"
            "ok_count = 0\n"
            "err_count = 0\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            "for _i, _op in enumerate(_OPS):\n"
            "    _kind = _op.get('op')\n"
            "    _r = {'i': _i, 'op': _kind}\n"
            "    try:\n"
            "        if _kind == 'adjust_bone':\n"
            "            _sot = _side(_op.get('side'))\n"
            "            _bn = str(_op['bone'])\n"
            "            _dp = float(_op.get('dp', 0))\n"
            "            _dy = float(_op.get('dy', 0))\n"
            "            _dr = float(_op.get('dr', 0))\n"
            "            _cq = ctrl.get_rotation_offset_for_retarget_pose_bone(_bn, _sot)\n"
            "            _cr = _cq.rotator()\n"
            "            _nr = unreal.Rotator(_cr.pitch + _dp, _cr.yaw + _dy, _cr.roll + _dr)\n"
            "            _nq = _nr.quaternion()\n"
            "            ctrl.set_rotation_offset_for_retarget_pose_bone(_bn, _nq, _sot)\n"
            "            _r['bone'] = _bn\n"
            "            _r['after_deg'] = _deg(_nq)\n"
            "        elif _kind == 'set_bone_offset_deg':\n"
            "            _sot = _side(_op.get('side'))\n"
            "            _bn = str(_op['bone'])\n"
            "            _roll = float(_op.get('roll', 0))\n"
            "            _pitch = float(_op.get('pitch', 0))\n"
            "            _yaw = float(_op.get('yaw', 0))\n"
            "            _before = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(_bn, _sot))\n"
            "            _q = unreal.Rotator(roll=_roll, pitch=_pitch, yaw=_yaw).quaternion()\n"
            "            ctrl.set_rotation_offset_for_retarget_pose_bone(_bn, _q, _sot)\n"
            "            _r['bone'] = _bn\n"
            "            _r['before_deg'] = _before\n"
            "            _r['after_deg'] = _deg(_q)\n"
            "        elif _kind == 'set_root_offset':\n"
            "            _sot = _side(_op.get('side'))\n"
            "            _v = unreal.Vector(float(_op.get('x', 0)), float(_op.get('y', 0)), float(_op.get('z', 0)))\n"
            "            ctrl.set_root_offset_in_retarget_pose(_v, _sot)\n"
            "            _r['offset'] = [_v.x, _v.y, _v.z]\n"
            "        elif _kind == 'set_chain_settings':\n"
            "            _cn = str(_op['chain'])\n"
            "            _cs = ctrl.get_retarget_chain_settings(_cn)\n"
            "            if _cs is None:\n"
            "                raise ValueError('Chain not found: ' + _cn)\n"
            "            if 'enable_fk' in _op:\n"
            "                _cs.fk.enable_fk = bool(_op['enable_fk'])\n"
            "            if 'rotation_alpha' in _op:\n"
            "                _cs.fk.rotation_alpha = float(_op['rotation_alpha'])\n"
            "            if 'translation_alpha' in _op:\n"
            "                _cs.fk.translation_alpha = float(_op['translation_alpha'])\n"
            "            ctrl.set_retarget_chain_settings(_cn, _cs)\n"
            "            _r['chain'] = _cn\n"
            "        elif _kind == 'set_global':\n"
            "            _gs = ctrl.get_global_settings()\n"
            "            if 'scale_horizontal' in _op:\n"
            "                _gs.scale_horizontal = float(_op['scale_horizontal'])\n"
            "            if 'scale_vertical' in _op:\n"
            "                _gs.scale_vertical = float(_op['scale_vertical'])\n"
            "            ctrl.set_global_settings(_gs)\n"
            "            _r['scale_horizontal'] = float(_gs.scale_horizontal)\n"
            "            _r['scale_vertical'] = float(_gs.scale_vertical)\n"
            "        elif _kind == 'create_pose':\n"
            "            _sot = _side(_op.get('side'))\n"
            "            _pn = str(_op['name'])\n"
            "            ctrl.create_retarget_pose(_pn, _sot)\n"
            "            _r['pose'] = _pn\n"
            "        elif _kind == 'set_current_pose':\n"
            "            _sot = _side(_op.get('side'))\n"
            "            _pn = str(_op['name'])\n"
            "            ctrl.set_current_retarget_pose(_pn, _sot)\n"
            "            _r['pose'] = _pn\n"
            "        elif _kind == 'save':\n"
            f'            _saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            "            _r['saved'] = _saved\n"
            "        else:\n"
            "            raise ValueError('Unknown op kind: ' + str(_kind))\n"
            "        _r['ok'] = True\n"
            "        ok_count += 1\n"
            "    except Exception as _e:\n"
            "        _r['ok'] = False\n"
            "        _r['error'] = str(_e)\n"
            "        err_count += 1\n"
            "    results.append(_r)\n"
            "print('__MCP_RESULT__' + json.dumps({\n"
            "    'count': len(_OPS),\n"
            "    'ok_count': ok_count,\n"
            "    'err_count': err_count,\n"
            "    'results': results,\n"
            "}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="bulk_adjust_bone_rotation",
        description=(
            "Apply euler deltas (pitch/yaw/roll in degrees) to many bones in ONE "
            "round-trip. deltas maps bone_name -> [dp, dy, dr]. This is the "
            "delta counterpart of batch_set_bone_rotation_offset (which sets "
            "absolute euler). source_or_target: 'Source' | 'Target' (default "
            "'Target'). Per-bone errors are captured; the batch continues."
        ),
    )
    async def bulk_adjust_bone_rotation(
        retargeter_path: str,
        deltas: dict,
        source_or_target: str = "Target",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not isinstance(deltas, dict) or not deltas:
            return _err("deltas must be a non-empty dict of {bone_name: [dp, dy, dr]}")

        clean = {}
        for bone, dpyr in deltas.items():
            if not (isinstance(dpyr, (list, tuple)) and len(dpyr) == 3):
                return _err(f"Bone {bone!r} delta must be [dp, dy, dr] (3 floats), got {dpyr!r}")
            try:
                clean[str(bone)] = [float(dpyr[0]), float(dpyr[1]), float(dpyr[2])]
            except (TypeError, ValueError) as e:
                return _err(f"Bone {bone!r} delta contains non-numeric values: {e}")

        rtp = escape_string(retargeter_path)
        side = "TARGET" if source_or_target.lower() == "target" else "SOURCE"
        deltas_json = json.dumps(clean)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"SIDE = unreal.RetargetSourceOrTarget.{side}\n"
            f"_deltas = {deltas_json}\n"
            "def _deg(q):\n"
            "    r = q.rotator()\n"
            "    return [round(r.roll, 3), round(r.pitch, 3), round(r.yaw, 3)]\n"
            "applied = []\n"
            "for bone, d in _deltas.items():\n"
            "    try:\n"
            "        cq = ctrl.get_rotation_offset_for_retarget_pose_bone(bone, SIDE)\n"
            "        before = _deg(cq)\n"
            "        cr = cq.rotator()\n"
            "        nr = unreal.Rotator(cr.pitch + d[0], cr.yaw + d[1], cr.roll + d[2])\n"
            "        nq = nr.quaternion()\n"
            "        ctrl.set_rotation_offset_for_retarget_pose_bone(bone, nq, SIDE)\n"
            "        applied.append({'bone': bone, 'delta_pyr': d, 'before_deg': before, 'after_deg': _deg(nq)})\n"
            "    except Exception as _e:\n"
            "        applied.append({'bone': bone, 'error': str(_e)})\n"
            "print('__MCP_RESULT__' + json.dumps({'count': len(_deltas), 'applied': applied}))"
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="bulk_set_chain_settings",
        description=(
            "Apply FK/IK retarget settings to multiple chains in ONE round-trip. "
            "settings maps target_chain_name -> {enable_fk?, rotation_alpha?, "
            "translation_alpha?}. Only provided fields per chain are modified. "
            "Per-chain errors are captured; the batch continues."
        ),
    )
    async def bulk_set_chain_settings(
        retargeter_path: str,
        settings: dict,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not isinstance(settings, dict) or not settings:
            return _err("settings must be a non-empty dict of {chain: {enable_fk?, rotation_alpha?, translation_alpha?}}")

        allowed = {"enable_fk", "rotation_alpha", "translation_alpha"}
        clean = {}
        for chain, cfg in settings.items():
            if not isinstance(cfg, dict):
                return _err(f"settings[{chain!r}] must be a dict; got {cfg!r}")
            extra = set(cfg) - allowed
            if extra:
                return _err(f"settings[{chain!r}] has unknown fields {sorted(extra)}; allowed: {sorted(allowed)}")
            norm = {}
            if "enable_fk" in cfg:
                norm["enable_fk"] = bool(cfg["enable_fk"])
            if "rotation_alpha" in cfg:
                norm["rotation_alpha"] = float(cfg["rotation_alpha"])
            if "translation_alpha" in cfg:
                norm["translation_alpha"] = float(cfg["translation_alpha"])
            clean[str(chain)] = norm

        rtp = escape_string(retargeter_path)
        cfg_json = json.dumps(clean)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"_cfg = {cfg_json}\n"
            "applied = []\n"
            "for chain, c in _cfg.items():\n"
            "    try:\n"
            "        cs = ctrl.get_retarget_chain_settings(chain)\n"
            "        if cs is None:\n"
            "            raise ValueError('Chain not found: ' + chain)\n"
            "        if 'enable_fk' in c:\n"
            "            cs.fk.enable_fk = bool(c['enable_fk'])\n"
            "        if 'rotation_alpha' in c:\n"
            "            cs.fk.rotation_alpha = float(c['rotation_alpha'])\n"
            "        if 'translation_alpha' in c:\n"
            "            cs.fk.translation_alpha = float(c['translation_alpha'])\n"
            "        ctrl.set_retarget_chain_settings(chain, cs)\n"
            "        applied.append({'chain': chain, 'ok': True, 'applied': list(c.keys())})\n"
            "    except Exception as _e:\n"
            "        applied.append({'chain': chain, 'ok': False, 'error': str(_e)})\n"
            "print('__MCP_RESULT__' + json.dumps({'count': len(_cfg), 'applied': applied}))"
        )
        result = conn.execute(script)
        return _ok(result)

"""Retargeter configuration I/O: export, import, and presets.

Save a whole retargeter configuration as JSON (chain mappings, all named
retarget poses with per-bone offsets, per-chain IK settings, op stack
settings). Import that JSON back onto a fresh retargeter to reconstruct
the state. Ship presets for common skeleton-to-skeleton pairings so 90%
of a new retargeter's manual setup is replaced by a single tool call.

Schema version 1 (see data/schema/retargeter_config_v1.json). Presets live
under data/presets/ and are discoverable via list_available_presets.

Design notes:
- Op settings are stored as T3D export text (what export_text() produces)
  and restored via import_text() on a fresh op instance. This round-trips
  cleanly in UE 5.6.
- Asset references (source/target rig paths) are present in a full export
  but are NOT part of shipped presets — presets are skeleton-convention
  level, not asset-specific, so the same preset works for any Mixamo-like
  source targeting any MetaHuman-like rig.
- Mappings and bone names are named keys, so they work across any rig that
  follows the convention even if bone indices differ.
- import supports selective restore via apply=['mappings', 'poses',
  'ik_chain_settings', 'ops'] so callers can layer presets on top of
  existing tuning without clobbering everything.
"""

import datetime
import json
from pathlib import Path

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


SCHEMA_VERSION = 1


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def _presets_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "presets"


_EXPORT_SCRIPT = r"""
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
    return [round(r.roll, 4), round(r.pitch, 4), round(r.yaw, 4)]

src_rig = ctrl.get_ik_rig(SRC)
tgt_rig = ctrl.get_ik_rig(TGT)
src_rig_ctrl = unreal.IKRigController.get_controller(src_rig) if src_rig else None
tgt_rig_ctrl = unreal.IKRigController.get_controller(tgt_rig) if tgt_rig else None

# Chain mappings on the target side
target_chains = [str(ch.chain_name) for ch in tgt_rig_ctrl.get_retarget_chains()] if tgt_rig_ctrl else []
mappings = {{}}
for tc in target_chains:
    sc = str(ctrl.get_source_chain(tc))
    mappings[tc] = sc if (sc and sc != "None") else None

# Target skeleton bone list (for enumerating retarget pose offsets)
def _bones_of(rig_ctrl_local):
    if not rig_ctrl_local: return []
    m = rig_ctrl_local.get_skeletal_mesh()
    if not m: return []
    a = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0,0,0), unreal.Rotator(0,0,0))
    try:
        smc = a.get_component_by_class(unreal.SkeletalMeshComponent)
        smc.set_skeletal_mesh_asset(m)
        return [str(smc.get_bone_name(i)) for i in range(smc.get_num_bones())]
    finally:
        unreal.EditorLevelLibrary.destroy_actor(a)

tgt_bones = _bones_of(tgt_rig_ctrl)
src_bones = _bones_of(src_rig_ctrl)

# All retarget poses on the target side with per-bone offsets
def _enum_poses(side, bones):
    poses_map = ctrl.get_retarget_poses(side)
    out = {{}}
    was_current = str(ctrl.get_current_retarget_pose_name(side))
    try:
        for pname in list(poses_map.keys()):
            ctrl.set_current_retarget_pose(pname, side)
            root_off = ctrl.get_root_offset_in_retarget_pose(side)
            bone_offs = {{}}
            for b in bones:
                try:
                    d = _deg(ctrl.get_rotation_offset_for_retarget_pose_bone(b, side))
                    # Only record non-identity offsets to keep JSON small
                    if d != [0.0, 0.0, 0.0] and d != [-0.0, -0.0, -0.0] and not (abs(d[0]) < 1e-4 and abs(d[1]) < 1e-4 and abs(d[2]) < 1e-4):
                        bone_offs[b] = d
                except Exception:
                    pass
            out[str(pname)] = {{
                "root_offset": [root_off.x, root_off.y, root_off.z],
                "bone_offsets_deg": bone_offs,
            }}
    finally:
        ctrl.set_current_retarget_pose(unreal.Name(was_current), side)
    return out, was_current

tgt_poses, tgt_current_pose = _enum_poses(TGT, tgt_bones) if tgt_rig_ctrl else ({{}}, "")
src_poses, src_current_pose = _enum_poses(SRC, src_bones) if src_rig_ctrl else ({{}}, "")

# Op stack serialization — type name + export_text of settings + enabled
ops = []
for i in range(ctrl.get_num_retarget_ops()):
    oc = ctrl.get_op_controller(i)
    s = oc.get_settings() if hasattr(oc, 'get_settings') else None
    # Infer op class name from controller class name: strip 'Controller'
    controller_type = type(oc).__name__
    op_type = controller_type.replace("Controller", "Op") if controller_type.endswith("Controller") else controller_type
    ops.append({{
        "name": str(ctrl.get_op_name(i)),
        "op_type": op_type,
        "controller_type": controller_type,
        "enabled": bool(ctrl.get_retarget_op_enabled(i)),
        "settings_t3d": s.export_text() if s else "",
    }})

# IK chain settings (per-chain static offset + blend)
ik_chain_settings = {{}}
for i in range(ctrl.get_num_retarget_ops()):
    oc = ctrl.get_op_controller(i)
    if type(oc).__name__ == "IKRetargetIKChainsController":
        s = oc.get_settings()
        arr = s.get_editor_property("chains_to_retarget") if s else []
        for ch in (arr or []):
            tcn = str(ch.get_editor_property("target_chain_name"))
            so = ch.get_editor_property("static_offset")
            slo = ch.get_editor_property("static_local_offset")
            sro = ch.get_editor_property("static_rotation_offset")
            ik_chain_settings[tcn] = {{
                "enable_ik": bool(ch.get_editor_property("enable_ik")),
                "blend_to_source": float(ch.get_editor_property("blend_to_source")),
                "blend_to_source_translation": float(ch.get_editor_property("blend_to_source_translation")),
                "blend_to_source_rotation": float(ch.get_editor_property("blend_to_source_rotation")),
                "static_offset": [so.x, so.y, so.z],
                "static_local_offset": [slo.x, slo.y, slo.z],
                "static_rotation_offset": [sro.pitch, sro.yaw, sro.roll],
                "scale_vertical": float(ch.get_editor_property("scale_vertical")),
                "extension": float(ch.get_editor_property("extension")),
            }}
        break

config = {{
    "source": {{
        "rig_path": src_rig.get_path_name() if src_rig else None,
        "current_pose": src_current_pose,
        "retarget_poses": src_poses,
    }},
    "target": {{
        "rig_path": tgt_rig.get_path_name() if tgt_rig else None,
        "current_pose": tgt_current_pose,
        "retarget_poses": tgt_poses,
    }},
    "chain_mappings": mappings,
    "ops": ops,
    "ik_chain_settings": ik_chain_settings,
}}
print("__MCP_RESULT__" + _json.dumps(config))
"""


def register(server):
    @server.tool(
        name="export_retargeter_config",
        description=(
            "Export a full IK Retargeter configuration as JSON. Captures: "
            "source/target rig paths, all named retarget poses on both sides "
            "(with root offsets and per-bone rotation offsets in degrees — "
            "only non-identity offsets are recorded to keep the JSON compact), "
            "chain mappings (target -> source), all ops with type + enabled "
            "state + T3D-serialized settings, and per-chain IK settings "
            "(StaticOffset, BlendToSource, EnableIK, ScaleVertical, ...). "
            "If save_as_preset is provided, the export is also written to "
            "the shipped presets directory as <save_as_preset>.json so it "
            "appears in list_available_presets and apply_preset."
        ),
    )
    async def export_retargeter_config(
        retargeter_path: str,
        save_as_preset: str = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        script = wrap_script(_EXPORT_SCRIPT.format(rtp=rtp))
        result = conn.execute(script)
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if not parsed:
            return _err(f"Export returned no parsed result. Raw: {result!r}")

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        full_config = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": now,
            "exported_from": retargeter_path,
            **parsed,
        }

        saved_to = None
        if save_as_preset:
            name = str(save_as_preset).strip()
            if not name:
                return _err("save_as_preset must be non-empty when provided")
            # Sanitize filename
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
            if not safe.endswith(".json"):
                safe = safe + ".json"
            preset_path = _presets_dir() / safe
            preset_path.parent.mkdir(parents=True, exist_ok=True)
            # Drop asset-specific rig_path fields when saving as preset
            preset_config = dict(full_config)
            preset_config["is_preset"] = True
            preset_config["source"] = dict(full_config["source"])
            preset_config["target"] = dict(full_config["target"])
            preset_config["source"]["rig_path"] = None
            preset_config["target"]["rig_path"] = None
            preset_path.write_text(json.dumps(preset_config, indent=2), encoding="utf-8")
            saved_to = str(preset_path)

        output = {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "config": full_config,
            "saved_as_preset": saved_to,
        }
        return _ok(output)

    @server.tool(
        name="import_retargeter_config",
        description=(
            "Apply a previously-exported retargeter config (or preset) to a "
            "retargeter. apply is an optional list selecting which sections "
            "to restore: 'mappings' (chain source->target), 'poses' "
            "(retarget poses with per-bone offsets and root offset), "
            "'ik_chain_settings' (per-chain IK StaticOffset etc.), 'ops' "
            "(op stack structure + settings — REPLACES current ops). "
            "Default: ['mappings', 'poses', 'ik_chain_settings']. Asset "
            "path fields in the config are ignored — callers supply the "
            "target retargeter. Schema-version mismatch raises an error."
        ),
    )
    async def import_retargeter_config(
        retargeter_path: str,
        config: dict,
        apply: list = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not isinstance(config, dict):
            return _err("config must be a dict (the JSON object returned by export_retargeter_config)")

        sv = config.get("schema_version")
        if sv != SCHEMA_VERSION:
            return _err(f"Schema mismatch: config is v{sv}, this MCP supports v{SCHEMA_VERSION}")

        sections = apply if apply else ["mappings", "poses", "ik_chain_settings"]
        valid = {"mappings", "poses", "ik_chain_settings", "ops"}
        unknown = [s for s in sections if s not in valid]
        if unknown:
            return _err(f"Unknown apply sections: {unknown}. Valid: {sorted(valid)}")

        rtp = escape_string(retargeter_path)
        payload = json.dumps({
            "sections": list(sections),
            "config": config,
        })

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "SRC = unreal.RetargetSourceOrTarget.SOURCE\n"
            "TGT = unreal.RetargetSourceOrTarget.TARGET\n"
            f"_payload = {payload}\n"
            "sections = set(_payload['sections'])\n"
            "cfg = _payload['config']\n"
            "report = {'applied_sections': [], 'warnings': []}\n"
            # -------- mappings --------
            "if 'mappings' in sections and cfg.get('chain_mappings'):\n"
            "    applied = 0\n"
            "    skipped = 0\n"
            "    for target_chain, source_chain in cfg['chain_mappings'].items():\n"
            "        try:\n"
            "            if source_chain is None or source_chain == 'None':\n"
            "                ctrl.set_source_chain('', target_chain)\n"
            "            else:\n"
            "                ctrl.set_source_chain(source_chain, target_chain)\n"
            "            applied += 1\n"
            "        except Exception as _e:\n"
            "            report['warnings'].append('mapping %s->%s failed: %s' % (source_chain, target_chain, _e))\n"
            "            skipped += 1\n"
            "    report['applied_sections'].append({'mappings': {'applied': applied, 'skipped': skipped}})\n"
            # -------- poses --------
            "if 'poses' in sections:\n"
            "    for side_key, side_enum in [('target', TGT), ('source', SRC)]:\n"
            "        side_cfg = cfg.get(side_key, {})\n"
            "        poses = side_cfg.get('retarget_poses', {})\n"
            "        applied_poses = 0\n"
            "        for pname, pdata in poses.items():\n"
            "            try:\n"
            "                existing = [str(n) for n in ctrl.get_retarget_poses(side_enum).keys()]\n"
            "                if pname not in existing:\n"
            "                    ctrl.create_retarget_pose(unreal.Name(pname), side_enum)\n"
            "                ctrl.set_current_retarget_pose(unreal.Name(pname), side_enum)\n"
            "                ro = pdata.get('root_offset') or [0,0,0]\n"
            "                ctrl.set_root_offset_in_retarget_pose(unreal.Vector(ro[0], ro[1], ro[2]), side_enum)\n"
            "                for bone, deg3 in (pdata.get('bone_offsets_deg') or {}).items():\n"
            "                    q = unreal.Rotator(roll=deg3[0], pitch=deg3[1], yaw=deg3[2]).quaternion()\n"
            "                    try:\n"
            "                        ctrl.set_rotation_offset_for_retarget_pose_bone(bone, q, side_enum)\n"
            "                    except Exception as _be:\n"
            "                        report['warnings'].append('bone %s on %s: %s' % (bone, side_key, _be))\n"
            "                applied_poses += 1\n"
            "            except Exception as _pe:\n"
            "                report['warnings'].append('pose %s on %s failed: %s' % (pname, side_key, _pe))\n"
            "        cur = side_cfg.get('current_pose')\n"
            "        if cur:\n"
            "            try:\n"
            "                ctrl.set_current_retarget_pose(unreal.Name(cur), side_enum)\n"
            "            except Exception as _ce:\n"
            "                report['warnings'].append('set_current %s on %s: %s' % (cur, side_key, _ce))\n"
            "        report['applied_sections'].append({'poses_%s' % side_key: applied_poses})\n"
            # -------- ik_chain_settings --------
            "if 'ik_chain_settings' in sections and cfg.get('ik_chain_settings'):\n"
            "    ik_idx = -1\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ == 'IKRetargetIKChainsController':\n"
            "            ik_idx = i; break\n"
            "    if ik_idx < 0:\n"
            "        report['warnings'].append('no IK Chains op on target retargeter; skipping ik_chain_settings')\n"
            "    else:\n"
            "        oc = ctrl.get_op_controller(ik_idx)\n"
            "        settings = oc.get_settings()\n"
            "        arr = settings.get_editor_property('chains_to_retarget') or []\n"
            "        new_arr = []\n"
            "        applied_ik = 0\n"
            "        for ch in arr:\n"
            "            tcn = str(ch.get_editor_property('target_chain_name'))\n"
            "            cfg_ch = cfg['ik_chain_settings'].get(tcn)\n"
            "            if cfg_ch:\n"
            "                try:\n"
            "                    ch.set_editor_property('enable_ik', bool(cfg_ch.get('enable_ik', True)))\n"
            "                    ch.set_editor_property('blend_to_source', float(cfg_ch.get('blend_to_source', 0.0)))\n"
            "                    ch.set_editor_property('blend_to_source_translation', float(cfg_ch.get('blend_to_source_translation', 1.0)))\n"
            "                    ch.set_editor_property('blend_to_source_rotation', float(cfg_ch.get('blend_to_source_rotation', 0.0)))\n"
            "                    so = cfg_ch.get('static_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_offset', unreal.Vector(so[0], so[1], so[2]))\n"
            "                    slo = cfg_ch.get('static_local_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_local_offset', unreal.Vector(slo[0], slo[1], slo[2]))\n"
            "                    sro = cfg_ch.get('static_rotation_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_rotation_offset', unreal.Rotator(pitch=sro[0], yaw=sro[1], roll=sro[2]))\n"
            "                    ch.set_editor_property('scale_vertical', float(cfg_ch.get('scale_vertical', 1.0)))\n"
            "                    ch.set_editor_property('extension', float(cfg_ch.get('extension', 1.0)))\n"
            "                    applied_ik += 1\n"
            "                except Exception as _ie:\n"
            "                    report['warnings'].append('ik chain %s: %s' % (tcn, _ie))\n"
            "            new_arr.append(ch)\n"
            "        settings.set_editor_property('chains_to_retarget', new_arr)\n"
            "        oc.set_settings(settings)\n"
            "        report['applied_sections'].append({'ik_chain_settings': applied_ik})\n"
            # -------- ops --------
            "if 'ops' in sections and cfg.get('ops'):\n"
            "    # Wipe and rebuild the op stack. Existing per-chain IK offsets survive\n"
            "    # only if ik_chain_settings is also applied after this.\n"
            "    ctrl.remove_all_ops()\n"
            "    applied_ops = 0\n"
            "    for op_info in cfg['ops']:\n"
            "        op_type_name = op_info.get('op_type', '')\n"
            "        op_class = getattr(unreal, op_type_name, None)\n"
            "        if op_class is None:\n"
            "            report['warnings'].append('unknown op type: ' + op_type_name)\n"
            "            continue\n"
            "        try:\n"
            "            new_idx = ctrl.add_retarget_op(op_class)\n"
            "            if op_info.get('name'):\n"
            "                try:\n"
            "                    ctrl.set_op_name(new_idx, unreal.Name(op_info['name']))\n"
            "                except Exception:\n"
            "                    pass\n"
            "            ctrl.set_retarget_op_enabled(new_idx, bool(op_info.get('enabled', True)))\n"
            "            t3d = op_info.get('settings_t3d', '')\n"
            "            if t3d:\n"
            "                oc = ctrl.get_op_controller(new_idx)\n"
            "                s = oc.get_settings()\n"
            "                try:\n"
            "                    s.import_text(t3d)\n"
            "                    oc.set_settings(s)\n"
            "                except Exception as _te:\n"
            "                    report['warnings'].append('op %s import_text: %s' % (op_type_name, _te))\n"
            "            applied_ops += 1\n"
            "        except Exception as _oe:\n"
            "            report['warnings'].append('add_retarget_op %s: %s' % (op_type_name, _oe))\n"
            "    report['applied_sections'].append({'ops': applied_ops})\n"
            "print('__MCP_RESULT__' + json.dumps(report))"
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="list_available_presets",
        description=(
            "List retargeter presets shipped with this MCP plus any user-"
            "saved presets in the data/presets directory. Returns each "
            "preset's filename, description (read from the preset's 'note' "
            "field if present), schema version, and the rig conventions it "
            "was exported from (if captured)."
        ),
    )
    async def list_available_presets() -> list[TextContent]:
        dirp = _presets_dir()
        presets = []
        if dirp.exists():
            for p in sorted(dirp.glob("*.json")):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    presets.append({
                        "name": p.stem,
                        "path": str(p),
                        "schema_version": data.get("schema_version"),
                        "exported_at": data.get("exported_at"),
                        "exported_from": data.get("exported_from"),
                        "note": data.get("note"),
                        "source_convention_hint": (data.get("source") or {}).get("convention"),
                        "target_convention_hint": (data.get("target") or {}).get("convention"),
                        "mapping_count": len(data.get("chain_mappings") or {}),
                        "is_preset": bool(data.get("is_preset", False)),
                    })
                except Exception as e:
                    presets.append({"name": p.stem, "path": str(p), "error": f"parse failed: {e}"})
        return _ok({"presets_dir": str(dirp), "presets": presets, "count": len(presets)})

    @server.tool(
        name="apply_preset",
        description=(
            "Apply a named preset to a retargeter. The preset is looked up "
            "by filename stem (without .json) in the shipped "
            "data/presets directory. Internally calls "
            "import_retargeter_config with the preset JSON. apply_sections "
            "is forwarded to import_retargeter_config to control which "
            "sections are restored (default: mappings + poses + "
            "ik_chain_settings; 'ops' is opt-in since it replaces the "
            "whole op stack)."
        ),
    )
    async def apply_preset(
        retargeter_path: str,
        preset_name: str,
        apply_sections: list = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not preset_name:
            return _err("preset_name is required")

        # Resolve preset name → file
        dirp = _presets_dir()
        safe = preset_name
        if safe.endswith(".json"):
            safe = safe[:-5]
        candidates = [dirp / f"{safe}.json", dirp / preset_name]
        preset_path = next((c for c in candidates if c.exists()), None)
        if preset_path is None:
            available = []
            if dirp.exists():
                available = [p.stem for p in sorted(dirp.glob("*.json"))]
            return _err(
                f"Preset {preset_name!r} not found in {dirp}. Available: {available}"
            )
        try:
            config = json.loads(preset_path.read_text(encoding="utf-8"))
        except Exception as e:
            return _err(f"Failed to parse preset {preset_path}: {e}")

        sv = config.get("schema_version")
        if sv != SCHEMA_VERSION:
            return _err(f"Preset {preset_name} is schema v{sv}; this MCP supports v{SCHEMA_VERSION}")

        sections = apply_sections if apply_sections else ["mappings", "poses", "ik_chain_settings"]
        valid = {"mappings", "poses", "ik_chain_settings", "ops"}
        unknown = [s for s in sections if s not in valid]
        if unknown:
            return _err(f"Unknown apply sections: {unknown}. Valid: {sorted(valid)}")

        rtp = escape_string(retargeter_path)
        payload = json.dumps({
            "sections": list(sections),
            "config": config,
        })

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "SRC = unreal.RetargetSourceOrTarget.SOURCE\n"
            "TGT = unreal.RetargetSourceOrTarget.TARGET\n"
            f"_payload = {payload}\n"
            "sections = set(_payload['sections'])\n"
            "cfg = _payload['config']\n"
            "report = {'applied_sections': [], 'warnings': [], 'preset_name': cfg.get('exported_from') or cfg.get('note')}\n"
            "if 'mappings' in sections and cfg.get('chain_mappings'):\n"
            "    applied = 0\n"
            "    for target_chain, source_chain in cfg['chain_mappings'].items():\n"
            "        try:\n"
            "            if source_chain is None or source_chain == 'None':\n"
            "                ctrl.set_source_chain('', target_chain)\n"
            "            else:\n"
            "                ctrl.set_source_chain(source_chain, target_chain)\n"
            "            applied += 1\n"
            "        except Exception as _e:\n"
            "            report['warnings'].append('mapping %s->%s: %s' % (source_chain, target_chain, _e))\n"
            "    report['applied_sections'].append({'mappings': applied})\n"
            "if 'poses' in sections:\n"
            "    for side_key, side_enum in [('target', TGT), ('source', SRC)]:\n"
            "        poses = (cfg.get(side_key) or {}).get('retarget_poses', {})\n"
            "        applied_poses = 0\n"
            "        for pname, pdata in poses.items():\n"
            "            try:\n"
            "                existing = [str(n) for n in ctrl.get_retarget_poses(side_enum).keys()]\n"
            "                if pname not in existing:\n"
            "                    ctrl.create_retarget_pose(unreal.Name(pname), side_enum)\n"
            "                ctrl.set_current_retarget_pose(unreal.Name(pname), side_enum)\n"
            "                ro = pdata.get('root_offset') or [0,0,0]\n"
            "                ctrl.set_root_offset_in_retarget_pose(unreal.Vector(ro[0], ro[1], ro[2]), side_enum)\n"
            "                for bone, deg3 in (pdata.get('bone_offsets_deg') or {}).items():\n"
            "                    q = unreal.Rotator(roll=deg3[0], pitch=deg3[1], yaw=deg3[2]).quaternion()\n"
            "                    try:\n"
            "                        ctrl.set_rotation_offset_for_retarget_pose_bone(bone, q, side_enum)\n"
            "                    except Exception as _be:\n"
            "                        report['warnings'].append('%s on %s: %s' % (bone, side_key, _be))\n"
            "                applied_poses += 1\n"
            "            except Exception as _pe:\n"
            "                report['warnings'].append('pose %s on %s: %s' % (pname, side_key, _pe))\n"
            "        cur = (cfg.get(side_key) or {}).get('current_pose')\n"
            "        if cur:\n"
            "            try:\n"
            "                ctrl.set_current_retarget_pose(unreal.Name(cur), side_enum)\n"
            "            except Exception:\n"
            "                pass\n"
            "        report['applied_sections'].append({'poses_%s' % side_key: applied_poses})\n"
            "if 'ik_chain_settings' in sections and cfg.get('ik_chain_settings'):\n"
            "    ik_idx = -1\n"
            "    for i in range(ctrl.get_num_retarget_ops()):\n"
            "        oc = ctrl.get_op_controller(i)\n"
            "        if type(oc).__name__ == 'IKRetargetIKChainsController':\n"
            "            ik_idx = i; break\n"
            "    if ik_idx < 0:\n"
            "        report['warnings'].append('no IK Chains op; skipping ik_chain_settings')\n"
            "    else:\n"
            "        oc = ctrl.get_op_controller(ik_idx)\n"
            "        settings = oc.get_settings()\n"
            "        arr = settings.get_editor_property('chains_to_retarget') or []\n"
            "        applied_ik = 0\n"
            "        for ch in arr:\n"
            "            tcn = str(ch.get_editor_property('target_chain_name'))\n"
            "            cfg_ch = cfg['ik_chain_settings'].get(tcn)\n"
            "            if cfg_ch:\n"
            "                try:\n"
            "                    ch.set_editor_property('enable_ik', bool(cfg_ch.get('enable_ik', True)))\n"
            "                    ch.set_editor_property('blend_to_source', float(cfg_ch.get('blend_to_source', 0.0)))\n"
            "                    ch.set_editor_property('blend_to_source_translation', float(cfg_ch.get('blend_to_source_translation', 1.0)))\n"
            "                    ch.set_editor_property('blend_to_source_rotation', float(cfg_ch.get('blend_to_source_rotation', 0.0)))\n"
            "                    so = cfg_ch.get('static_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_offset', unreal.Vector(so[0], so[1], so[2]))\n"
            "                    slo = cfg_ch.get('static_local_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_local_offset', unreal.Vector(slo[0], slo[1], slo[2]))\n"
            "                    sro = cfg_ch.get('static_rotation_offset') or [0,0,0]\n"
            "                    ch.set_editor_property('static_rotation_offset', unreal.Rotator(pitch=sro[0], yaw=sro[1], roll=sro[2]))\n"
            "                    ch.set_editor_property('scale_vertical', float(cfg_ch.get('scale_vertical', 1.0)))\n"
            "                    ch.set_editor_property('extension', float(cfg_ch.get('extension', 1.0)))\n"
            "                    applied_ik += 1\n"
            "                except Exception as _ie:\n"
            "                    report['warnings'].append('ik %s: %s' % (tcn, _ie))\n"
            "        settings.set_editor_property('chains_to_retarget', arr)\n"
            "        oc.set_settings(settings)\n"
            "        report['applied_sections'].append({'ik_chain_settings': applied_ik})\n"
            "if 'ops' in sections and cfg.get('ops'):\n"
            "    ctrl.remove_all_ops()\n"
            "    applied_ops = 0\n"
            "    for op_info in cfg['ops']:\n"
            "        op_class = getattr(unreal, op_info.get('op_type', ''), None)\n"
            "        if op_class is None:\n"
            "            report['warnings'].append('unknown op type: ' + op_info.get('op_type', ''))\n"
            "            continue\n"
            "        new_idx = ctrl.add_retarget_op(op_class)\n"
            "        if op_info.get('name'):\n"
            "            try:\n"
            "                ctrl.set_op_name(new_idx, unreal.Name(op_info['name']))\n"
            "            except Exception:\n"
            "                pass\n"
            "        ctrl.set_retarget_op_enabled(new_idx, bool(op_info.get('enabled', True)))\n"
            "        t3d = op_info.get('settings_t3d', '')\n"
            "        if t3d:\n"
            "            oc = ctrl.get_op_controller(new_idx)\n"
            "            s = oc.get_settings()\n"
            "            try:\n"
            "                s.import_text(t3d)\n"
            "                oc.set_settings(s)\n"
            "            except Exception as _te:\n"
            "                report['warnings'].append('%s import_text: %s' % (op_info.get('op_type', ''), _te))\n"
            "        applied_ops += 1\n"
            "    report['applied_sections'].append({'ops': applied_ops})\n"
            "print('__MCP_RESULT__' + json.dumps(report))"
        )
        result = safe_execute(conn, script)
        return _ok(result)
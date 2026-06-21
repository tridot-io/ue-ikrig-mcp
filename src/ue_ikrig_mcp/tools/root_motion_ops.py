"""Root motion operations: diagnostic pelvis motion report + batch retarget wrapper.

`bake_pelvis_to_root_motion` ships in **report mode** first — it measures the
pelvis' horizontal drift per frame so the caller can decide whether to apply
the bake. A full write-path via UE's IAnimationDataController is exposed via
`apply=True` and is marked experimental because the controller's behavior
varies across UE 5.3/5.4/5.5/5.6.

`batch_retarget_with_root_motion` chains IKRetargetBatchOperation with the
set_root_motion_flags_bulk tool — the most common "retarget a folder of
mocap and set the root-motion flags" combo.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_LOCKS = {"ZERO", "REF_POSE", "ANIM_FIRST_FRAME"}


def register(server):
    @server.tool(
        name="bake_pelvis_to_root_motion",
        description=(
            "Measure (and optionally bake) pelvis horizontal motion into root "
            "motion on an AnimSequence. Mocap exports often have all motion in "
            "the pelvis — games want it on the root. In report mode (default) "
            "returns per-frame pelvis delta summary for inspection. In apply=True "
            "mode (EXPERIMENTAL) writes root translation keys and zeros pelvis "
            "XY via unreal.IAnimationDataController; availability of that API "
            "varies by UE version. "
            "vertical: 'preserve' keeps pelvis Z (recommended), 'full' transfers "
            "Z too, 'zero' zeros pelvis Z. Always sets enable_root_motion=True "
            "on apply."
        ),
    )
    async def bake_pelvis_to_root_motion(
        anim_sequence_path: str,
        pelvis_bone: str = "pelvis",
        root_bone: str = "root",
        vertical: str = "preserve",
        apply: bool = False,
    ) -> list[TextContent]:
        if vertical not in ("preserve", "full", "zero"):
            return _err("vertical must be 'preserve' | 'full' | 'zero'")
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        ap = escape_string(anim_sequence_path)
        pb = escape_string(pelvis_bone)
        rb = escape_string(root_bone)
        vm = f'"{vertical}"'
        do_apply = "True" if apply else "False"

        script = wrap_script(
            "import unreal\n"
            "import math as _math\n"
            f'seq = unreal.load_asset("{ap}")\n'
            "if seq is None:\n"
            f'    raise ValueError("AnimSequence not found: {ap}")\n'
            "if type(seq).__name__ != 'AnimSequence':\n"
            '    raise ValueError(f"Asset is not an AnimSequence: {type(seq).__name__}")\n'
            f'pelvis = "{pb}"; root = "{rb}"\n'
            f"vmode = {vm}; do_apply = {do_apply}\n"
            # Determine frame count and rate
            "num_frames = int(seq.get_editor_property('number_of_sampled_frames')) if hasattr(seq, 'number_of_sampled_frames') else None\n"
            "if num_frames is None:\n"
            "    try: num_frames = int(seq.get_number_of_sampled_keys())\n"
            "    except Exception:\n"
            "        try: num_frames = int(seq.get_editor_property('sequence_length') * 30)\n"
            "        except Exception: num_frames = 0\n"
            "if num_frames <= 0:\n"
            '    raise ValueError(f"cannot determine frame count on {seq.get_path_name()}")\n'
            # Sample pelvis per frame
            "samples = []\n"
            "try:\n"
            "    for f in range(num_frames):\n"
            "        t = unreal.AnimationLibrary.get_bone_pose_for_frame(seq, pelvis, f, False)\n"
            "        loc = t.translation\n"
            "        samples.append((f, float(loc.x), float(loc.y), float(loc.z)))\n"
            "except Exception as e:\n"
            '    raise ValueError(f"AnimationLibrary.get_bone_pose_for_frame failed: {e}")\n'
            "if not samples:\n"
            '    raise ValueError("no pelvis samples captured")\n'
            "f0 = samples[0]\n"
            "max_dx = max(abs(s[1] - f0[1]) for s in samples)\n"
            "max_dy = max(abs(s[2] - f0[2]) for s in samples)\n"
            "max_dz = max(abs(s[3] - f0[3]) for s in samples)\n"
            "report = {\n"
            "    'frames': num_frames,\n"
            "    'pelvis_bone': pelvis, 'root_bone': root,\n"
            "    'first_frame_xyz': [f0[1], f0[2], f0[3]],\n"
            "    'max_abs_delta': {'x': max_dx, 'y': max_dy, 'z': max_dz},\n"
            "    'applied': False, 'vertical_mode': vmode,\n"
            "}\n"
            "if do_apply:\n"
            "    try:\n"
            "        controller = seq.get_controller()\n"
            "    except Exception as e:\n"
            '        report["warning"] = f"seq.get_controller() failed: {e}; apply skipped"\n'
            '        print("__MCP_RESULT__" + json.dumps(report))\n'
            "    else:\n"
            "        controller.open_bracket('bake_pelvis_to_root_motion')\n"
            "        try:\n"
            "            # For each frame: root.translation += (pelvis.xy - pelvis[0].xy); pelvis.xy -= same delta\n"
            "            new_pelvis_trans = []\n"
            "            new_root_trans = []\n"
            "            for (f, x, y, z) in samples:\n"
            "                dx = x - f0[1]\n"
            "                dy = y - f0[2]\n"
            "                new_root = unreal.Vector(dx, dy, 0.0)\n"
            "                new_pel = unreal.Vector(f0[1], f0[2], z)\n"
            "                if vmode == 'full':\n"
            "                    new_root.z = z - f0[3]\n"
            "                    new_pel.z = f0[3]\n"
            "                elif vmode == 'zero':\n"
            "                    new_pel.z = 0.0\n"
            "                new_root_trans.append(new_root)\n"
            "                new_pelvis_trans.append(new_pel)\n"
            "            # NOTE: UE's IAnimationDataController write API varies by version; try multiple names\n"
            "            set_track = None\n"
            "            for name in ('set_bone_track_keys', 'update_bone_track_keys', 'add_bone_curve'):\n"
            "                if hasattr(controller, name):\n"
            "                    set_track = getattr(controller, name); break\n"
            "            if set_track is None:\n"
            "                raise ValueError('IAnimationDataController does not expose a bone-track write method on this UE version')\n"
            "            # Default rotations/scales: identity (controller requires arrays of same length)\n"
            "            identity_rot = [unreal.Quat(0, 0, 0, 1) for _ in range(len(new_root_trans))]\n"
            "            identity_scale = [unreal.Vector(1, 1, 1) for _ in range(len(new_root_trans))]\n"
            "            try:\n"
            "                set_track(root, new_root_trans, identity_rot, identity_scale)\n"
            "                set_track(pelvis, new_pelvis_trans, identity_rot, identity_scale)\n"
            "                report['applied'] = True\n"
            "            except Exception as e:\n"
            "                report['apply_err'] = str(e)[:200]\n"
            "            seq.set_editor_property('enable_root_motion', True)\n"
            "        finally:\n"
            "            controller.close_bracket()\n"
            "        ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'        report["saved"] = bool(ed.save_asset("{ap}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps(report))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="batch_retarget_with_root_motion",
        description=(
            "Batch-retarget every AnimSequence in source_folder via the given "
            "IKRetargeter from source_mesh to target_mesh, then set root-motion "
            "flags on every newly created output. One call, two common steps. "
            "Uses IKRetargetBatchOperation.duplicate_and_retarget internally. "
            "root_motion_root_lock: 'Zero' | 'RefPose' | 'AnimFirstFrame'."
        ),
    )
    async def batch_retarget_with_root_motion(
        retargeter_path: str,
        source_mesh_path: str,
        target_mesh_path: str,
        source_folder: str,
        dest_folder: str,
        enable_root_motion: bool = True,
        root_motion_root_lock: str = "AnimFirstFrame",
        force_root_lock: bool = False,
        name_prefix: str = "",
        name_suffix: str = "_Retargeted",
    ) -> list[TextContent]:
        norm = root_motion_root_lock.replace(" ", "_").upper()
        if norm not in _VALID_LOCKS:
            return _err(f"root_motion_root_lock must be one of ['Zero', 'RefPose', 'AnimFirstFrame']")

        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        smp = escape_string(source_mesh_path)
        tmp = escape_string(target_mesh_path)
        srcf = escape_string(source_folder)
        dstf = escape_string(dest_folder)
        erm = "True" if enable_root_motion else "False"
        lk = f'"{norm}"'
        frl = "True" if force_root_lock else "False"
        pfx = escape_string(name_prefix)
        sfx = escape_string(name_suffix)

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            f'src_mesh = unreal.load_asset("{smp}")\n'
            f'tgt_mesh = unreal.load_asset("{tmp}")\n'
            "if rtg is None or src_mesh is None or tgt_mesh is None:\n"
            '    raise ValueError("retargeter/source_mesh/target_mesh asset missing")\n'
            f'src_folder = "{srcf}"\n'
            f'dst_folder = "{dstf}"\n'
            # Gather AnimSequence AssetData in source_folder
            "eal = unreal.EditorAssetLibrary\n"
            "paths = eal.list_assets(src_folder, recursive=True, include_folder=False)\n"
            "anim_datas = []\n"
            "for p in paths:\n"
            "    d = eal.find_asset_data(p)\n"
            "    try: cls = str(d.asset_class_path.asset_name)\n"
            "    except Exception: cls = str(d.asset_class)\n"
            "    if cls == 'AnimSequence':\n"
            "        anim_datas.append(d)\n"
            "if not anim_datas:\n"
            "    raise ValueError(f'no AnimSequence found under {src_folder}')\n"
            "batch = unreal.IKRetargetBatchOperation\n"
            "fn = getattr(batch, 'duplicate_and_retarget')\n"
            "try:\n"
            f'    new_datas = fn(anim_datas, src_mesh, tgt_mesh, rtg, "{sfx}", "{pfx}", False)\n'
            "except TypeError:\n"
            # Fallback: older signature order with search/replace/prefix/suffix
            f'    new_datas = fn(anim_datas, src_mesh, tgt_mesh, rtg, "", "", "{pfx}", "{sfx}", False)\n'
            "new_paths = [str(d.package_name) if hasattr(d, 'package_name') else str(d.object_path) for d in (new_datas or [])]\n"
            # Apply root motion flags to each output, saving in-place
            f"erm_v = {erm}; frl_v = {frl}\n"
            f"lk_s = {lk}\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            "rm_succeeded = 0\n"
            "rm_failed = []\n"
            "for np in new_paths:\n"
            "    try:\n"
            "        # new_paths is package names; convert to object path\n"
            "        obj_path = np + '.' + np.split('/')[-1]\n"
            "        anim = unreal.load_asset(obj_path)\n"
            "        if anim is None:\n"
            "            rm_failed.append({'path': np, 'err': 'load_asset returned None'}); continue\n"
            "        if erm_v is not None: anim.set_editor_property('enable_root_motion', bool(erm_v))\n"
            "        anim.set_editor_property('root_motion_root_lock', getattr(unreal.RootMotionRootLock, lk_s))\n"
            "        if frl_v is not None: anim.set_editor_property('force_root_lock', bool(frl_v))\n"
            "        ed.save_asset(obj_path, only_if_is_dirty=False)\n"
            "        rm_succeeded += 1\n"
            "    except Exception as e:\n"
            "        rm_failed.append({'path': np, 'err': str(e)[:200]})\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"source_count": len(anim_datas),'
            '"retargeted_count": len(new_paths),'
            '"root_motion_applied_count": rm_succeeded,'
            '"root_motion_failed": rm_failed,'
            '"sample_outputs": new_paths[:10]'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

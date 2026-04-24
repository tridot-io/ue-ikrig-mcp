"""Batch FBX animation import for the mocap / Mixamo pipeline.

Wraps `unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks()` with
`FbxImportUI` + `FbxAnimSequenceImportData` configured for animation-only import
against a specific target skeleton — the common case for mocap integrators.

Mesh import is explicitly disabled: this tool only produces AnimSequences. If
you want a mesh + skeleton bootstrap, use UE's Content Browser importer.

Common presets and their defaults:
  * default         — UE Mannequin / MetaHuman conservative defaults
  * mixamo_to_mh    — Mixamo y-up-to-z-up scene conversion, uniform scale 1.0
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
        name="batch_import_fbx_animations",
        description=(
            "Import every .fbx (animation-only) in source_folder into dest_path "
            "bound to the given skeleton, using consistent options. Use this "
            "instead of clicking through the Content Browser importer for "
            "dozens of mocap takes. "
            "source_folder is a filesystem path (e.g. "
            "'C:/mocap_exports/takes/'). "
            "dest_path is a /Game/ content path. "
            "skeleton_path must resolve to a USkeleton asset. "
            "import_uniform_scale defaults to 1.0; Mixamo exports often need "
            "0.01 or check convert_scene=True."
        ),
    )
    async def batch_import_fbx_animations(
        source_folder: str,
        dest_path: str,
        skeleton_path: str,
        import_uniform_scale: float = 1.0,
        convert_scene: bool = True,
        use_t0_as_ref_pose: bool = False,
        import_morph_targets: bool = False,
        import_custom_attributes: bool = True,
        set_material_drive_parameter_on_custom_attribute: bool = False,
        replace_existing: bool = False,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        sf = escape_string(source_folder.replace("\\", "/"))
        dp = escape_string(dest_path)
        sp = escape_string(skeleton_path)

        script = wrap_script(
            "import unreal\n"
            "import os as _os\n"
            f'src = r"{sf}"\n'
            f'dest = "{dp}"\n'
            f'skel_path = "{sp}"\n'
            "skel = unreal.load_asset(skel_path)\n"
            "if skel is None or type(skel).__name__ != 'Skeleton':\n"
            "    raise ValueError(f'Skeleton not found at {skel_path}')\n"
            "if not _os.path.isdir(src):\n"
            "    raise ValueError(f'source_folder not a directory: {src}')\n"
            "fbx_files = []\n"
            "for name in _os.listdir(src):\n"
            "    low = name.lower()\n"
            "    if low.endswith('.fbx'):\n"
            "        fbx_files.append(_os.path.join(src, name))\n"
            "if not fbx_files:\n"
            "    raise ValueError('no .fbx files found in source_folder')\n"
            f"ius = float({float(import_uniform_scale)})\n"
            f"conv = {('True' if convert_scene else 'False')}\n"
            f"t0 = {('True' if use_t0_as_ref_pose else 'False')}\n"
            f"morph = {('True' if import_morph_targets else 'False')}\n"
            f"custom = {('True' if import_custom_attributes else 'False')}\n"
            f"mdrive = {('True' if set_material_drive_parameter_on_custom_attribute else 'False')}\n"
            f"replace = {('True' if replace_existing else 'False')}\n"
            "tasks = []\n"
            "for path in fbx_files:\n"
            "    task = unreal.AssetImportTask()\n"
            "    task.filename = path\n"
            "    task.destination_path = dest\n"
            "    task.automated = True\n"
            "    task.replace_existing = replace\n"
            "    task.save = True\n"
            "    opts = unreal.FbxImportUI()\n"
            "    opts.import_as_skeletal = True\n"
            "    opts.import_mesh = False\n"
            "    opts.import_animations = True\n"
            "    opts.mesh_type_to_import = unreal.FBXImportType.FBXIT_ANIMATION\n"
            "    opts.skeleton = skel\n"
            "    opts.create_physics_asset = False\n"
            "    anim_data = opts.anim_sequence_import_data\n"
            "    anim_data.set_editor_property('import_uniform_scale', ius)\n"
            "    anim_data.set_editor_property('convert_scene', conv)\n"
            "    anim_data.set_editor_property('use_default_sample_rate', False)\n"
            "    if hasattr(anim_data, 'preserve_local_transform'):\n"
            "        anim_data.set_editor_property('preserve_local_transform', True)\n"
            "    skel_data = opts.skeletal_mesh_import_data\n"
            "    skel_data.set_editor_property('import_uniform_scale', ius)\n"
            "    skel_data.set_editor_property('convert_scene', conv)\n"
            "    if hasattr(skel_data, 'use_t0_as_ref_pose'):\n"
            "        skel_data.set_editor_property('use_t0_as_ref_pose', t0)\n"
            "    opts.set_editor_property('import_morph_targets', morph) if hasattr(opts, 'import_morph_targets') else None\n"
            "    task.options = opts\n"
            "    tasks.append(task)\n"
            "at = unreal.AssetToolsHelpers.get_asset_tools()\n"
            "at.import_asset_tasks(tasks)\n"
            "imported = []\n"
            "failed = []\n"
            "for i, t in enumerate(tasks):\n"
            "    out = t.get_editor_property('imported_object_paths') if hasattr(t, 'get_editor_property') else t.imported_object_paths\n"
            "    if out:\n"
            "        imported.append({'fbx': t.filename, 'assets': list(out)})\n"
            "    else:\n"
            "        failed.append({'fbx': t.filename, 'err': 'imported_object_paths empty'})\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"total_fbx": len(fbx_files),'
            '"imported_count": len(imported),'
            '"imported_sample": imported[:10],'
            '"failed": failed'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

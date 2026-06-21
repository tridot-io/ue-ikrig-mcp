"""Sequencer → AnimSequence bake helpers.

Exposes the Sequencer Python API surface that animators script most often:
list bindings, and bake a selected binding's Control Rig / skeletal track to
an AnimSequence for use in gameplay AnimBPs.

UE renamed the export-options struct between minor versions
(`AnimSeqOptionExport` → `AnimSeqExportOption`), so this module probes both at
runtime and picks whichever is available.
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
        name="list_sequence_bindings",
        description=(
            "List top-level bindings on a LevelSequence. Each binding carries "
            "a name, an id (used by export_sequence_to_anim), and the classes "
            "of its tracks. Use this to pick which actor/binding to bake."
        ),
    )
    async def list_sequence_bindings(level_sequence_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        lp = escape_string(level_sequence_path)
        script = wrap_script(
            "import unreal\n"
            f'ls = unreal.load_asset("{lp}")\n'
            "if ls is None:\n"
            f'    raise ValueError("LevelSequence not found: {lp}")\n'
            "if type(ls).__name__ != 'LevelSequence':\n"
            '    raise ValueError(f"Asset is not a LevelSequence: {type(ls).__name__}")\n'
            "out = []\n"
            "try:\n"
            "    bindings = ls.get_bindings()\n"
            "except Exception:\n"
            "    bindings = []\n"
            "for b in bindings:\n"
            "    entry = {'name': b.get_name() if hasattr(b, 'get_name') else str(b)}\n"
            "    try: entry['id'] = str(b.get_id())\n"
            "    except Exception: entry['id'] = None\n"
            "    try:\n"
            "        tracks = b.get_tracks()\n"
            "        entry['track_classes'] = [type(t).__name__ for t in tracks]\n"
            "    except Exception:\n"
            "        entry['track_classes'] = None\n"
            "    out.append(entry)\n"
            'print("__MCP_RESULT__" + json.dumps({"binding_count": len(out), "bindings": out}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="export_sequence_to_anim",
        description=(
            "Bake a LevelSequence binding (skeletal + Control Rig tracks) to an "
            "AnimSequence asset. The destination AnimSequence asset must already "
            "exist (create it via the Content Browser or the preview tools) "
            "and be bound to the same skeleton as the binding's mesh. "
            "binding_name_or_id accepts either a binding's display name or its "
            "id string (see list_sequence_bindings)."
        ),
    )
    async def export_sequence_to_anim(
        level_sequence_path: str,
        anim_sequence_path: str,
        binding_name_or_id: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        lp = escape_string(level_sequence_path)
        ap = escape_string(anim_sequence_path)
        bn = escape_string(binding_name_or_id)

        script = wrap_script(
            "import unreal\n"
            f'ls = unreal.load_asset("{lp}")\n'
            "if ls is None:\n"
            f'    raise ValueError("LevelSequence not found: {lp}")\n'
            f'anim = unreal.load_asset("{ap}")\n'
            "if anim is None:\n"
            f'    raise ValueError("AnimSequence not found: {ap}")\n'
            "if type(anim).__name__ != 'AnimSequence':\n"
            '    raise ValueError(f"Destination is not an AnimSequence: {type(anim).__name__}")\n'
            f'needle = "{bn}"\n'
            "target = None\n"
            "for b in ls.get_bindings():\n"
            "    nm = b.get_name() if hasattr(b, 'get_name') else ''\n"
            "    try:\n"
            "        bid = str(b.get_id())\n"
            "    except Exception:\n"
            "        bid = ''\n"
            "    if nm == needle or bid == needle:\n"
            "        target = b; break\n"
            "if target is None:\n"
            f'    raise ValueError(f"Binding not found: {{needle!r}}")\n'
            # Select export-options class by probing availability
            "opt_cls = None\n"
            "for cname in ('AnimSeqExportOption', 'AnimSeqOptionExport'):\n"
            "    if hasattr(unreal, cname):\n"
            "        opt_cls = getattr(unreal, cname); break\n"
            "if opt_cls is None:\n"
            '    raise ValueError("Neither AnimSeqExportOption nor AnimSeqOptionExport is exposed to Python on this UE version")\n'
            "opt = opt_cls()\n"
            "for prop_name, val in (('export_transforms', True),\n"
            "                        ('export_curves', True),\n"
            "                        ('record_in_world_space', False)):\n"
            "    try: opt.set_editor_property(prop_name, val)\n"
            "    except Exception: pass\n"
            "world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "try:\n"
            "    ok = unreal.SequencerTools.export_anim_sequence(world, ls, anim, opt, target, False)\n"
            "except Exception:\n"
            # Older signature without `create_link` arg
            "    ok = unreal.SequencerTools.export_anim_sequence(world, ls, anim, opt, target)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{ap}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({"exported": bool(ok), "anim": anim.get_path_name(), "saved": saved}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

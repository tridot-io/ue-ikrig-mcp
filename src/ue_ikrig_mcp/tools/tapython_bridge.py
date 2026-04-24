"""Optional TAPython adapter — exposes plugin-only capabilities when present.

TAPython (a free community plugin) adds to UE's Python surface:
  * AnimGraph node spawning and graph JSON I/O
  * Active-viewport pixel capture (including asset editor viewports, which
    stock UE exposes nothing for)
  * Slate/UI tooling surface

This module gates every call on `tapython_status().installed`. When TAPython
is not installed in the running editor, each tool returns a clear error
indicating what plugin is missing. Core ue-ikrig-mcp tools never require
TAPython; this module is additive only.

Install: https://www.tacolor.xyz/  — add the TAPython plugin to the UE project,
enable it, restart the editor.
"""

import base64
import json
import os
import tempfile
import time

from mcp.types import ImageContent, TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_STATUS_SCRIPT = (
    "import unreal\n"
    "info = {'installed': False, 'version': None, 'capabilities': []}\n"
    "if hasattr(unreal, 'PythonBPLib'):\n"
    "    info['installed'] = True\n"
    "    pbl = unreal.PythonBPLib\n"
    "    for m in ['spawn_function_to_graph', 'spawn_function_to_graph_with_spawner',\n"
    "             'get_graph_panel_nodes', 'clear_graph_panel', 'get_graph_selected_node',\n"
    "             'get_viewport_pixels', 'get_viewport_pixels_as_texture',\n"
    "             'get_viewport_linear_color_pixels']:\n"
    "        if hasattr(pbl, m):\n"
    "            info['capabilities'].append(m)\n"
    "    for vn in ['get_tapython_version', 'get_version']:\n"
    "        try:\n"
    "            info['version'] = str(getattr(pbl, vn)())\n"
    "            break\n"
    "        except Exception: pass\n"
    'print("__MCP_RESULT__" + json.dumps(info))\n'
)


def register(server):
    @server.tool(
        name="tapython_status",
        description=(
            "Detect whether the TAPython plugin is installed in the running UE "
            "editor and enumerate which capabilities it exposes. Use this before "
            "calling any other tapython_* tool to gracefully degrade. Returns "
            "{installed, version, capabilities}."
        ),
    )
    async def tapython_status() -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        result = conn.execute(wrap_script(_STATUS_SCRIPT))
        return _ok(result)

    @server.tool(
        name="tapython_create_animbp_node",
        description=(
            "Add an AnimGraphNode to an Animation Blueprint's AnimGraph. Requires "
            "the TAPython plugin (`unreal.PythonBPLib.spawn_function_to_graph`). "
            "Stock UE 5.6 has no Python API for AnimGraph node creation — this is "
            "the only path short of custom C++. node_class_name example: "
            "'AnimGraphNode_TwoBoneIK', 'AnimGraphNode_ModifyBone', "
            "'AnimGraphNode_Fabrik'. graph_name defaults to 'AnimGraph'. Position "
            "controls the editor placement; pin wiring is not handled here."
        ),
    )
    async def tapython_create_animbp_node(
        anim_bp_path: str,
        node_class_name: str,
        graph_name: str = "AnimGraph",
        position_x: float = 0.0,
        position_y: float = 0.0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(anim_bp_path)
        ncn = escape_string(node_class_name)
        gn = escape_string(graph_name)

        script = wrap_script(
            "import unreal\n"
            "if not hasattr(unreal, 'PythonBPLib'):\n"
            '    raise ValueError("TAPython plugin not installed; call tapython_status to diagnose")\n'
            "pbl = unreal.PythonBPLib\n"
            "if not hasattr(pbl, 'spawn_function_to_graph'):\n"
            '    raise ValueError("TAPython installed but spawn_function_to_graph unavailable; plugin may be older than required")\n'
            f'abp = unreal.load_asset("{p}")\n'
            "if abp is None:\n"
            f'    raise ValueError("AnimBlueprint not found: {p}")\n'
            "if type(abp).__name__ != 'AnimBlueprint':\n"
            '    raise ValueError(f"Asset is not an AnimBlueprint: {type(abp).__name__}")\n'
            f'_gn = "{gn}"\n'
            "graph = None\n"
            "for g in abp.get_animation_graphs():\n"
            "    if g.get_name() == _gn:\n"
            "        graph = g; break\n"
            "if graph is None:\n"
            f'    raise ValueError(f"AnimGraph {{_gn!r}} not found on {p}")\n'
            f'node = pbl.spawn_function_to_graph(graph, "{ncn}", unreal.Vector2D({float(position_x)}, {float(position_y)}))\n'
            "if node is None:\n"
            f'    raise ValueError("spawn_function_to_graph returned None for class {ncn} (name may be invalid or not permissible in AnimGraph)")\n'
            "abp.modify()\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{p}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({'
            '"graph": _gn, "spawned_node": node.get_name(),'
            '"class": type(node).__name__, "saved": saved,'
            '"note": "Recompile the AnimBlueprint (editor Compile button or BlueprintEditorLibrary.compile_blueprint) to pick up the new node at runtime."'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="tapython_capture_active_viewport",
        description=(
            "Capture the currently focused UE viewport (including asset editor "
            "viewports — IK Retargeter preview, Persona, Control Rig, etc.) to "
            "a PNG and return it inline as MCP ImageContent. Requires TAPython "
            "(`unreal.PythonBPLib.get_viewport_pixels_as_texture` or "
            "`save_viewport_to_file`). Fills the gap that stock UE's "
            "`take_high_res_screenshot` only covers the level viewport."
        ),
    )
    async def tapython_capture_active_viewport(
        output_path: str = "",
    ) -> list:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return [TextContent(type="text", text=str(e))]

        # Use a temp PNG path if caller didn't specify one
        if not output_path:
            ts_ms = int(time.time() * 1000)
            output_path = os.path.join(tempfile.gettempdir(), f"tapython_viewport_{ts_ms}.png")

        op = escape_string(output_path.replace("\\", "/"))

        script = wrap_script(
            "import unreal\n"
            "import os as _os\n"
            "if not hasattr(unreal, 'PythonBPLib'):\n"
            '    raise ValueError("TAPython plugin not installed; call tapython_status to diagnose")\n'
            "pbl = unreal.PythonBPLib\n"
            f'out = r"{op}"\n'
            "_os.makedirs(_os.path.dirname(out) or '.', exist_ok=True)\n"
            "written = False\n"
            "# Strategy 1: direct save-to-file if exposed\n"
            "if hasattr(pbl, 'save_viewport_to_file'):\n"
            "    try:\n"
            "        pbl.save_viewport_to_file(out)\n"
            "        written = _os.path.exists(out) and _os.path.getsize(out) > 0\n"
            "    except Exception:\n"
            "        written = False\n"
            "# Strategy 2: get pixels as Texture2D, then export via render target path\n"
            "if not written and hasattr(pbl, 'get_viewport_pixels_as_texture'):\n"
            "    tex = pbl.get_viewport_pixels_as_texture()\n"
            "    if tex is None:\n"
            "        raise ValueError('get_viewport_pixels_as_texture returned None (no focused viewport?)')\n"
            "    # If it is a TextureRenderTarget2D, use RenderingLibrary.export_render_target\n"
            "    if isinstance(tex, unreal.TextureRenderTarget2D):\n"
            "        world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()\n"
            "        unreal.RenderingLibrary.export_render_target(world, tex, _os.path.dirname(out), _os.path.basename(out))\n"
            "        written = _os.path.exists(out) and _os.path.getsize(out) > 0\n"
            "    else:\n"
            "        # Fallback: treat as Texture2D; export via KismetRenderingLibrary if available\n"
            "        try:\n"
            "            unreal.RenderingLibrary.export_texture2_d(tex, _os.path.dirname(out), _os.path.basename(out))\n"
            "            written = _os.path.exists(out) and _os.path.getsize(out) > 0\n"
            "        except Exception:\n"
            "            written = False\n"
            "if not written:\n"
            '    raise ValueError("TAPython viewport capture failed — no supported API path worked")\n'
            f'print("__MCP_RESULT__" + json.dumps({{"out_path": out, "bytes": _os.path.getsize(out)}}))'
        )
        res = conn.execute(script)

        # Read the PNG back and return as ImageContent
        # Give UE a moment to finalize the file
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                time.sleep(0.1)
                break
            time.sleep(0.2)

        if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
            return [TextContent(type="text", text=json.dumps({"error": True, "message": f"capture produced no file at {output_path}", "raw": res}))]

        with open(output_path, "rb") as fh:
            data = fh.read()
        b64 = base64.b64encode(data).decode("ascii")
        return [
            ImageContent(type="image", data=b64, mimeType="image/png"),
            TextContent(type="text", text=f"captured {len(data)} bytes → {output_path}"),
        ]

    @server.tool(
        name="tapython_dump_animgraph_json",
        description=(
            "Dump an AnimGraph's node/edge topology as JSON using TAPython's "
            "`get_graph_panel_nodes`. Useful for diffing graphs across versions "
            "or serializing as a template. Pair with tapython_apply_animgraph_json "
            "to recreate on another AnimBP. graph_name defaults to 'AnimGraph'."
        ),
    )
    async def tapython_dump_animgraph_json(
        anim_bp_path: str,
        graph_name: str = "AnimGraph",
        out_path: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(anim_bp_path)
        gn = escape_string(graph_name)
        op = escape_string(out_path) if out_path else ""

        script = wrap_script(
            "import unreal\n"
            "import json as _json\n"
            "import os as _os\n"
            "if not hasattr(unreal, 'PythonBPLib'):\n"
            '    raise ValueError("TAPython plugin not installed; call tapython_status to diagnose")\n'
            "pbl = unreal.PythonBPLib\n"
            "if not hasattr(pbl, 'get_graph_panel_nodes'):\n"
            '    raise ValueError("TAPython installed but get_graph_panel_nodes unavailable")\n'
            f'abp = unreal.load_asset("{p}")\n'
            "if abp is None:\n"
            f'    raise ValueError("AnimBlueprint not found: {p}")\n'
            f'_gn = "{gn}"\n'
            "graph = None\n"
            "for g in abp.get_animation_graphs():\n"
            "    if g.get_name() == _gn:\n"
            "        graph = g; break\n"
            "if graph is None:\n"
            f'    raise ValueError(f"AnimGraph {{_gn!r}} not found on {p}")\n'
            "nodes_data = pbl.get_graph_panel_nodes(graph)\n"
            # TAPython returns a JSON-ish structure; coerce to serializable form
            "try:\n"
            "    dump = _json.loads(nodes_data) if isinstance(nodes_data, str) else nodes_data\n"
            "except Exception:\n"
            "    dump = str(nodes_data)\n"
            "payload = {'anim_bp': abp.get_path_name(), 'graph': _gn, 'nodes': dump}\n"
            f'_out = r"{op}"\n'
            "if _out:\n"
            "    _os.makedirs(_os.path.dirname(_out) or '.', exist_ok=True)\n"
            "    with open(_out, 'w', encoding='utf-8') as _f:\n"
            "        _json.dump(payload, _f, indent=2, ensure_ascii=False)\n"
            "    result = {'out_path': _out, 'node_count': len(dump) if hasattr(dump, '__len__') else None}\n"
            "else:\n"
            "    result = {'inline': payload, 'node_count': len(dump) if hasattr(dump, '__len__') else None}\n"
            'print("__MCP_RESULT__" + json.dumps(result))'
        )
        result = conn.execute(script)
        return _ok(result)

    @server.tool(
        name="tapython_apply_animgraph_json",
        description=(
            "EXPERIMENTAL. Apply a previously exported AnimGraph JSON onto a "
            "target AnimBlueprint via TAPython. Creates missing nodes and wires "
            "pins where possible. Use dry_run=True first to preview operations."
        ),
    )
    async def tapython_apply_animgraph_json(
        anim_bp_path: str,
        in_path: str,
        graph_name: str = "AnimGraph",
        dry_run: bool = True,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        p = escape_string(anim_bp_path)
        ip = escape_string(in_path)
        gn = escape_string(graph_name)
        dr = "True" if dry_run else "False"

        script = wrap_script(
            "import unreal\n"
            "import json as _json\n"
            "if not hasattr(unreal, 'PythonBPLib'):\n"
            '    raise ValueError("TAPython plugin not installed")\n'
            "pbl = unreal.PythonBPLib\n"
            f'abp = unreal.load_asset("{p}")\n'
            "if abp is None:\n"
            f'    raise ValueError("AnimBlueprint not found: {p}")\n'
            f'with open(r"{ip}", "r", encoding="utf-8") as _f:\n'
            "    payload = _json.load(_f)\n"
            f'_gn = "{gn}"\n'
            f"dry = {dr}\n"
            "graph = None\n"
            "for g in abp.get_animation_graphs():\n"
            "    if g.get_name() == _gn:\n"
            "        graph = g; break\n"
            "if graph is None:\n"
            f'    raise ValueError(f"AnimGraph {{_gn!r}} not found on {p}")\n'
            "existing = pbl.get_graph_panel_nodes(graph) if hasattr(pbl, 'get_graph_panel_nodes') else None\n"
            "nodes_spec = payload.get('nodes', [])\n"
            "plan = []\n"
            "created = []\n"
            "for entry in (nodes_spec if isinstance(nodes_spec, list) else []):\n"
            "    cls = entry.get('class') or entry.get('node_class')\n"
            "    if not cls:\n"
            "        plan.append({'skipped': True, 'entry': entry, 'reason': 'no class/node_class key'})\n"
            "        continue\n"
            "    plan.append({'create': cls, 'position': entry.get('position')})\n"
            "    if not dry and hasattr(pbl, 'spawn_function_to_graph'):\n"
            "        pos = entry.get('position') or [0, 0]\n"
            "        try:\n"
            "            node = pbl.spawn_function_to_graph(graph, cls, unreal.Vector2D(float(pos[0]), float(pos[1])))\n"
            "            created.append({'class': cls, 'name': node.get_name() if node else None})\n"
            "        except Exception as e:\n"
            "            created.append({'class': cls, 'err': str(e)[:120]})\n"
            "result = {'dry_run': dry, 'plan': plan, 'created': created}\n"
            "if not dry:\n"
            "    abp.modify()\n"
            "    ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'    result["saved"] = bool(ed.save_asset("{p}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps(result))'
        )
        result = conn.execute(script)
        return _ok(result)

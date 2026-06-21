"""Optional TAPython adapter — exposes plugin-only capabilities when present.

TAPython (a free community plugin) adds to UE's Python surface. What is and
isn't usable through this MCP was verified live against TAPython 1.3.3 / UE 5.7:

  * Active-viewport pixel capture (including asset editor viewports, which stock
    UE exposes nothing for) — USABLE: it captures the focused editor viewport,
    which matches an agent driving the editor interactively.
  * Graph node spawning / inspection (`spawn_function_to_graph`,
    `get_graph_panel_nodes`, `get_all_k2_nodes`) — NOT usable headlessly. These
    are bound to interactive editor UI context, not to assets by path:
    `spawn_function_to_graph`/`get_graph_panel_nodes` are instance methods on a
    `ChameleonData` object tied to an open Chameleon tool's graph panel, and
    `PythonBPAssetLib.get_all_k2_nodes()` takes NO arguments (it reads whatever
    Blueprint graph is currently focused in the editor). The MCP operates on
    assets by path over remote execution with no guaranteed open editor, so the
    graph-authoring/inspection tools below report a clear capability error and
    point at the headless alternative (the `cr_*` tools for Control Rig graphs).

This module gates every call on `tapython_status().installed`. Core
ue-ikrig-mcp tools never require TAPython; this module is additive only.

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
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


# Why TAPython's graph APIs can't be driven by asset path through this MCP.
# Verified live (TAPython 1.3.3, UE 5.7): spawn_function_to_graph /
# get_graph_panel_nodes are ChameleonData *instance* methods bound to an open
# Chameleon tool's graph panel, and PythonBPAssetLib.get_all_k2_nodes() takes no
# arguments (it reads the currently-focused Blueprint editor graph).
_GRAPH_CTX_NOTE = (
    "TAPython's graph APIs are bound to interactive editor UI context, not to assets "
    "by path. spawn_function_to_graph / get_graph_panel_nodes are instance methods on "
    "a ChameleonData object tied to an open Chameleon tool's graph panel, and "
    "PythonBPAssetLib.get_all_k2_nodes() takes no arguments (it reads whatever "
    "Blueprint graph is currently focused in the editor). This MCP operates on assets "
    "by path over remote execution with no guaranteed open editor, so it cannot target "
    "an arbitrary AnimBlueprint/ControlRig asset graph this way. (Verified live against "
    "TAPython 1.3.3 / UE 5.7.)"
)
_GRAPH_AUTHORING_UNAVAILABLE = (
    "AnimGraph node authoring via TAPython is not available through this MCP. "
    + _GRAPH_CTX_NOTE
    + " For Control Rig graphs use the cr_* tools (RigVMController), which author nodes "
    "headlessly. Stock UE exposes no Python API to create AnimGraph nodes."
)
_GRAPH_INSPECTION_UNAVAILABLE = (
    "AnimGraph topology dump via TAPython is not available through this MCP. "
    + _GRAPH_CTX_NOTE
    + " For Control Rig graph inspection use cr_dump_graph."
)


_STATUS_SCRIPT = (
    "import unreal\n"
    "info = {'installed': False, 'version': None, 'libs': [], 'capabilities': {},\n"
    "        'graph_api_note': 'Graph node APIs (spawn_function_to_graph, "
    "get_graph_panel_nodes, get_all_k2_nodes) are bound to interactive editor UI / "
    "Chameleon-tool context, not to assets by path; they cannot author or inspect an "
    "arbitrary asset graph headlessly. Use the cr_* tools for Control Rig graphs.'}\n"
    "info['installed'] = hasattr(unreal, 'PythonBPLib')\n"
    "if info['installed']:\n"
    "    for lib in ['PythonBPLib', 'PythonBPAssetLib', 'ChameleonData', 'PythonMeshLib',\n"
    "                'PythonTextureLib', 'PythonWidgetLib', 'PythonMaterialLib',\n"
    "                'PythonStructLib', 'PythonDataTableLib', 'PythonEnumLib',\n"
    "                'PythonLevelLib', 'PythonTestLib']:\n"
    "        if hasattr(unreal, lib):\n"
    "            info['libs'].append(lib)\n"
    "    try:\n"
    "        _v = unreal.PythonBPLib.get_ta_python_version()\n"
    "        try:\n"
    "            info['version'] = _v if isinstance(_v, dict) else json.loads(str(_v))\n"
    "        except Exception:\n"
    "            info['version'] = str(_v)\n"
    "    except Exception:\n"
    "        info['version'] = None\n"
    "    pbl = unreal.PythonBPLib\n"
    "    cd = getattr(unreal, 'ChameleonData', None)\n"
    "    bpa = getattr(unreal, 'PythonBPAssetLib', None)\n"
    "    info['capabilities']['viewport_capture'] = any(hasattr(pbl, m) for m in\n"
    "        ['get_viewport_pixels_as_data', 'get_viewport_pixels_as_texture', 'save_viewport_to_file'])\n"
    "    info['capabilities']['chameleon_graph_panel'] = bool(cd is not None and hasattr(cd, 'spawn_function_to_graph'))\n"
    "    info['capabilities']['k2_node_inspection_open_editor'] = bool(bpa is not None and hasattr(bpa, 'get_all_k2_nodes'))\n"
    "    try:\n"
    "        info['open_chameleon_tools'] = (list(pbl.get_all_chameleon_data_paths())\n"
    "            if hasattr(pbl, 'get_all_chameleon_data_paths') else [])\n"
    "    except Exception as _e:\n"
    "        info['open_chameleon_tools'] = 'err:%s' % _e\n"
    'print("__MCP_RESULT__" + json.dumps(info, default=str))\n'
)


def register(server):
    @server.tool(
        name="tapython_status",
        description=(
            "Detect whether the TAPython plugin is installed in the running UE "
            "editor and report what it can actually do through this MCP. Use this "
            "before any other tapython_* tool to gracefully degrade. Returns "
            "{installed, version, libs, capabilities, open_chameleon_tools, "
            "graph_api_note}. Note: graph node authoring/inspection is NOT available "
            "headlessly (the APIs are bound to interactive editor UI context); only "
            "viewport_capture is generally usable. Use the cr_* tools for Control Rig "
            "graphs."
        ),
    )
    async def tapython_status() -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        result = safe_execute(conn, wrap_script(_STATUS_SCRIPT))
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
        # Not achievable headlessly — see _GRAPH_CTX_NOTE. The previously-shipped
        # implementation called unreal.PythonBPLib.spawn_function_to_graph, which
        # does not exist on PythonBPLib (the method is a ChameleonData instance
        # method), so it always failed; this returns an accurate capability error.
        return _err(_GRAPH_AUTHORING_UNAVAILABLE)

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
        # Not achievable headlessly — see _GRAPH_CTX_NOTE. get_graph_panel_nodes is
        # a ChameleonData instance method (open Chameleon tool panel) and
        # get_all_k2_nodes() takes no args (reads the focused editor graph); neither
        # can dump an arbitrary AnimBlueprint asset by path. The prior implementation
        # called unreal.PythonBPLib.get_graph_panel_nodes, which does not exist.
        return _err(_GRAPH_INSPECTION_UNAVAILABLE)

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
        # Not achievable headlessly — see _GRAPH_CTX_NOTE. Applying a graph spec
        # requires spawn_function_to_graph, a ChameleonData instance method that
        # cannot target an AnimBlueprint asset by path. The prior implementation
        # called the non-existent unreal.PythonBPLib.spawn_function_to_graph.
        return _err(_GRAPH_AUTHORING_UNAVAILABLE)

"""MCP tool for capturing asset-editor preview viewports.

Opens the named asset in its UE editor tab (IK Retargeter, AnimBP, SkeletalMesh,
PhysicsAsset, ControlRigBlueprint, etc.) and captures the resulting window via
the OS-level helper from capture_os.
"""

import sys

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string
from .capture_common import parse_mcp_result
from .capture_os import _capture_window_by_title


# Asset classes that UE docks inside the main editor window rather than tearing off.
# For these, the title_match falls back to "Unreal Editor" unless the user overrides.
_DOCKED_CLASSES = {
    "IKRetargeter",
    "IKRigDefinition",
    "AnimBlueprint",
    "SkeletalMesh",
    "PhysicsAsset",
    "ControlRigBlueprint",
}


def _build_open_asset_script(asset_path: str) -> str:
    p = escape_string(asset_path)
    return wrap_script(
        "import unreal as _ue\n"
        "import json as _json\n"
        f'_asset = _ue.load_asset("{p}")\n'
        "if _asset is None:\n"
        f'    print("__MCP_RESULT__" + _json.dumps({{"error": True, "message": "asset not found", "asset_path": "{p}"}}))\n'
        "else:\n"
        "    _subsystem = _ue.get_editor_subsystem(_ue.AssetEditorSubsystem)\n"
        "    if _subsystem:\n"
        "        _subsystem.open_editor_for_asset(_asset)\n"
        "    _level_sub = _ue.get_editor_subsystem(_ue.LevelEditorSubsystem)\n"
        "    if _level_sub:\n"
        "        try:\n"
        "            _level_sub.editor_invalidate_viewports()\n"
        "        except Exception:\n"
        "            pass\n"
        "    _name = _asset.get_name()\n"
        "    _cls = _asset.get_class().get_name() if _asset.get_class() else 'Unknown'\n"
        '    print("__MCP_RESULT__" + _json.dumps({"name": _name, "class": _cls}))\n'
    )


def register(server):
    @server.tool()
    async def capture_asset_editor(
        asset_path: str,
        settle_ms: int = 400,
        tab_match: str | None = None,
        method: str = "auto",
    ) -> list:
        """Capture an asset-editor preview viewport for IK Retargeter, AnimBP, Skeleton, PhysicsAsset, or ControlRig.

        Opens the asset in its UE editor (if not already open), waits for Slate to
        repaint, then takes an OS-level screenshot of the relevant window region.

        Args:
            asset_path: Full UE content path, e.g. '/Game/Characters/RTG_Body'.
            settle_ms: Milliseconds to wait after opening/focusing before capture. Default 400.
            tab_match: Optional substring to narrow capture to a specific child tab/widget.
                When None, defaults to the asset short name for IKRetargeter assets so
                only the retargeter viewport tab is captured.
            method: 'auto' (default), 'mss', or 'printwindow'.
        """
        if sys.platform != "win32":
            return [TextContent(
                type="text",
                text="capture_asset_editor: only supported on Windows. Use capture_viewport for cross-platform level viewport capture.",
            )]

        try:
            conn = get_connection()
        except UENotRunningError as e:
            return [TextContent(type="text", text=str(e))]

        script = _build_open_asset_script(asset_path)
        result = conn.execute(script)

        parsed = parse_mcp_result(result)
        if parsed is None:
            return [TextContent(
                type="text",
                text=f"capture_asset_editor: UE script returned no result for {asset_path!r}. Is UE running and the path valid?",
            )]

        if parsed.get("error"):
            return [TextContent(
                type="text",
                text=f"capture_asset_editor: {parsed.get('message', 'unknown error')} — asset_path={asset_path!r}",
            )]

        asset_name: str = parsed.get("name", "")
        asset_class: str = parsed.get("class", "")

        # Resolve title_match: asset editors typically title the window with the asset name.
        # The main UE window title also contains the project name; asset_name is a safe substring.
        title_match = asset_name if asset_name else "Unreal Editor"

        # Default tab_match for IKRetargeter to the asset name so we scope to its tab.
        resolved_tab_match = tab_match
        if resolved_tab_match is None and asset_class == "IKRetargeter":
            resolved_tab_match = asset_name

        asset_meta = {
            "asset_name": asset_name,
            "asset_class": asset_class,
            "asset_path": asset_path,
        }

        result_list = await _capture_window_by_title(
            title_match=title_match,
            client_only=True,
            foreground=True,
            settle_ms=settle_ms,
            tab_match=resolved_tab_match,
            method=method,
            extra_text=asset_meta,
        )

        # If capture failed by title and asset is likely docked, retry with main window
        if (
            len(result_list) == 1
            and hasattr(result_list[0], "text")
            and "no visible window matched" in result_list[0].text
            and asset_class in _DOCKED_CLASSES
        ):
            result_list = await _capture_window_by_title(
                title_match="Unreal Editor",
                client_only=True,
                foreground=True,
                settle_ms=settle_ms,
                tab_match=resolved_tab_match or asset_name,
                method=method,
                extra_text=asset_meta,
            )

        return result_list

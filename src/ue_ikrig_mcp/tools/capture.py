"""Viewport capture tool for the ue-ikrig MCP.

Takes a screenshot of the active Unreal Editor viewport and returns it inline
as an MCP ImageContent so the calling LLM can see the result of an edit
without a human having to screenshot and paste.
"""

import asyncio
import base64
import json
import os
import time

from mcp.types import ImageContent, TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script


def register(server):
    @server.tool()
    async def capture_viewport(width: int = 1280, height: int = 720) -> list:
        """Capture the active Unreal Editor viewport to a PNG and return it inline.

        Writes the image to `<ProjectSaved>/Screenshots/Claude/capture_<ts>.png`
        via `unreal.AutomationLibrary.take_high_res_screenshot`, then reads it back
        and returns it as an MCP ImageContent. Useful after IK rig / retargeter
        tuning changes — the caller can see the updated preview without needing a
        human screenshot in the loop.

        Args:
            width: Capture width in pixels (default 1280).
            height: Capture height in pixels (default 720).
        """
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return [TextContent(type="text", text=str(e))]

        script = wrap_script(
            "import unreal\n"
            "import os as _os\n"
            "import time as _time\n"
            f"_w, _h = {int(width)}, {int(height)}\n"
            "_proj_saved = unreal.Paths.project_saved_dir()\n"
            "_out_dir = _os.path.join(_proj_saved, 'Screenshots', 'Claude')\n"
            "_os.makedirs(_out_dir, exist_ok=True)\n"
            "_fname = 'capture_' + str(int(_time.time() * 1000)) + '.png'\n"
            "_full_path = _os.path.abspath(_os.path.join(_out_dir, _fname))\n"
            "unreal.AutomationLibrary.take_high_res_screenshot(_w, _h, _full_path)\n"
            'print("__MCP_RESULT__" + json.dumps({"path": _full_path}))'
        )

        result = conn.execute(script)
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if not parsed or not parsed.get("path"):
            raw = result.get("output") if isinstance(result, dict) else str(result)
            return [TextContent(type="text", text=f"capture_viewport: failed to resolve output path. Raw output: {raw!r}")]

        path = parsed["path"]

        # UE renders the screenshot on a subsequent editor tick, so poll the
        # filesystem from the MCP-side process (outside UE's main thread).
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    # Let UE finish writing
                    await asyncio.sleep(0.1)
                    break
            except OSError:
                pass
            await asyncio.sleep(0.2)

        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return [TextContent(
                type="text",
                text=(
                    f"capture_viewport: timed out waiting for screenshot at {path}. "
                    "If the editor is in a modal dialog or no viewport has focus, "
                    "the screenshot request may be silently dropped."
                ),
            )]

        with open(path, "rb") as fh:
            data = fh.read()

        b64 = base64.b64encode(data).decode("ascii")
        return [
            ImageContent(type="image", data=b64, mimeType="image/png"),
            TextContent(type="text", text=f"captured {len(data)} bytes → {path}"),
        ]
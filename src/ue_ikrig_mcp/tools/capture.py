"""Viewport capture tool for the ue-ikrig MCP.

Takes a screenshot of the active Unreal Editor viewport and returns it inline
as an MCP ImageContent so the calling LLM can see the result of an edit
without a human having to screenshot and paste.
"""

import asyncio
import json
import os
import time

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script
from .capture_common import wait_for_stable_file, png_payload, parse_mcp_result


def register(server):
    @server.tool()
    async def capture_viewport(
        width: int = 1280,
        height: int = 720,
        force_realtime: bool = True,
        settle_ms: int = 150,
    ) -> list:
        """Capture the active Unreal Editor viewport to a PNG and return it inline.

        Writes the image to `<ProjectSaved>/Screenshots/Claude/capture_<ts>.png`
        via `unreal.AutomationLibrary.take_high_res_screenshot`, then reads it back
        and returns it as an MCP ImageContent. Useful after IK rig / retargeter
        tuning changes — the caller can see the updated preview without needing a
        human screenshot in the loop.

        Args:
            width: Capture width in pixels (default 1280).
            height: Capture height in pixels (default 720).
            force_realtime: Call editor_set_viewport_realtime + editor_invalidate_viewports
                before capturing to ensure the viewport renders. Default True.
            settle_ms: Milliseconds to sleep before issuing the screenshot command,
                giving Slate time to repaint after realtime is forced. Default 150.
        """
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return [TextContent(type="text", text=str(e))]

        ts_ms = int(time.time() * 1000)

        script = wrap_script(
            "import unreal\n"
            "import os as _os\n"
            "import json as _json\n"
            f"_w, _h = {int(width)}, {int(height)}\n"
            "_proj_saved = unreal.Paths.project_saved_dir()\n"
            "_out_dir = _os.path.join(_proj_saved, 'Screenshots', 'Claude')\n"
            "_os.makedirs(_out_dir, exist_ok=True)\n"
            f"_ts = {ts_ms}\n"
            "_fname = f'capture_{_ts}.png'\n"
            "_full_path = _os.path.abspath(_os.path.join(_out_dir, _fname))\n"
            "_sidecar = _os.path.join(_out_dir, f'result_{_ts}.json')\n"
            + (
                "_subsystem = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
                "if _subsystem:\n"
                "    try:\n"
                "        _subsystem.editor_set_viewport_realtime(True, False)\n"
                "        _subsystem.editor_invalidate_viewports()\n"
                "    except Exception:\n"
                "        pass\n"
                if force_realtime else ""
            )
            + "unreal.AutomationLibrary.take_high_res_screenshot(_w, _h, _full_path)\n"
            "_sidecar_data = {'path': _full_path.replace('\\\\', '/')}\n"
            "with open(_sidecar, 'w') as _f:\n"
            "    _json.dump(_sidecar_data, _f)\n"
            "print('__MCP_RESULT__' + _json.dumps(_sidecar_data))\n"
        )

        t0 = time.monotonic()

        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)

        result = conn.execute(script)

        # Prefer sidecar JSON for path resolution — avoids string-parsing UE stdout
        path = None

        project_dir = os.environ.get("UE_PROJECT_DIR", "")
        if project_dir:
            sidecar_path = os.path.join(project_dir, "Saved", "Screenshots", "Claude", f"result_{ts_ms}.json")
            try:
                with open(sidecar_path) as fh:
                    sc = json.load(fh)
                path = sc.get("path")
            except (OSError, json.JSONDecodeError):
                pass

        # Fall back to parsing __MCP_RESULT__ from stdout
        if not path:
            parsed = parse_mcp_result(result)
            if parsed and isinstance(parsed, dict):
                path = parsed.get("path")

        # Last resort: construct the expected path from env var
        if not path and project_dir:
            path = os.path.join(project_dir, "Saved", "Screenshots", "Claude", f"capture_{ts_ms}.png")

        if not path:
            raw = result.get("output") if isinstance(result, dict) else str(result)
            return [TextContent(type="text", text=f"capture_viewport: could not resolve screenshot path. Raw: {raw!r}")]

        # Normalize Windows backslashes from UE
        path = path.replace("/", os.sep)

        ready = await wait_for_stable_file(path, timeout_s=10.0)
        if not ready:
            return [TextContent(type="text", text=(
                f"capture_viewport: timed out waiting for screenshot at {path}. "
                "If the editor is in a modal dialog or no viewport has focus, "
                "the screenshot request may be silently dropped."
            ))]

        elapsed_s = round(time.monotonic() - t0, 3)
        return png_payload(path, extra_text={
            "elapsed_s": elapsed_s,
            "realtime_forced": force_realtime,
            "settled_ms": settle_ms,
        })

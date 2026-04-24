"""OS-level window screenshot for UE asset editors.

Captures the Unreal Editor window (or a torn-off asset-editor tool window) from
the Windows desktop buffer. Unlike ``capture_viewport`` — which routes through
``unreal.AutomationLibrary.take_high_res_screenshot`` and only captures the main
level-editor viewport — this tool captures pixel-for-pixel whatever the user
sees in the named window, so asset-editor viewports like the IK Retargeter
preview come through correctly.

Windows-only (uses ``user32`` via ``ctypes`` plus ``mss`` for the grab).
"""

import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import sys

from mcp.types import ImageContent, TextContent


def _find_visible_window(title_match: str):
    """First visible top-level window whose title contains `title_match`
    (case-insensitive). Returns (hwnd, actual_title) or None."""
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    found = [None]
    needle = title_match.lower()

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if needle in buf.value.lower():
            found[0] = (hwnd, buf.value)
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return found[0]


def _window_bbox(hwnd, client_only: bool):
    """Return (left, top, width, height) in screen coords."""
    user32 = ctypes.windll.user32
    if client_only:
        rect = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y, rect.right, rect.bottom
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _raise_window(hwnd):
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)


def register(server):
    @server.tool()
    async def capture_ue_window(
        title_match: str = "Unreal Editor",
        client_only: bool = True,
        foreground: bool = True,
        settle_ms: int = 250,
    ) -> list:
        """Capture an Unreal Editor window via OS-level screenshot.

        Complements ``capture_viewport`` (which only captures the main level
        viewport). Use this when an asset editor like the IK Retargeter preview
        has focus and ``capture_viewport`` returns a black image.

        Args:
            title_match: Case-insensitive substring of the window title to match.
                Default 'Unreal Editor' matches the main UE window. Pass an asset
                name (e.g. 'RTG_Body') to target a torn-off asset editor window.
                If an asset editor is tabbed into the main window, pass the main
                window title — the whole window is captured including that tab.
            client_only: Capture only the client area (no titlebar/border). Default True.
            foreground: Restore/raise the window before capture. Default True.
            settle_ms: Milliseconds to wait after raising before capturing, so Slate
                has time to repaint. Default 250.
        """
        if sys.platform != "win32":
            return [TextContent(type="text", text=f"capture_ue_window: only supported on Windows, not {sys.platform}")]

        # Physical-pixel coordinates regardless of display scaling.
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        try:
            import mss
            import mss.tools
        except ImportError as e:
            return [TextContent(type="text", text=(
                f"capture_ue_window: required dependency 'mss' is not installed ({e}). "
                "Reinstall the MCP server so its pyproject deps resolve."
            ))]

        match = _find_visible_window(title_match)
        if match is None:
            return [TextContent(type="text", text=f"capture_ue_window: no visible window matched title {title_match!r}")]
        hwnd, title = match

        if foreground:
            try:
                _raise_window(hwnd)
            except Exception:
                pass
            await asyncio.sleep(max(0, settle_ms) / 1000.0)

        left, top, w, h = _window_bbox(hwnd, client_only=client_only)
        if w <= 0 or h <= 0:
            return [TextContent(type="text", text=(
                f"capture_ue_window: window rect is empty ({w}x{h}); window may be minimized or offscreen"
            ))]

        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
            png_bytes = mss.tools.to_png(shot.rgb, shot.size)

        b64 = base64.b64encode(png_bytes).decode("ascii")
        return [
            ImageContent(type="image", data=b64, mimeType="image/png"),
            TextContent(type="text", text=(
                f"captured {len(png_bytes)} bytes from {title!r} (hwnd={hwnd}, {w}x{h} @ {left},{top})"
            )),
        ]

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
import ctypes
import ctypes.wintypes as wt
import json
import sys

from mcp.types import TextContent

from .capture_common import png_to_image_content

# Win32 constants
SW_RESTORE = 9
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY = 0x00CC0020

if sys.platform == "win32":
    _u32 = ctypes.windll.user32
    _gdi = ctypes.windll.gdi32

    # Prevent 64-bit HANDLE truncation: default restype is c_int (32-bit)
    _u32.GetDC.restype = wt.HDC
    _u32.GetDC.argtypes = [wt.HWND]
    _u32.ReleaseDC.restype = ctypes.c_int
    _u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _u32.PrintWindow.restype = wt.BOOL
    _u32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    _u32.IsWindowVisible.restype = wt.BOOL
    _u32.IsWindowVisible.argtypes = [wt.HWND]
    _u32.IsIconic.restype = wt.BOOL
    _u32.IsIconic.argtypes = [wt.HWND]
    _u32.BringWindowToTop.restype = wt.BOOL
    _u32.BringWindowToTop.argtypes = [wt.HWND]
    _u32.SetForegroundWindow.restype = wt.BOOL
    _u32.SetForegroundWindow.argtypes = [wt.HWND]
    _u32.ShowWindow.restype = wt.BOOL
    _u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    _u32.GetClientRect.restype = wt.BOOL
    _u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _u32.GetWindowRect.restype = wt.BOOL
    _u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _u32.ClientToScreen.restype = wt.BOOL
    _u32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    _u32.GetWindowTextLengthW.restype = ctypes.c_int
    _u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    _u32.GetWindowTextW.restype = ctypes.c_int
    _u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

    _EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _u32.EnumWindows.restype = wt.BOOL
    _u32.EnumWindows.argtypes = [_EnumWindowsProc, wt.LPARAM]
    _u32.EnumChildWindows.restype = wt.BOOL
    _u32.EnumChildWindows.argtypes = [wt.HWND, _EnumWindowsProc, wt.LPARAM]

    _HGDIOBJ = ctypes.c_void_p
    _gdi.CreateCompatibleDC.restype = wt.HDC
    _gdi.CreateCompatibleDC.argtypes = [wt.HDC]
    _gdi.CreateCompatibleBitmap.restype = _HGDIOBJ
    _gdi.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _gdi.SelectObject.restype = _HGDIOBJ
    _gdi.SelectObject.argtypes = [wt.HDC, _HGDIOBJ]
    _gdi.DeleteObject.restype = wt.BOOL
    _gdi.DeleteObject.argtypes = [_HGDIOBJ]
    _gdi.DeleteDC.restype = wt.BOOL
    _gdi.DeleteDC.argtypes = [wt.HDC]
    _gdi.BitBlt.restype = wt.BOOL
    _gdi.BitBlt.argtypes = [
        wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD,
    ]
    _gdi.GetDIBits.restype = ctypes.c_int
    _gdi.GetDIBits.argtypes = [
        wt.HDC, _HGDIOBJ, wt.UINT, wt.UINT,
        ctypes.c_void_p, ctypes.c_void_p, wt.UINT,
    ]


def _find_visible_window(title_match: str):
    """First visible top-level window whose title contains `title_match`
    (case-insensitive). Returns (hwnd, actual_title) or None."""
    found = [None]
    needle = title_match.lower()

    def _cb(hwnd, _lparam):
        if not _u32.IsWindowVisible(hwnd):
            return True
        n = _u32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, buf, n + 1)
        if needle in buf.value.lower():
            found[0] = (hwnd, buf.value)
            return False
        return True

    _u32.EnumWindows(_EnumWindowsProc(_cb), 0)
    return found[0]


def _list_visible_window_titles(max_results: int = 5) -> list[str]:
    """Return up to max_results visible top-level window titles for error messages."""
    titles = []

    def _cb(hwnd, _lparam):
        if not _u32.IsWindowVisible(hwnd):
            return True
        n = _u32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, buf, n + 1)
        titles.append(buf.value)
        return len(titles) < max_results

    _u32.EnumWindows(_EnumWindowsProc(_cb), 0)
    return titles


def _window_bbox(hwnd, client_only: bool):
    """Return (left, top, width, height) in screen coords."""
    rect = wt.RECT()
    if client_only:
        _u32.GetClientRect(hwnd, ctypes.byref(rect))
        pt = wt.POINT(0, 0)
        _u32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y, rect.right - rect.left, rect.bottom - rect.top
    _u32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _raise_window(hwnd):
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, SW_RESTORE)
    _u32.BringWindowToTop(hwnd)
    _u32.SetForegroundWindow(hwnd)


def _find_child_tab(hwnd, tab_match: str):
    """Enumerate child windows and return bbox of first one whose title contains tab_match.

    Returns (left, top, width, height) in screen coords, or None.
    """
    needle = tab_match.lower()
    found = [None]

    def _cb(child_hwnd, _lparam):
        n = _u32.GetWindowTextLengthW(child_hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(child_hwnd, buf, n + 1)
        if needle in buf.value.lower():
            rect = wt.RECT()
            _u32.GetWindowRect(child_hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 0 and h > 0:
                found[0] = (rect.left, rect.top, w, h)
                return False
        return True

    _u32.EnumChildWindows(hwnd, _EnumWindowsProc(_cb), 0)
    return found[0]


def _intersect_rects(r1, r2):
    """Intersect two (left, top, w, h) rects. Returns intersected rect or None."""
    l1, t1, w1, h1 = r1
    l2, t2, w2, h2 = r2
    left = max(l1, l2)
    top = max(t1, t2)
    right = min(l1 + w1, l2 + w2)
    bottom = min(t1 + h1, t2 + h2)
    if right > left and bottom > top:
        return left, top, right - left, bottom - top
    return None


def _mean_luminance(rgb_bytes: bytes, w: int, h: int) -> float:
    """Compute mean luminance (0-255) from raw RGB bytes."""
    n = w * h
    if n == 0:
        return 0.0
    total = sum(rgb_bytes[i] * 299 + rgb_bytes[i + 1] * 587 + rgb_bytes[i + 2] * 114
                for i in range(0, len(rgb_bytes), 3))
    return total / (1000.0 * n)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _printwindow_capture(hwnd, left: int, top: int, w: int, h: int) -> bytes | None:
    """Render window into offscreen DC via PrintWindow and return PNG bytes or None."""
    hdc_screen = None
    hdc_mem = None
    hbm = None
    hdc_pw = None
    hbm_pw = None
    png_bytes = None

    try:
        hdc_screen = _u32.GetDC(None)
        hdc_mem = _gdi.CreateCompatibleDC(hdc_screen)
        hbm = _gdi.CreateCompatibleBitmap(hdc_screen, w, h)
        _gdi.SelectObject(hdc_mem, hbm)

        win_rect = wt.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(win_rect))
        win_w = win_rect.right - win_rect.left
        win_h = win_rect.bottom - win_rect.top

        hdc_pw = _gdi.CreateCompatibleDC(hdc_screen)
        hbm_pw = _gdi.CreateCompatibleBitmap(hdc_screen, win_w, win_h)
        _gdi.SelectObject(hdc_pw, hbm_pw)

        if not _u32.PrintWindow(hwnd, hdc_pw, PW_RENDERFULLCONTENT):
            return None

        src_x = left - win_rect.left
        src_y = top - win_rect.top
        _gdi.BitBlt(hdc_mem, 0, 0, w, h, hdc_pw, src_x, src_y, SRCCOPY)

        bih = _BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = -h  # negative = top-down
        bih.biPlanes = 1
        bih.biBitCount = 24
        bih.biCompression = BI_RGB
        row_bytes = (w * 3 + 3) & ~3
        buf_size = row_bytes * h
        buf = (ctypes.c_uint8 * buf_size)()
        _gdi.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bih), DIB_RGB_COLORS)

        # GetDIBits returns BGR; convert to RGB
        raw = bytes(buf)
        rgb = bytearray(w * h * 3)
        for row in range(h):
            src_off = row * row_bytes
            dst_off = row * w * 3
            for col in range(w):
                s = src_off + col * 3
                d = dst_off + col * 3
                rgb[d] = raw[s + 2]
                rgb[d + 1] = raw[s + 1]
                rgb[d + 2] = raw[s]

        import mss.tools
        png_bytes = mss.tools.to_png(bytes(rgb), (w, h))

    except Exception:
        png_bytes = None

    finally:
        if hbm_pw:
            _gdi.DeleteObject(hbm_pw)
        if hdc_pw:
            _gdi.DeleteDC(hdc_pw)
        if hbm:
            _gdi.DeleteObject(hbm)
        if hdc_mem:
            _gdi.DeleteDC(hdc_mem)
        if hdc_screen:
            _u32.ReleaseDC(None, hdc_screen)

    return png_bytes


async def _capture_window_by_title(
    title_match: str,
    client_only: bool = True,
    foreground: bool = True,
    settle_ms: int = 250,
    tab_match: str | None = None,
    method: str = "auto",
    extra_text: dict | None = None,
) -> list:
    """Core capture implementation — importable by other tools (e.g. capture_asset_editor).

    extra_text keys are merged into the TextContent JSON response (lets callers
    inject metadata like asset_name/asset_class without round-tripping JSON).
    """
    if sys.platform != "win32":
        return [TextContent(type="text", text=f"capture_ue_window: only supported on Windows, not {sys.platform}")]

    try:
        _u32.SetProcessDPIAware()
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
        candidates = _list_visible_window_titles(5)
        cand_str = ", ".join(repr(t) for t in candidates) if candidates else "(none found)"
        return [TextContent(type="text", text=(
            f"capture_ue_window: no visible window matched title {title_match!r}. "
            f"Visible windows (up to 5): {cand_str}"
        ))]
    hwnd, title = match

    is_iconic = bool(_u32.IsIconic(hwnd))

    if foreground:
        try:
            _raise_window(hwnd)
        except Exception:
            pass
        await asyncio.sleep(max(0, settle_ms) / 1000.0)
    # When foreground=False and window is minimized, attempt PrintWindow directly

    left, top, w, h = _window_bbox(hwnd, client_only=client_only)

    if w <= 0 or h <= 0:
        if is_iconic:
            return [TextContent(type="text", text=(
                f"capture_ue_window: window {title!r} is minimized and offscreen — "
                "pass foreground=True to restore it before capturing."
            ))]
        return [TextContent(type="text", text=(
            f"capture_ue_window: window rect is empty ({w}x{h}); window may be minimized or offscreen"
        ))]

    # Narrow to tab area if tab_match provided
    tab_match_resolved = False
    if tab_match:
        child_bbox = _find_child_tab(hwnd, tab_match)
        if child_bbox:
            intersected = _intersect_rects((left, top, w, h), child_bbox)
            if intersected:
                left, top, w, h = intersected
                tab_match_resolved = True

    capture_method = "mss"
    png_bytes: bytes | None = None

    use_printwindow = (method == "printwindow") or (not foreground and is_iconic)

    if not use_printwindow:
        with mss.mss() as sct:
            shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
            raw_rgb = shot.rgb
            shot_size = shot.size

        # Black-frame detection (only when client_only=True to avoid titlebar bias)
        if client_only and method != "mss" and _mean_luminance(raw_rgb, w, h) < 5.0:
            if foreground:
                # Retry once after extra settle
                await asyncio.sleep(max(0, settle_ms * 2) / 1000.0)
                with mss.mss() as sct2:
                    shot2 = sct2.grab({"left": left, "top": top, "width": w, "height": h})
                    raw_rgb = shot2.rgb
                    shot_size = shot2.size
                if _mean_luminance(raw_rgb, w, h) < 5.0:
                    use_printwindow = True
            else:
                use_printwindow = True

        if not use_printwindow:
            png_bytes = mss.tools.to_png(raw_rgb, shot_size)

    if use_printwindow or (method == "printwindow"):
        pw_bytes = _printwindow_capture(hwnd, left, top, w, h)
        if pw_bytes:
            png_bytes = pw_bytes
            capture_method = "printwindow"

    if png_bytes is None:
        return [TextContent(type="text", text=(
            f"capture_ue_window: failed to capture window {title!r}"
        ))]

    info: dict = {
        "captured_bytes": len(png_bytes),
        "title": title,
        "hwnd": hwnd,
        "size": f"{w}x{h}",
        "origin": f"{left},{top}",
        "method": capture_method,
    }
    if tab_match:
        info["tab_match"] = tab_match
        info["tab_match_resolved"] = tab_match_resolved
    if extra_text:
        info.update(extra_text)

    return [
        png_to_image_content(png_bytes),
        TextContent(type="text", text=json.dumps(info)),
    ]


def register(server):
    @server.tool()
    async def capture_ue_window(
        title_match: str = "Unreal Editor",
        client_only: bool = True,
        foreground: bool = True,
        settle_ms: int = 250,
        tab_match: str | None = None,
        method: str = "auto",
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
                When False, captures without changing window z-order; uses PrintWindow
                for minimized or occluded windows.
            settle_ms: Milliseconds to wait after raising before capturing, so Slate
                has time to repaint. Default 250.
            tab_match: Optional case-insensitive substring to match a child tab/widget
                title. When set, narrows the capture bbox to that tab area.
            method: 'auto' (default), 'mss' (force screen grab), or 'printwindow'
                (force off-screen render). Auto uses mss first and falls back to
                PrintWindow when the result is black/near-black.
        """
        return await _capture_window_by_title(
            title_match=title_match,
            client_only=client_only,
            foreground=foreground,
            settle_ms=settle_ms,
            tab_match=tab_match,
            method=method,
        )

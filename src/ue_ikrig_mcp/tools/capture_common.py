"""Shared helpers for all capture tools — pure Python, no UE deps."""

import asyncio
import base64
import json
import os
import time

from mcp.types import ImageContent, TextContent


async def wait_for_stable_file(
    path: str,
    timeout_s: float = 15.0,
    stable_checks: int = 2,
    poll_s: float = 0.2,
) -> bool:
    """Return True when path exists, size>0, and size unchanged for stable_checks consecutive polls."""
    deadline = time.monotonic() + timeout_s
    stable = 0
    last_size = -1
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
            if size > 0:
                if size == last_size:
                    stable += 1
                    if stable >= stable_checks:
                        return True
                else:
                    stable = 0
                last_size = size
        except OSError:
            stable = 0
            last_size = -1
        await asyncio.sleep(poll_s)
    return False


def png_to_image_content(data: bytes) -> ImageContent:
    """Wrap raw PNG bytes as an MCP ImageContent."""
    return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")


def png_payload(path: str, extra_text: dict | None = None) -> list:
    """Read a PNG file and return [ImageContent, TextContent(json)].

    extra_text keys are merged into the TextContent JSON dict alongside
    captured_bytes and path.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    info: dict = {"captured_bytes": len(data), "path": path}
    if extra_text:
        info.update(extra_text)
    return [
        png_to_image_content(data),
        TextContent(type="text", text=json.dumps(info)),
    ]


def parse_mcp_result(stdout_or_dict) -> dict | None:
    """Extract the JSON dict that UE scripts emit via print('__MCP_RESULT__' + json.dumps(...)).

    Handles str, dict with 'output'/'result' keys, and Python-repr wrapping
    that the UE bridge sometimes applies.
    """
    MARKER = "__MCP_RESULT__"

    if isinstance(stdout_or_dict, dict):
        combined = (stdout_or_dict.get("output") or "") + (stdout_or_dict.get("result") or "")
    else:
        combined = str(stdout_or_dict)

    idx = combined.find(MARKER)
    if idx == -1:
        return None

    tail = combined[idx + len(MARKER):].strip()

    # Find the JSON object/array bounds
    for start, (open_c, close_c) in enumerate([('{', '}'), ('[', ']')]):
        pos = tail.find(open_c)
        if pos != -1:
            # Scan for matching close
            depth = 0
            for i, ch in enumerate(tail[pos:], pos):
                if ch == open_c:
                    depth += 1
                elif ch == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(tail[pos : i + 1])
                        except json.JSONDecodeError:
                            break
            break

    # Last resort: try parsing the whole tail
    try:
        return json.loads(tail)
    except json.JSONDecodeError:
        return None


def resolve_screenshot_path(ts_ms: int, project_dir: str | None = None) -> str:
    """Return the Windows path for capture_<ts_ms>.png under <ProjectSaved>/Screenshots/Claude/.

    Uses project_dir if provided, else falls back to UE_PROJECT_DIR env var.
    """
    base = project_dir or os.environ.get("UE_PROJECT_DIR", "")
    return os.path.join(base, "Saved", "Screenshots", "Claude", f"capture_{ts_ms}.png")

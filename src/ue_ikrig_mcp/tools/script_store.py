"""Saved UE Python script store (4 tools).

Token economy: the driver pays the generation cost of a script once
(save_script), then replays it for a few dozen tokens (run_script) with
JSON parameters exposed to the script as the ARGS dict. Scripts persist on
disk across sessions.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from mcp.types import TextContent

from ..ue_connection import (
    get_connection,
    UEMultipleEditorsAmbiguousError,
    UENotRunningError,
    UEConnectionError,
    _script_syntax_preflight,
)
from ..script_exec import (
    add_line_offset_hint,
    ensure_connected,
    prepare_user_code,
    shape_result,
)

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_DESCRIPTION_PREFIX = "# description:"


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    if isinstance(msg, dict):
        return _ok(msg)
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def _script_dir() -> Path:
    configured = os.environ.get("UE_MCP_SCRIPT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ue_ikrig_mcp" / "scripts"


def _script_path(name: str) -> Optional[Path]:
    if not _NAME_RE.fullmatch(name):
        return None
    return _script_dir() / f"{name}.py"


def _read_description(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as f:
            first_line = f.readline().rstrip("\n")
    except OSError:
        return ""
    if first_line.startswith(_DESCRIPTION_PREFIX):
        return first_line[len(_DESCRIPTION_PREFIX):].strip()
    return ""


def _strip_description(code: str) -> str:
    lines = code.split("\n")
    if lines and lines[0].startswith(_DESCRIPTION_PREFIX):
        return "\n".join(lines[1:])
    return code


def register(server, connection=None):

    def _conn():
        return connection if connection is not None else get_connection()

    @server.tool(
        name="save_script",
        description=(
            "Save a reusable UE Python script under a name for later run_script calls "
            "(persisted on disk across sessions). Write the script once, replay it cheaply: "
            "reference runtime parameters via the ARGS dict (injected by run_script), use the "
            "pre-defined helpers load()/mcp_result()/subsys()/asset_registry(), and end with "
            "mcp_result(...). Overwrites any existing script with the same name."
        ),
    )
    async def save_script(
        name: str,
        code: str,
        description: str = "",
    ) -> list[TextContent]:
        path = _script_path(name)
        if path is None:
            return _err(
                f"Invalid script name {name!r}: use 1-64 chars of letters, digits, '_' or '-' "
                "(starting with a letter or digit)."
            )
        preflight_failure = _script_syntax_preflight(code, "ExecuteFile")
        if preflight_failure is not None:
            return _ok(preflight_failure)
        # run_script always injects ARGS + helpers above the stored code, so a
        # __future__ import could never stay the first statement at run time.
        if any(line.lstrip().startswith("from __future__") for line in code.splitlines()):
            return _err(
                "Saved scripts cannot use 'from __future__' imports: run_script "
                "injects the ARGS line and helper prelude above your code, which "
                "would make the import illegal. Use execute_python with "
                "inject_helpers=False for such scripts."
            )
        replaced = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"{_DESCRIPTION_PREFIX} {description.strip()}\n" if description.strip() else ""
        path.write_text(header + code, encoding="utf-8")
        return _ok({
            "saved": True,
            "name": name,
            "replaced": replaced,
            "path": str(path),
            "chars": len(code),
        })

    @server.tool(
        name="run_script",
        description=(
            "Run a script previously stored with save_script inside Unreal Engine. "
            "args (a JSON object) is exposed to the script as the ARGS dict. "
            "Auto-connects; results are shaped like execute_python (parsed/hints, compact "
            "raw-output omission, max_output_chars truncation)."
        ),
    )
    async def run_script(
        name: str,
        args: Optional[dict] = None,
        timeout_seconds: Optional[float] = None,
        compact: bool = True,
        max_output_chars: int = 8000,
    ) -> list[TextContent]:
        path = _script_path(name)
        if path is None:
            return _err(f"Invalid script name {name!r}.")
        if not path.exists():
            return _err(
                f"No saved script named {name!r}. Use list_scripts to see what exists, "
                "or save_script to create it."
            )
        code = _strip_description(path.read_text(encoding="utf-8"))

        try:
            conn = _conn()
        except UENotRunningError as e:
            return _err(str(e))
        connect_error = ensure_connected(conn)
        if connect_error is not None:
            return _err(connect_error)

        args_json = json.dumps(args if args is not None else {}, ensure_ascii=True)
        script = (
            f"ARGS = __import__('json').loads({args_json!r})\n"
            + code
        )
        try:
            result = conn.execute(
                prepare_user_code(script, "ExecuteFile", True),
                mode="ExecuteFile",
                timeout=timeout_seconds,
            )
        except UEMultipleEditorsAmbiguousError as e:
            return _ok(e.to_payload())
        except UEConnectionError as e:
            return _err(str(e))
        if result is None:
            return _ok({"success": True})
        # extra_offset=1 accounts for the injected ARGS line.
        add_line_offset_hint(result, extra_offset=1)
        return _ok(shape_result(result, max_output_chars=max_output_chars, compact=compact))

    @server.tool(
        name="list_scripts",
        description="List saved UE Python scripts (name, description, size) available to run_script.",
    )
    async def list_scripts() -> list[TextContent]:
        directory = _script_dir()
        scripts = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.py")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                scripts.append({
                    "name": path.stem,
                    "description": _read_description(path),
                    "chars": stat.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                })
        return _ok({"scripts": scripts, "directory": str(directory)})

    @server.tool(
        name="delete_script",
        description="Delete a saved UE Python script by name.",
    )
    async def delete_script(name: str) -> list[TextContent]:
        path = _script_path(name)
        if path is None:
            return _err(f"Invalid script name {name!r}.")
        if not path.exists():
            return _err(f"No saved script named {name!r}.")
        path.unlink()
        return _ok({"deleted": True, "name": name})

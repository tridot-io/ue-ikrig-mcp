"""Shared execution support for driver-authored UE Python scripts.

Token economy for MCP drivers: the prelude removes per-script boilerplate
(imports, load guards, sentinel printing), and result shaping bounds the raw
output echoed back into the driver's context.
"""

from typing import Any, Optional

from .ue_connection import UENotRunningError, UEConnectionError

# Prepended to every ExecuteFile-mode driver script (execute_python and
# run_script) unless inject_helpers=False. Keep it small and side-effect free:
# every byte here crosses the transport on every call.
EXECUTE_PYTHON_PRELUDE = '''\
import json
import unreal

def load(path):
    """Load an asset or raise with the offending path."""
    _a = unreal.load_asset(path)
    if _a is None:
        raise ValueError("Asset not found: %s" % (path,))
    return _a

def mcp_result(payload):
    """Emit the __MCP_RESULT__ sentinel; unreal types coerce via str()."""
    print("__MCP_RESULT__" + json.dumps(payload, default=str))

def subsys(cls):
    """Shorthand for unreal.get_editor_subsystem(cls)."""
    return unreal.get_editor_subsystem(cls)

def asset_registry():
    return unreal.AssetRegistryHelpers.get_asset_registry()
'''

_TRUNCATION_HEAD_CHARS = 1000

# Lines the prelude (plus its joining newline) adds ahead of user code.
PRELUDE_LINE_OFFSET = EXECUTE_PYTHON_PRELUDE.count("\n") + 1


def add_line_offset_hint(result: dict, extra_offset: int = 0) -> dict:
    """On failure, tell the driver how far traceback line numbers are shifted."""
    if isinstance(result, dict) and not result.get("success"):
        offset = PRELUDE_LINE_OFFSET + extra_offset
        hints = result.setdefault("hints", [])
        hints.append(
            f"Traceback line numbers include {offset} injected helper lines; "
            f"your code starts at line {offset + 1}."
        )
    return result


def prepare_user_code(code: str, mode: str, inject_helpers: bool) -> str:
    """Return the script to ship to Unreal, with the prelude when applicable.

    Only ExecuteFile mode gets the prelude: the statement/expression modes
    compile a single unit where prepended definitions are invalid.
    """
    if not inject_helpers or mode != "ExecuteFile":
        return code
    return EXECUTE_PYTHON_PRELUDE + "\n" + code


def ensure_connected(conn) -> Optional[str]:
    """Auto-connect a disconnected UEConnection. Returns an error message or None."""
    if (
        hasattr(conn, "is_connected")
        and hasattr(conn, "connect")
        and not conn.is_connected()
    ):
        try:
            conn.connect()
        except (UENotRunningError, UEConnectionError) as e:
            return (
                f"Auto-connect to Unreal Editor failed: {e} "
                "Run preflight_discovery to diagnose the transport."
            )
    return None


def _truncate(text: str, limit: int) -> str:
    """Keep the head and tail of oversized text; errors/tracebacks live at the end."""
    if limit <= 0 or len(text) <= limit:
        return text
    if limit < 64:
        # Below the truncation-marker overhead a head+tail split would return
        # more than the limit itself; hard-slice instead.
        return text[:limit]
    tail_budget = max(limit - _TRUNCATION_HEAD_CHARS, limit // 2)
    head_budget = max(limit - tail_budget, 0)
    return (
        text[:head_budget]
        + f"\n...[truncated {len(text) - head_budget - tail_budget} of {len(text)} chars]...\n"
        + text[len(text) - tail_budget:]
    )


def shape_result(
    result: dict[str, Any],
    *,
    max_output_chars: int = 8000,
    compact: bool = True,
) -> dict[str, Any]:
    """Bound the tokens a script result echoes back into the driver's context.

    - compact: when the script succeeded AND returned structured data via the
      __MCP_RESULT__ sentinel, the raw output/result echo is redundant — drop
      it and keep only `parsed` (plus hints and byte counts).
    - max_output_chars (0 = unlimited): truncate the raw echo otherwise,
      preserving head and tail (tracebacks are at the end).
    """
    if not isinstance(result, dict):
        return result
    shaped = dict(result)
    output = shaped.get("output")
    result_str = shaped.get("result")
    if (
        compact
        and shaped.get("success")
        and shaped.get("parsed") is not None
    ):
        shaped["output"] = ""
        shaped["result"] = ""
        shaped["raw_omitted"] = {
            "reason": "structured parsed result present (compact mode)",
            "output_chars": len(output) if isinstance(output, str) else 0,
            "result_chars": len(result_str) if isinstance(result_str, str) else 0,
        }
        return shaped
    if max_output_chars > 0:
        if isinstance(output, str):
            shaped["output"] = _truncate(output, max_output_chars)
        if isinstance(result_str, str):
            shaped["result"] = _truncate(result_str, max_output_chars)
    return shaped

"""Unreal Python API catalogue tools (3 tools).

Find engine APIs without editor round-trips or dir(unreal) token dumps: the
catalogue is harvested once per engine version into a local file, then
searched lexically (BM25) in milliseconds.
"""

import json
import re

from mcp.types import TextContent

from .. import api_index
from ..ue_connection import (
    get_connection,
    UEMultipleEditorsAmbiguousError,
    UENotRunningError,
    UEConnectionError,
    _wsl_path_to_windows,
)
from ..ue_scripts import escape_string, wrap_script
from ..script_exec import ensure_connected

_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,200}")


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    if isinstance(msg, dict):
        return _ok(msg)
    return [
        TextContent(
            type="text", text=json.dumps({"error": True, "message": msg}, indent=2)
        )
    ]


_NO_CATALOG_MSG = (
    "No API catalogue exists yet and the editor is not connected to build one. "
    "Connect (connect_to_editor) and search again - the catalogue harvests "
    "automatically on first use - or run build_api_catalog explicitly."
)


def _build_catalog(conn, force: bool, timeout_seconds: float) -> tuple[bool, dict]:
    """Version-probe, harvest, and install the catalogue file.

    Shared by the explicit build_api_catalog tool and the build-on-first-miss
    path in search/describe. Returns (success, response_payload)."""
    try:
        version_result = conn.execute(api_index.ENGINE_VERSION_SCRIPT)
    except UEMultipleEditorsAmbiguousError as e:
        return False, e.to_payload()
    except UEConnectionError as e:
        return False, {"error": True, "message": str(e)}
    parsed = version_result.get("parsed") or {}
    engine_version = parsed.get("engine")
    if not engine_version:
        return False, version_result
    final_path = api_index.catalog_path_for_version(engine_version)
    if final_path.exists() and not force:
        meta, _entries, _index = api_index._load_indexed(final_path)
        return True, {"built": False, "cached": True, **meta}
    final_path.parent.mkdir(parents=True, exist_ok=True)
    # Version-scoped tmp name so concurrent builds for different engine
    # versions cannot clobber each other's harvest before the rename.
    tmp_path = final_path.with_name(f"_harvest_tmp_{final_path.name}")
    target_windows = _wsl_path_to_windows(str(tmp_path))
    try:
        harvest_result = conn.execute(
            api_index.build_harvest_script(target_windows),
            timeout=timeout_seconds,
        )
    except UEMultipleEditorsAmbiguousError as e:
        return False, e.to_payload()
    except UEConnectionError as e:
        return False, {"error": True, "message": str(e)}
    harvest_parsed = harvest_result.get("parsed") or {}
    if not harvest_result.get("success") or harvest_parsed.get("error"):
        return False, harvest_result
    if not tmp_path.exists():
        return False, {
            "error": True,
            "message": (
                f"The editor reported success but {tmp_path} did not appear on this "
                f"side. The editor wrote to {target_windows!r}; check that the path "
                "is reachable from Windows, or point UE_MCP_CATALOG_DIR at a "
                "directory both sides share."
            ),
        }
    tmp_path.replace(final_path)
    meta, _entries, _index = api_index._load_indexed(final_path)
    return True, {"built": True, "cached": False, **meta}


def register(server, connection=None):

    def _conn():
        return connection if connection is not None else get_connection()

    @server.tool(
        name="build_api_catalog",
        description=(
            "Harvest the live editor's unreal Python API (classes, methods, properties "
            "with signatures and doc summaries) plus the project's own types from the "
            "asset registry (Blueprints, widgets, anim BPs, material assets/functions/"
            "instances, user structs/enums, data assets) into a local catalogue file, "
            "keyed by engine version. "
            "search_unreal_api runs this automatically on first use when the editor "
            "is connected, so the explicit call is only needed with force=true - "
            "after engine upgrades, plugin changes, or when new project assets "
            "should be searchable. Skips harvesting when a catalogue for the "
            "current engine version already exists unless force=true. Blocks the "
            "editor game thread for a few seconds."
        ),
    )
    async def build_api_catalog(
        force: bool = False,
        timeout_seconds: float = 240.0,
    ) -> list[TextContent]:
        try:
            conn = _conn()
        except UENotRunningError as e:
            return _err(str(e))
        connect_error = ensure_connected(conn)
        if connect_error is not None:
            return _err(connect_error)
        _success, payload = _build_catalog(conn, force, timeout_seconds)
        return _ok(payload)

    def _connected():
        """The live connection, or None - build-on-miss never auto-connects:
        search must stay instant when no editor is around."""
        try:
            conn = _conn()
        except UENotRunningError:
            return None
        if getattr(conn, "is_connected", lambda: False)():
            return conn
        return None

    def _auto_build_on_miss():
        """No catalogue on disk: harvest one now if the editor is connected.
        Returns build metadata when a catalogue was installed, else None."""
        conn = _connected()
        if conn is None:
            return None
        success, payload = _build_catalog(conn, force=False, timeout_seconds=240.0)
        if not success:
            return None
        return {key: payload.get(key) for key in ("engine", "entry_count", "path")}

    @server.tool(
        name="search_unreal_api",
        description=(
            "Search the local Unreal Python API catalogue (built by build_api_catalog) "
            "for classes/methods/properties by keywords — e.g. 'retarget chain offset' "
            "or 'skeletal mesh bone transform'. Also covers the project's own types: "
            "Blueprint classes, widgets, anim BPs, material assets/functions/instances, "
            "user structs/enums, data assets (with parent class and asset path). Local "
            "and instant: no editor round-trip, no "
            "dir(unreal) dumps. Use this BEFORE writing unreal.* calls you are not "
            "certain exist. When no catalogue exists yet and the editor is "
            "connected, the first search harvests one automatically (~4s once, "
            "reported as auto_built). Zero-hit queries automatically retry with UE "
            "synonyms, substring, and typo-tolerant matching (match_mode in the "
            "response says which); an empty result therefore genuinely means 'not "
            "in this catalogue'. Optional kind filter: class, method, property, "
            "function, blueprint, widget, animbp, material, material_function, "
            "material_instance, struct, enum, dataasset."
        ),
    )
    async def search_unreal_api(
        query: str,
        limit: int = 12,
        kind: str = "",
    ) -> list[TextContent]:
        if not query.strip():
            return _err("Provide search keywords, e.g. 'ik retargeter chain settings'.")
        result = api_index.search_catalog(
            query, limit=max(1, min(limit, 50)), kind=kind
        )
        if result is None:
            auto_built = _auto_build_on_miss()
            if auto_built is not None:
                result = api_index.search_catalog(
                    query, limit=max(1, min(limit, 50)), kind=kind
                )
                if result is not None:
                    result["auto_built"] = auto_built
        if result is None:
            return _err(_NO_CATALOG_MSG)
        return _ok(result)

    @server.tool(
        name="describe_unreal_api",
        description=(
            "Full docstring for one unreal symbol (e.g. 'IKRetargeter' or "
            "'IKRetargeterController.set_chain_settings'). Fetches live from the editor "
            "when connected (exact + current), otherwise answers from the local "
            "catalogue. Class responses include the ancestor chain; pass "
            "include_inherited=true to also list which members each ancestor "
            "contributes (UE members live on the defining class, so a class's own "
            "entry shows only a fraction of its surface). Project "
            "Blueprint/widget/AnimBlueprint/material/material-function/material-instance/"
            "struct/enum symbols (e.g. a BP class name or material asset name) are "
            "resolved as assets: parent class, generated class, and Blueprint "
            "variables when the editor is connected."
        ),
    )
    async def describe_unreal_api(
        symbol: str,
        include_inherited: bool = False,
    ) -> list[TextContent]:
        name = symbol.strip()
        if not _SYMBOL_RE.fullmatch(name):
            return _err(f"Invalid symbol {symbol!r}: expected a dotted unreal name.")
        if name.startswith("unreal."):
            name = name[len("unreal.") :]

        described = api_index.describe_from_catalog(
            name, include_inherited=include_inherited
        )
        if described is None and _auto_build_on_miss() is not None:
            described = api_index.describe_from_catalog(
                name, include_inherited=include_inherited
            )
        project_entry = None
        if described is not None:
            for entry in described["entries"]:
                if entry.get("asset_path"):
                    project_entry = entry
                    break

        # Ancestor depth comes from the catalogue either way; attach it to
        # live responses too so the editor path is never shallower.
        lineage = {
            key: described[key]
            for key in ("ancestors", "inherited")
            if described and key in described
        }

        conn = None
        try:
            conn = _conn()
        except UENotRunningError:
            pass
        if conn is not None and getattr(conn, "is_connected", lambda: False)():
            if project_entry is not None:
                # Project asset: getattr on the unreal module can never find
                # it - load the asset and report what reflection exposes.
                script = api_index.build_asset_describe_script(
                    project_entry["asset_path"],
                    project_entry["symbol"],
                    project_entry["kind"],
                )
            else:
                script = wrap_script(
                    "import unreal\n"
                    "obj = unreal\n"
                    f'for part in "{escape_string(name)}".split("."):\n'
                    "    obj = getattr(obj, part)\n"
                    "doc = (getattr(obj, '__doc__', '') or '')[:4000]\n"
                    'print("__MCP_RESULT__" + json.dumps('
                    f'{{"symbol": "{escape_string(name)}", "type": type(obj).__name__, "doc": doc}}))'
                )
            try:
                result = conn.execute(script)
                parsed = result.get("parsed") or {}
                if result.get("success") and not parsed.get("error"):
                    if project_entry is not None and parsed.get("asset_class"):
                        return _ok(
                            {
                                "source": "editor",
                                "symbol": project_entry["symbol"],
                                "kind": project_entry["kind"],
                                "parent": project_entry.get("parent") or "",
                                **parsed,
                            }
                        )
                    if project_entry is None and parsed.get("doc") is not None:
                        return _ok({"source": "editor", **parsed, **lineage})
            except UEConnectionError:
                pass

        if described is None:
            return _err(_NO_CATALOG_MSG)
        if not described["entries"]:
            return _err(
                f"No symbol {name!r} in the catalogue. Try search_unreal_api with "
                "keywords first."
            )
        return _ok({"source": "catalog", **described})

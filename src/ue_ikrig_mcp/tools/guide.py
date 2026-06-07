"""UE Python scripting guide for MCP drivers (1 tool).

The guide encodes the conventions that make generated Unreal Python scripts
succeed on the first try: the result protocol, asset-path rules, modern API
choices, editor-state checks, and timeout discipline.
"""

import json
from mcp.types import TextContent

_SECTIONS: dict[str, str] = {}

_SECTIONS["workflow"] = """\
## Connection workflow

- `execute_python` auto-connects when no editor connection exists yet; other
  tools require `connect_to_editor` first.
- If anything transport-related fails, run `preflight_discovery` and follow its
  `next_action` instead of retrying `connect_to_editor`/`execute_python` blindly.
- `connection_status` is cheap; use it to confirm the transport before a long
  batch run.
- One MCP call should carry one whole script. Loop **inside** the script
  (server-side) instead of issuing one MCP call per item - each call is a full
  editor round-trip.
"""

_SECTIONS["protocol"] = """\
## Result protocol and pre-defined helpers

In `ExecuteFile` mode (the default), every `execute_python`/`run_script` call
already has these defined — do **not** rewrite imports or boilerplate:

| Helper | Replaces |
|---|---|
| `load(path)` | `unreal.load_asset` + None-guard (raises with the bad path) |
| `mcp_result(payload)` | `print("__MCP_RESULT__" + json.dumps(payload, default=str))` |
| `subsys(cls)` | `unreal.get_editor_subsystem(cls)` |
| `asset_registry()` | `unreal.AssetRegistryHelpers.get_asset_registry()` |
| `unreal`, `json` | already imported |

A complete script is just the unique logic:

```python
mesh = load(ARGS.get("mesh", "/Game/Characters/Hero/SK_Hero"))
mcp_result({"materials": [str(m.material_slot_name) for m in mesh.materials]})
```

- End every script with **one** `mcp_result(...)` call - it comes back in the
  `parsed` field. No sentinel -> `parsed` is null and you only get raw text.
- `mcp_result` serializes unreal types via `str()` automatically; convert
  explicitly only when you need structure (e.g. `list(vec.to_tuple())`).
- Report partial failures inside the payload (`{"success": False, ...}`)
  rather than letting an exception discard partial results.
- When `parsed` is present the raw output echo is omitted from the response
  (compact mode); pass `compact=False` if you genuinely need stdout text.
- `mode`: keep the default `ExecuteFile`; `EvaluateStatement` is for a single
  expression only (helpers are not injected in statement/expression modes).
- Helpers can be disabled with `inject_helpers=False` (e.g. for scripts using
  `from __future__` imports, which must be the first statement).
"""

_SECTIONS["tokens"] = """\
## Token economy

Cheapest path first - each step down costs roughly 10x more tokens:

1. **Dedicated tool** (~30 tokens): list_skeletal_meshes, adjust_bone_rotation,
   capture_viewport, ... Check the tool list before writing any Python.
2. **Batch tool** (1 call instead of N): batch_retargeter_ops collapses 12+
   per-bone/per-chain calls into one; batch_retarget handles whole animation
   sets. Loop server-side, never one MCP call per item.
3. **run_script** (~40 tokens): replay a script saved earlier with
   save_script, passing parameters via `args` (exposed as ARGS). If you have
   written substantially the same execute_python script twice, save it now -
   scripts persist across sessions.
4. **execute_python** (hundreds of tokens): last resort for one-off logic.
   Rely on the pre-defined helpers (see the protocol section) instead of
   rewriting boilerplate, and return only the data you need via mcp_result -
   filter and slice in UE, not in your context.

Response size: results are shaped by default - structured `parsed` data
suppresses the raw output echo, and `max_output_chars` (default 8000)
truncates oversized text keeping head+tail. Never request unbounded listings;
filter server-side (path prefixes, class filters, limits).

API lookup: `search_unreal_api` / `describe_unreal_api` answer from a local
catalogue - cheaper and faster than dir(unreal) dumps through the editor,
and they prevent the costliest failure of all: a script written against a
hallucinated API.
"""

_SECTIONS["assets"] = """\
## Asset paths and loading

- Asset paths are **object paths**: `/Game/Folder/AssetName` - never a
  filesystem path, never a `.uasset` extension, never a `Content/` prefix.
  Engine plugins use `/Engine/...` or `/<PluginName>/...`.
- Always guard loads:

```python
asset = unreal.load_asset("/Game/Characters/Hero/SK_Hero")
if asset is None:
    raise ValueError("Asset not found: /Game/Characters/Hero/SK_Hero")
```

- Don't guess paths. Enumerate first with `list_skeletal_meshes` /
  `list_ik_rigs` / `list_retargeters`, or query the registry:

```python
ar = unreal.AssetRegistryHelpers.get_asset_registry()
assets = ar.get_assets_by_path("/Game/Characters", recursive=True)
print([str(a.package_name) for a in assets])
```

- Check existence with `unreal.EditorAssetLibrary.does_asset_exist(path)`
  before destructive operations; save with
  `unreal.EditorAssetLibrary.save_loaded_asset(asset)` after mutating.
"""

_SECTIONS["api"] = """\
## Engine API correctness

- Never invent `unreal.*` names. Resolve them locally first - it costs zero
  editor round-trips:
  - `search_unreal_api("retarget chain offset")` - keyword search over every
    class/method/property of this engine version (the catalogue harvests
    itself on first search while connected; `build_api_catalog` is only
    needed explicitly for `force=true` rebuilds).
  - `describe_unreal_api("IKRetargeterController")` - the full docstring for
    one symbol (live from the editor when connected).
- UE members live on the **defining** class, so a class's own entry shows only
  a fraction of its surface. Class describes always return the `ancestors`
  chain; pass `include_inherited=true` to map each ancestor to the member
  names it contributes, then describe `Base.member` for exact signatures.
- A search miss does NOT prove the API is absent. Zero-hit queries already
  retry with UE synonyms, substring, and typo matching (`match_mode` shows
  which pass answered) - if matches are still empty, try the UE term for the
  concept ('rotator' not 'rotation', 'spawn' not 'create'), rebuild the
  catalogue if a plugin was enabled since the last harvest, and only then
  probe live with dir(unreal).
- The catalogue also covers **project types**: Blueprint classes, widgets,
  user structs/enums, data assets (kinds: blueprint, widget, animbp, struct,
  enum, dataasset). Blueprint classes are assets, never `unreal.*` attributes -
  reach one via `load("/Game/.../BP_Foo")` or its generated class
  `unreal.load_object(None, "/Game/.../BP_Foo.BP_Foo_C")`.
  `describe_unreal_api("BP_Foo")` returns parent class, generated class, and
  BP variables; BP-defined *functions* are not reflected by UE Python - call
  them on instances, do not enumerate them. Rebuild with
  `build_api_catalog(force=true)` after creating new project assets.
- Only fall back to in-editor probing when the catalogue is unavailable:

```python
print([n for n in dir(unreal) if "retarget" in n.lower()])
obj = unreal.load_asset(p); print([n for n in dir(obj) if not n.startswith("_")])
```

- Check the engine version when behavior differs across releases:
  `print(unreal.SystemLibrary.get_engine_version())`.
- Prefer modern subsystems over deprecated libraries:

| Deprecated | Use instead |
|---|---|
| `unreal.EditorLevelLibrary` (actors) | `unreal.get_editor_subsystem(unreal.EditorActorSubsystem)` |
| `unreal.EditorLevelLibrary` (levels/viewport) | `unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)` |
| `unreal.EditorUtilityLibrary` selection | `unreal.get_editor_subsystem(unreal.EditorUtilitySubsystem)` |
| `unreal.EditorAssetLibrary` (bulk ops) | `unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)` |

  (`EditorAssetLibrary.does_asset_exist`/`save_loaded_asset` still work and are
  fine for one-off checks.)
- Property access uses snake_case (`mesh.skeleton`), or
  `obj.get_editor_property("target_skeleton")` /
  `obj.set_editor_property(...)` when the attribute is not exposed directly.
  Wrap multi-edit mutations in `with unreal.ScopedEditorTransaction("desc"):`
  so they are undoable and notify the editor once.
- For Control Rig authoring patterns (RigVM node names, pin paths, struct
  drift), read docs/control_rig_python_patterns.md in this repo first.
"""

_SECTIONS["timeouts"] = """\
## Long operations and timeouts

- Scripts run **synchronously on the editor game thread**. The editor UI is
  frozen until the script returns - never call `input()`, never busy-wait or
  `time.sleep()` in a polling loop.
- Default command timeout is 120s (UE_COMMAND_EXEC_TIMEOUT /
  UE_WINDOWS_BRIDGE_EXEC_TIMEOUT). For known-heavy work (batch retargets,
  FBX import/export), pass `timeout_seconds` explicitly on `execute_python`.
- If a call times out, the editor is usually **still running the script** -
  do not immediately resend it (you would queue it twice). Check
  `connection_status`, give the editor time to finish, then verify the effect
  of the first attempt before retrying.
- A failed reconnect **right after a timeout** usually means busy, not dead:
  UE answers discovery on the game thread, so heavy work (compiles, bakes)
  makes a healthy editor look absent. The error says when the editor process
  is still alive (`EDITOR_PROCESS_ALIVE_BUT_SILENT` in preflight_discovery) -
  wait and retry instead of restarting, and size `timeout_seconds` to the
  operation.
- Split very large batches into chunks (e.g. 25 assets per call) so each call
  fits comfortably inside the timeout and partial progress is observable.
- Wrap long loops in `unreal.ScopedSlowTask` for cancellable progress:

```python
with unreal.ScopedSlowTask(len(items), "Retargeting...") as task:
    task.make_dialog(True)
    for item in items:
        if task.should_cancel():
            break
        task.enter_progress_frame(1)
        process(item)
```
"""

_SECTIONS["errors"] = """\
## Reading failures

Failed results include a `hints` list with classified guidance. Common cases:

| Symptom | Cause / fix |
|---|---|
| `AttributeError: module 'unreal' has no attribute 'X'` | API renamed/removed/hallucinated - probe `dir(unreal)` and check the engine version. |
| `'NoneType' object has no attribute ...` | A load/find returned None (usually a bad asset path) - guard and list candidates first. |
| `Failed to load ...` / `... not found` | Wrong object path format or nonexistent asset. |
| `... is deprecated` warning | Switch to the subsystem equivalent (see the api section). |
| SyntaxError reported locally | The script never reached Unreal; fix escaping (quotes, backslashes, f-string braces). |
| Timeout | Editor still busy on the game thread - see the timeouts section. |
| `parsed` is null on success | The script did not print the `__MCP_RESULT__` sentinel. |

Diagnosis order for transport errors: `connection_status` ->
`preflight_discovery` -> editor's Output Log (Python category).
"""

_TOPICS = ("workflow", "protocol", "tokens", "assets", "api", "timeouts", "errors")


def register(server):

    @server.tool(
        name="ue_python_guide",
        description=(
            "Return the Unreal Python scripting guide for this MCP server: result "
            "protocol (__MCP_RESULT__), asset-path rules, modern vs deprecated engine "
            "APIs, long-operation/timeout discipline, and failure triage. Read it once "
            "per session before generating non-trivial scripts for execute_python. "
            f"Optional topic filter: one of {', '.join(_TOPICS)}, or 'all'."
        ),
    )
    async def ue_python_guide(topic: str = "all") -> list[TextContent]:
        requested = topic.strip().lower()
        if requested in ("", "all"):
            keys = list(_TOPICS)
        elif requested in _SECTIONS:
            keys = [requested]
        else:
            return [TextContent(
                type="text",
                text=json.dumps({
                    "error": True,
                    "message": f"Unknown topic {topic!r}.",
                    "topics": list(_TOPICS) + ["all"],
                }, indent=2),
            )]
        header = "# Unreal Python scripting guide (ue-ikrig-mcp)\n\n"
        return [TextContent(type="text", text=header + "\n".join(_SECTIONS[k] for k in keys))]

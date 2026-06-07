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
## Result protocol (__MCP_RESULT__)

To return structured data, end every script with exactly one sentinel line:

```python
import json
print("__MCP_RESULT__" + json.dumps({"success": True, "items": items}))
```

- The server parses everything after `__MCP_RESULT__` as JSON and returns it in
  the `parsed` field. No sentinel -> `parsed` is null and you only get raw text.
- Print the sentinel **last**, once. Earlier prints are fine; they land in
  `output`.
- Only JSON-serializable values: convert unreal types first, e.g.
  `str(asset.get_path_name())`, `list(vector.to_tuple())`.
- Report failures inside the payload (`{"success": False, "error": ...}`)
  rather than letting an exception discard the partial result, unless the
  failure should abort the whole script.
- `mode` parameter: keep the default `ExecuteFile` for scripts;
  `EvaluateStatement` is for a single expression only.
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

- Never invent `unreal.*` names. If unsure, probe first - it costs one fast
  call:

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

_TOPICS = ("workflow", "protocol", "assets", "api", "timeouts", "errors")


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

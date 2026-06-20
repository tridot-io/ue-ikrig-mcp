# ue-ikrig-mcp

MCP server for creating and fine-tuning IK Rigs and IK Retargeters in Unreal Engine 5.

Enables conversational retarget tuning through Claude Code: "the left arm looks off, rotate it inward 5 degrees" -> instant live update in the UE Editor viewport.

## Prerequisites

- Unreal Engine 5.x with **Python Editor Script Plugin** enabled
- **Python Remote Execution** enabled in Editor Preferences
- [uv](https://docs.astral.sh/uv/) installed (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)

For Windows-hosted Unreal with WSL-hosted agents, verify Unreal's Python Remote
Execution settings before retrying MCP calls:

```ini
[/Script/PythonScriptPlugin.PythonScriptPluginSettings]
bRemoteExecution=True
RemoteExecutionMulticastGroupEndpoint=239.0.0.1:6766
RemoteExecutionMulticastBindAddress=0.0.0.0 ; or the correct Windows adapter IP
RemoteExecutionMulticastTtl=1              ; WSL/Windows; keep 0 only for same namespace
```

Do **not** use `RemoteExecutionMulticastBindAddress=127.0.0.1` when the MCP
server runs in WSL and Unreal runs on Windows. That binds Unreal's Remote
Execution UDP listener to Windows loopback only; WSL discovery then either sees
no `pong` or fails to bind `0.0.0.0:6766` because the Windows loopback endpoint
is mirrored into WSL. The MCP defaults include a WSL multicast-group bind
fallback, but Unreal still needs a non-loopback bind address and an editor
restart after changing this setting.

When developing in WSL, launch the MCP server via Windows Python (pythonw.exe)
so it discovers Unreal on Windows localhost directly — there is no WSL→Windows
bridge anymore. For example:
`.venv-win/Scripts/pythonw.exe -m ue_ikrig_mcp`. Running the server inside WSL
will not discover an editor on the Windows host (the WSL/Windows network
namespace blocks the discovery UDP), and the server prints a warning to that
effect on startup.

Unreal may need a restart after plugin or network setting changes. Windows
Defender Firewall must allow `UnrealEditor.exe`, UDP multicast on `6766`, and
the TCP callback port from Windows back to the MCP host.

## Installation

### Claude Code (recommended)

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "ue-ikrig": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tridot-io/ue-ikrig-mcp.git", "ue-ikrig-mcp"]
    }
  }
}
```

No manual install needed. `uvx` handles everything automatically.

### Manual install

```bash
pip install git+https://github.com/tridot-io/ue-ikrig-mcp.git
ue-ikrig-mcp
```

## Tools

### Connection (5)
- `preflight_discovery` - Deterministic UDP ping/pong and optional TCP callback diagnostic
- `discover_editors` - Find running UE Editor instances; the response may contain multiple selectable `node_id`s
- `connect_to_editor` - Open this MCP process's single active command channel to one selected editor (`node_id` required when discovery is ambiguous)
- `disconnect_editor` - Close the command/discovery sockets held by this MCP process
- `connection_status` - Check connection state, including the active `node_id`, discovered nodes, and whether selection is required

### IK Rig (10)
- `create_ik_rig` - Create new IK Rig asset
- `inspect_ik_rig` - Read full rig state
- `set_ik_rig_mesh` - Assign skeletal mesh
- `set_retarget_root` - Set retarget root bone
- `add_retarget_chain` / `remove_retarget_chain` / `get_retarget_chains` - Manage chains
- `list_bones` - List skeleton bone hierarchy
- `list_ik_assets` - Find existing IK/RTG assets
- `save_asset` - Save asset to disk

### IK Retargeter (7)
- `create_retargeter` - Create new IK Retargeter asset
- `inspect_retargeter` - Read full retargeter state
- `set_retargeter_rigs` - Assign source/target IK Rigs
- `auto_map_chains` - Auto-map chains by name similarity
- `set_chain_mapping` / `get_chain_mappings` - Manual chain mapping
- `auto_align_all_bones` - Auto-align retarget pose

### Fine-Tuning (10)
- `get_bone_rotation_offset` / `set_bone_rotation_offset` - Read/write bone rotation
- `adjust_bone_rotation` - Incremental euler rotation (primary tuning tool)
- `set_root_offset` - Adjust root translation
- `get_chain_settings` / `set_chain_settings` - FK/IK blend settings
- `get_global_settings` / `set_global_settings` - Global retarget settings
- `create_retarget_pose` / `set_current_pose` - Pose management

### Batch & Utility (4)
- `batch_retarget` - Bulk retarget animations
- `execute_python` - Raw Python escape hatch (`mode` and optional `timeout_seconds`); auto-connects, validates syntax locally, injects helper prelude, returns failure `hints`
- `list_skeletal_meshes` - Find skeletal meshes
- `ue_python_guide` - Unreal Python scripting guide for MCP drivers (result protocol, token economy, asset paths, API pitfalls, timeouts)

### Script store (4)
- `save_script` - Persist a reusable UE Python script under a name (syntax-checked; survives sessions)
- `run_script` - Replay a saved script with JSON parameters exposed as the `ARGS` dict
- `list_scripts` / `delete_script` - Manage the store (`UE_MCP_SCRIPT_DIR`, default `~/.ue_ikrig_mcp/scripts`)

### API catalogue (3)
- `build_api_catalog` - One-time harvest of the editor's `unreal` Python API (classes/methods/properties with signatures and doc summaries) plus the project's own types from the asset registry (Blueprint/widget/anim-BP classes, user structs/enums, data assets - with parent class and asset path, no asset loading) into a local file keyed by engine version; runs automatically on the first search when the editor is connected, so the explicit call is mainly for `force=true` rebuilds
- `search_unreal_api` - Instant local BM25 keyword search over the catalogue - no editor round-trip, prevents hallucinated API names; zero-hit queries cascade through UE-synonym, substring, and typo-tolerant passes (`match_mode` reports which); kind filters include `blueprint`, `widget`, `struct`, `enum`, `dataasset`
- `describe_unreal_api` - Full docstring for one symbol (live from the editor when connected, catalogue otherwise); class responses carry the ancestor chain, and `include_inherited=true` maps each ancestor to the members it contributes (UE members live on the defining class); project Blueprint symbols resolve as assets - parent class, generated class, BP variables (`UE_MCP_CATALOG_DIR`, default `~/.ue_ikrig_mcp/api_catalog`)

### Capture (3)
- `capture_viewport` - Level editor viewport screenshot via UE AutomationLibrary (hardened with realtime/repaint forcing)
- `capture_ue_window` - OS-level window or tab screenshot with PrintWindow fallback (works for any visible UE window)
- `capture_asset_editor` - Open an asset in its editor and capture the preview viewport (IK Retargeter, AnimBP, SkeletalMesh, PhysicsAsset, ControlRig)

## Architecture

```
Claude Code  <--stdio-->  MCP Server (Python)  <--UDP/TCP-->  UE Editor
```

The server communicates with UE Editor via the built-in Python Remote Execution protocol (UDP multicast discovery + TCP commands). If the configured command port is still held by another local MCP process, `connect_to_editor` automatically falls back to a free local port unless `UE_COMMAND_PORT_STRICT=true` is set.

Multiple independent MCP server processes may connect to the same Unreal Editor.
That concurrency boundary is **per MCP process**: each process keeps its own
discovery state and local command listener. It does **not** mean one MCP process
can hold multiple active editor connections at once. When a second MCP process
targets the same editor and the default local command port is already in use, it
should fall back to an ephemeral local port and report that in
`connection_status`.

### Discover many, connect one

`discover_editors` is a discovery tool: it can return every visible Unreal
Editor node discovered over direct UDP. Use the returned `node_id` values to
choose the target editor explicitly.

`connect_to_editor` is a selection tool: one MCP server process keeps exactly
one active editor connection at a time. When more than one editor is discovered
and no still-valid active editor is already selected, omitting `node_id` is
ambiguous. Instead of silently choosing the first editor, the tool reports the
stable ambiguity signal:

```json
{
  "error": true,
  "error_code": "MULTIPLE_EDITORS_DISCOVERED",
  "classification": "MULTIPLE_EDITORS_DISCOVERED",
  "message": "Multiple Unreal Editor instances were discovered. Retry with node_id.",
  "nodes": [
    {
      "node_id": "...",
      "project_name": "...",
      "_transport": "direct_udp"
    }
  ],
  "next_action": "Call connect_to_editor(node_id=<one of nodes[].node_id>)."
}
```

Retry with `connect_to_editor(node_id="<chosen node_id>")` to bind this MCP
process to one editor. Selecting a different `node_id` later switches the
process to that editor; it does not create a second active session.

If `connection_status` already shows a still-valid active editor, a later
`connect_to_editor()` call with omitted `node_id` is idempotent: it reuses the
current active editor even when discovery can see additional editors. Status
responses distinguish the active selection from discovery inventory with fields
such as `connected`, `node_id`, `discovered_nodes`, `selection_required`, and
`one_active_editor_per_process`.

## Discovery preflight / doctor

Run `preflight_discovery` before `discover_editors`/`connect_to_editor` when
bringing up a new machine or editor. It sends Unreal's exact Remote Execution
UDP `ping` packet and waits for a `pong`. Only after a pong does it optionally
test the TCP `open_connection` callback; it never executes Python in Unreal.

If `preflight_discovery` reports `NO_PONG_RECEIVED_UNPROVEN`, discovery was not
proven. Do **not** keep retrying `connect_to_editor` or `execute_python`; fix
discovery first:

- enable Python Remote Execution in Unreal,
- match `RemoteExecutionMulticastGroupEndpoint` with `UE_MULTICAST_GROUP` /
  `UE_MULTICAST_PORT`,
- set the correct Unreal multicast bind address,
- check Windows Firewall and network profile,
- if running under WSL, launch the server via Windows Python (pythonw.exe) so
  discovery runs on the Windows host instead of across the WSL namespace.

Useful environment overrides:

```bash
UE_MULTICAST_GROUP=239.0.0.1
UE_MULTICAST_PORT=6766
UE_MULTICAST_BIND=0.0.0.0,239.0.0.1    # comma/semicolon list accepted
UE_MULTICAST_INTERFACE=172.30.1.10     # optional; WSL auto-detects when unset
UE_MULTICAST_MEMBERSHIP=172.30.1.10    # optional; WSL auto-detects when unset
UE_MULTICAST_TTL=1                     # WSL default; non-WSL default is 0
UE_COMMAND_HOST=0.0.0.0
UE_COMMAND_PORT=6777
UE_COMMAND_EXEC_TIMEOUT=120            # seconds for direct TCP command execution
UE_CONNECTION_STATUS_TIMEOUT=0.25      # seconds for connection_status liveness probes
UE_CALLBACK_HOST=172.30.1.10           # never advertise 0.0.0.0 to Unreal
UE_DISCOVERY_SETTLE=0.25               # extra wait after first pong for more editors
UE_BROKER=true                         # shared editor-command broker (native, non-WSL); false to disable
UE_SCRIPT_PREFLIGHT=true               # local syntax check before sending scripts to UE
UE_MCP_SCRIPT_DIR=~/.ue_ikrig_mcp/scripts      # saved script store location
UE_MCP_CATALOG_DIR=~/.ue_ikrig_mcp/api_catalog # unreal API catalogue location
```

### Script guidance for MCP drivers

- `ue_python_guide` returns the scripting guide (result protocol, asset-path
  rules, modern vs deprecated APIs, timeout discipline, failure triage); read
  it once per session before generating non-trivial scripts.
- For graph authoring across Blueprint, WidgetBlueprint, AnimBlueprint, Control Rig, and Material, see `docs/ue_graph_authoring_driver_guideline.md` before assuming node creation or pin-wiring support.
- `execute_python` auto-connects when no editor connection exists, validates
  script syntax locally before any editor round-trip
  (`UE_SCRIPT_PREFLIGHT=false` to disable), and failed results include a
  `hints` list that classifies common Unreal Python failures (hallucinated or
  deprecated APIs, bad asset paths, missing `__MCP_RESULT__` sentinel,
  timeouts) into actionable fixes.

### Token economy for drivers

Generating UE Python inline burns driver tokens; the server removes the
recurring overhead:

- **Helper prelude**: in `ExecuteFile` mode every `execute_python`/`run_script`
  call has `load()`, `mcp_result()`, `subsys()`, `asset_registry()` and the
  `unreal`/`json` imports pre-defined — scripts contain only unique logic
  (`inject_helpers=false` to opt out).
- **Saved scripts**: `save_script` once, `run_script(name, args)` forever —
  parameters arrive as the `ARGS` dict, scripts persist on disk across
  sessions (`UE_MCP_SCRIPT_DIR`, default `~/.ue_ikrig_mcp/scripts`).
- **Result shaping**: when a script returns structured data via
  `__MCP_RESULT__`, the raw output echo is omitted (`compact=false` to keep
  it); oversized text is truncated head+tail at `max_output_chars`
  (default 8000, `0` = unlimited).
- The `ue_python_guide` `tokens` topic gives drivers the cheapest-path
  ranking: dedicated tool → batch tool → `run_script` → `execute_python`.

Failed preflight output includes OS/WSL detection, local IPv4 candidates, the
route-selected local address for the multicast group, bind/interface/membership
candidates, TTL, ping timestamp, pong sources, packet parse errors, callback
listener details, and socket errors. If you need help, collect:

- `Saved/Logs/<Project>.log`,
- Unreal Output Log filtered for Python, sockets, or remote execution,
- Windows Firewall status for `UnrealEditor.exe`,
- packet evidence such as WSL `tcpdump udp port 6766` or Windows
  Wireshark/`pktmon`.

`connection_status` actively probes the current transport before reporting
`connected`: direct TCP sockets are peeked for peer closure, and a broker
connection is status-pinged. The `connection_liveness` field records the probe
result, and stale transports are cleared instead of being reported as connected.
It also reports the loaded package version/source file under `package`.
For multi-editor sessions, check the active `node_id` separately from
`discovered_nodes`: `selection_required=true` means discovery found more than
one editor and the next connect must name a `node_id`; `selection_required=false`
with `connected=true` means this MCP process has one active editor selected.
Check these fields when `uvx` appears to be running stale code or when a
previously connected editor has disappeared.

`execute_python` accepts `timeout_seconds` for per-call command bounds. When it
is omitted, command execution uses `UE_COMMAND_EXEC_TIMEOUT` (default 120s).

## License

MIT

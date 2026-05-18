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

When direct WSL UDP multicast still cannot receive Unreal's `pong`, the MCP
automatically falls back to a Windows-side Python subprocess bridge
(`UE_WINDOWS_BRIDGE=true`, default on WSL). The bridge discovers Unreal and
opens the command callback from Windows localhost, which avoids WSL/Windows
multicast namespace loss while keeping the MCP server in WSL.

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
- `discover_editors` - Find running UE Editor instances
- `connect_to_editor` - Open command channel to an editor
- `disconnect_editor` - Close the command/discovery sockets held by this MCP process
- `connection_status` - Check connection state

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

### Batch & Utility (3)
- `batch_retarget` - Bulk retarget animations
- `execute_python` - Raw Python escape hatch
- `list_skeletal_meshes` - Find skeletal meshes

### Capture (3)
- `capture_viewport` - Level editor viewport screenshot via UE AutomationLibrary (hardened with realtime/repaint forcing)
- `capture_ue_window` - OS-level window or tab screenshot with PrintWindow fallback (works for any visible UE window)
- `capture_asset_editor` - Open an asset in its editor and capture the preview viewport (IK Retargeter, AnimBP, SkeletalMesh, PhysicsAsset, ControlRig)

## Architecture

```
Claude Code  <--stdio-->  MCP Server (Python)  <--UDP/TCP-->  UE Editor
```

The server communicates with UE Editor via the built-in Python Remote Execution protocol (UDP multicast discovery + TCP commands). If the configured command port is still held by another local MCP process, `connect_to_editor` automatically falls back to a free local port unless `UE_COMMAND_PORT_STRICT=true` is set.

## Discovery preflight / doctor

Run `preflight_discovery` before `discover_editors`/`connect_to_editor` when
bringing up a new machine, WSL environment, or editor. It sends Unreal's exact
Remote Execution UDP `ping` packet and waits for a `pong`. Only after a pong
does it optionally test the TCP `open_connection` callback; it never executes
Python in Unreal.

If `preflight_discovery` reports `NO_PONG_RECEIVED_UNPROVEN`, do **not** keep
retrying `connect_to_editor` or `execute_python`. Fix UDP discovery first:

- enable Python Remote Execution in Unreal,
- match `RemoteExecutionMulticastGroupEndpoint` with `UE_MULTICAST_GROUP` /
  `UE_MULTICAST_PORT`,
- set the correct Unreal multicast bind address,
- check Windows Firewall and network profile,
- for WSL, try TTL `1`, explicit interface overrides, mirrored networking, or
  leave the Windows bridge enabled if multicast cannot cross the WSL namespace.

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
UE_CALLBACK_HOST=172.30.1.10           # never advertise 0.0.0.0 to Unreal
UE_WINDOWS_BRIDGE=true                 # WSL default; set false to disable
UE_WINDOWS_BRIDGE_DISCOVERY_TIMEOUT=5  # seconds for Windows-side discovery
UE_WINDOWS_BRIDGE_EXEC_TIMEOUT=120     # seconds for bridge command execution
```

Failed preflight output includes OS/WSL detection, local IPv4 candidates, the
route-selected local address for the multicast group, bind/interface/membership
candidates, TTL, ping timestamp, pong sources, packet parse errors, callback
listener details, and socket errors. If you need help, collect:

- `Saved/Logs/<Project>.log`,
- Unreal Output Log filtered for Python, sockets, or remote execution,
- Windows Firewall status for `UnrealEditor.exe`,
- packet evidence such as WSL `tcpdump udp port 6766` or Windows
  Wireshark/`pktmon`.

## License

MIT

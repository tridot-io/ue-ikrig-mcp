# ue-ikrig-mcp

MCP server for creating and fine-tuning IK Rigs and IK Retargeters in Unreal Engine 5.

Enables conversational retarget tuning through Claude Code: "the left arm looks off, rotate it inward 5 degrees" -> instant live update in the UE Editor viewport.

## Prerequisites

- Unreal Engine 5.x with **Python Editor Script Plugin** enabled
- **Python Remote Execution** enabled in Editor Preferences
- [uv](https://docs.astral.sh/uv/) installed (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)

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

### Connection (4)
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

## Architecture

```
Claude Code  <--stdio-->  MCP Server (Python)  <--UDP/TCP-->  UE Editor
```

The server communicates with UE Editor via the built-in Python Remote Execution protocol (UDP multicast discovery + TCP commands). If the configured command port is still held by another local MCP process, `connect_to_editor` automatically falls back to a free local port unless `UE_COMMAND_PORT_STRICT=true` is set.

## License

MIT

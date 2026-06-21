"""Retarget-op enable/disable + generic info + common op-specific setters.

UE 5.6's IKRetargeterController exposes `set_retarget_op_enabled(idx, bool)` and
`get_retarget_op_enabled(idx)` but existing MCP tools like `inspect_retargeter_full`
can misreport the enabled flag. These tools go through the authoritative API.

Also covers two common per-op mutations that came up repeatedly in real
retargeter authoring but had no dedicated tool:
  * Run IK Rig: excluded_goals (exclude hand/foot goals from FBIK solve)
  * Scale Source: source_scale_factor (cross-proportion correction)
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def register(server):
    @server.tool(
        name="set_retarget_op_enabled",
        description=(
            "Enable or disable a single op in the retargeter op stack by index. "
            "Uses the authoritative IKRetargeterController API so the state is "
            "reliably persisted and read. Returns the before/after state."
        ),
    )
    async def set_retarget_op_enabled(
        retargeter_path: str,
        op_index: int,
        enabled: bool,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        val = "True" if enabled else "False"
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "if type(rtg).__name__ != 'IKRetargeter':\n"
            "    raise ValueError('Asset is not an IKRetargeter (got %s): %s' % (type(rtg).__name__, rtg.get_path_name()))\n"
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"idx = int({int(op_index)})\n"
            "n = ctrl.get_num_retarget_ops()\n"
            "if idx < 0 or idx >= n:\n"
            f'    raise ValueError(f"op_index out of range: idx={{idx}} n={{n}}")\n'
            "before = bool(ctrl.get_retarget_op_enabled(idx))\n"
            f"ctrl.set_retarget_op_enabled(idx, {val})\n"
            "after = bool(ctrl.get_retarget_op_enabled(idx))\n"
            "name = str(ctrl.get_op_name(idx))\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"index": idx, "name": name, "before": before, "after": after'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="get_retarget_op_info",
        description=(
            "Return one or all retarget ops with their authoritative enabled state "
            "and raw settings export_text dump. If op_index is omitted (-1), returns "
            "the full stack. More reliable than inspect_retargeter_full for the "
            "enabled flag, and produces the canonical settings text for inspection."
        ),
    )
    async def get_retarget_op_info(
        retargeter_path: str,
        op_index: int = -1,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        idx_arg = str(int(op_index))
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "if type(rtg).__name__ != 'IKRetargeter':\n"
            "    raise ValueError('Asset is not an IKRetargeter (got %s): %s' % (type(rtg).__name__, rtg.get_path_name()))\n"
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            f"query_idx = int({idx_arg})\n"
            "n = ctrl.get_num_retarget_ops()\n"
            "indices = [query_idx] if query_idx >= 0 else list(range(n))\n"
            "ops = []\n"
            "for i in indices:\n"
            "    if i < 0 or i >= n: continue\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    name = str(ctrl.get_op_name(i))\n"
            "    ctype = type(oc).__name__\n"
            "    enabled = bool(ctrl.get_retarget_op_enabled(i))\n"
            "    try:\n"
            "        stg = oc.get_settings()\n"
            "        stg_text = stg.export_text() if stg else ''\n"
            "        stg_type = type(stg).__name__ if stg else None\n"
            "    except Exception as _e:\n"
            "        stg_text = f'err: {_e}'\n"
            "        stg_type = None\n"
            "    ops.append({'index': i, 'name': name, 'controller_type': ctype,\n"
            "                'enabled': enabled, 'settings_type': stg_type,\n"
            "                'settings_text': stg_text})\n"
            'print("__MCP_RESULT__" + json.dumps({"count": n, "ops": ops}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_run_ik_rig_excluded_goals",
        description=(
            "Set the excluded_goals list on the first 'Run IK Rig' op in the "
            "retargeter. Goals in this list are NOT driven by the Retarget IK "
            "Goals op — useful when you want certain chains (e.g. arms) to use "
            "FK retargeting only while others (e.g. feet) retain IK for foot "
            "planting. Pass an empty list to clear the exclusion."
        ),
    )
    async def set_run_ik_rig_excluded_goals(
        retargeter_path: str,
        goal_names: list = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        names = goal_names if goal_names else []
        names_json = json.dumps([str(g) for g in names])
        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "if type(rtg).__name__ != 'IKRetargeter':\n"
            "    raise ValueError('Asset is not an IKRetargeter (got %s): %s' % (type(rtg).__name__, rtg.get_path_name()))\n"
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "run_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetRunIKRigController':\n"
            "        run_idx = i\n"
            "        break\n"
            "if run_idx < 0:\n"
            '    raise ValueError("No Run IK Rig op found in retargeter")\n'
            "oc = ctrl.get_op_controller(run_idx)\n"
            "stg = oc.get_settings()\n"
            f"raw_names = {names_json}\n"
            "excluded = [unreal.Name(n) for n in raw_names]\n"
            "stg.set_editor_property('excluded_goals', excluded)\n"
            "oc.set_settings(stg)\n"
            "after = stg.get_editor_property('excluded_goals')\n"
            "after_list = [str(n) for n in after]\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({'
            '"op_index": run_idx, "excluded_goals": after_list, "saved": saved'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_scale_source_factor",
        description=(
            "Set the SourceScaleFactor on the 'Scale Source' op. Values < 1 "
            "shrink source motion to fit shorter target proportions (e.g. 0.844 "
            "= 46.05/54.57 for MetaHuman-length arms against a taller source). "
            "Values > 1 enlarge. Default UE value is 1.0."
        ),
    )
    async def set_scale_source_factor(
        retargeter_path: str,
        source_scale_factor: float,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "if type(rtg).__name__ != 'IKRetargeter':\n"
            "    raise ValueError('Asset is not an IKRetargeter (got %s): %s' % (type(rtg).__name__, rtg.get_path_name()))\n"
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "ss_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetScaleSourceController':\n"
            "        ss_idx = i\n"
            "        break\n"
            "if ss_idx < 0:\n"
            '    raise ValueError("No Scale Source op found in retargeter")\n'
            "oc = ctrl.get_op_controller(ss_idx)\n"
            "stg = oc.get_settings()\n"
            "before = float(stg.get_editor_property('source_scale_factor'))\n"
            f"stg.set_editor_property('source_scale_factor', float({float(source_scale_factor)}))\n"
            "oc.set_settings(stg)\n"
            "after = float(oc.get_settings().get_editor_property('source_scale_factor'))\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            'print("__MCP_RESULT__" + json.dumps({'
            '"op_index": ss_idx, "before": before, "after": after, "saved": saved'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

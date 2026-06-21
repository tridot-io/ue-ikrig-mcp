"""Per-chain FK rotation/translation settings for the Retarget FK Chains op.

UE 5.6's Retarget FK Chains op holds a TArray<FRetargetFKChainSettings> keyed
by target chain name. Each chain entry controls:
  * enable_fk (bool)
  * rotation_mode (INTERPOLATED, ONE_TO_ONE, ONE_TO_ONE_REVERSED)
  * rotation_alpha (0..1 copy-through for the FK rotation)
  * translation_mode (NONE, COPY_FK_TRANSLATION, ...)
  * translation_alpha (0..1)

Mutating array-of-structs through Python's reflection requires rebuilding the
array because struct-value mutation inside a TArray element does not write back.
These tools handle the rebuild pattern for safe in-place updates.

Common use: set ONE_TO_ONE mode on matching-bone-count chains (LeftArm/RightArm
3<->3, LeftLeg/RightLeg 4<->4) for exact rotation transfer, while leaving the
spine on INTERPOLATED to handle its 3<->5 bone-count mismatch.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_ROT_MODES = {"INTERPOLATED", "ONE_TO_ONE", "ONE_TO_ONE_REVERSED"}
_VALID_TRANS_MODES = {"NONE", "COPY_FK_TRANSLATION"}


def register(server):
    @server.tool(
        name="get_fk_chain_settings",
        description=(
            "Return per-chain FK settings on the Retarget FK Chains op. If "
            "target_chain_name is omitted, returns all chains. Fields per chain: "
            "enable_fk, rotation_mode, rotation_alpha, translation_mode, "
            "translation_alpha."
        ),
    )
    async def get_fk_chain_settings(
        retargeter_path: str,
        target_chain_name: str = "",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rtp = escape_string(retargeter_path)
        filter_name = escape_string(target_chain_name or "")
        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "fk_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetFKChainsController':\n"
            "        fk_idx = i\n"
            "        break\n"
            "if fk_idx < 0:\n"
            '    raise ValueError("No Retarget FK Chains op found in retargeter")\n'
            "fk = ctrl.get_op_controller(fk_idx)\n"
            "stg = fk.get_settings()\n"
            "chains = stg.get_editor_property('chains_to_retarget')\n"
            f'_filter = "{filter_name}"\n'
            "result = []\n"
            "for c in chains:\n"
            "    name = str(c.get_editor_property('target_chain_name'))\n"
            "    if _filter and name != _filter: continue\n"
            "    result.append({\n"
            "        'target_chain_name': name,\n"
            "        'enable_fk': bool(c.get_editor_property('enable_fk')),\n"
            "        'rotation_mode': str(c.get_editor_property('rotation_mode')).split('.')[-1].split(':')[0].strip(),\n"
            "        'rotation_alpha': float(c.get_editor_property('rotation_alpha')),\n"
            "        'translation_mode': str(c.get_editor_property('translation_mode')).split('.')[-1].split(':')[0].strip(),\n"
            "        'translation_alpha': float(c.get_editor_property('translation_alpha')),\n"
            "    })\n"
            'print("__MCP_RESULT__" + json.dumps({"op_index": fk_idx, "chains": result}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="set_fk_chain_settings",
        description=(
            "Update per-chain FK settings on the Retarget FK Chains op. Only "
            "provided fields are changed, others preserved. Rebuilds the "
            "chains_to_retarget TArray to avoid the UE struct-in-array mutation "
            "no-op bug. rotation_mode: INTERPOLATED | ONE_TO_ONE | "
            "ONE_TO_ONE_REVERSED. translation_mode: NONE | COPY_FK_TRANSLATION."
        ),
    )
    async def set_fk_chain_settings(
        retargeter_path: str,
        target_chain_name: str,
        rotation_mode: str = None,
        rotation_alpha: float = None,
        translation_mode: str = None,
        translation_alpha: float = None,
        enable_fk: bool = None,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if rotation_mode is not None and rotation_mode.upper() not in _VALID_ROT_MODES:
            return _err(f"rotation_mode must be one of {sorted(_VALID_ROT_MODES)}; got {rotation_mode!r}")
        if translation_mode is not None and translation_mode.upper() not in _VALID_TRANS_MODES:
            return _err(f"translation_mode must be one of {sorted(_VALID_TRANS_MODES)}; got {translation_mode!r}")

        rtp = escape_string(retargeter_path)
        cname = escape_string(target_chain_name)
        rm = f'"{rotation_mode.upper()}"' if rotation_mode is not None else "None"
        ra = str(float(rotation_alpha)) if rotation_alpha is not None else "None"
        tm = f'"{translation_mode.upper()}"' if translation_mode is not None else "None"
        ta = str(float(translation_alpha)) if translation_alpha is not None else "None"
        ef = "True" if enable_fk is True else ("False" if enable_fk is False else "None")

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "fk_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetFKChainsController':\n"
            "        fk_idx = i\n"
            "        break\n"
            "if fk_idx < 0:\n"
            '    raise ValueError("No Retarget FK Chains op found in retargeter")\n'
            "fk = ctrl.get_op_controller(fk_idx)\n"
            "stg = fk.get_settings()\n"
            "chains = stg.get_editor_property('chains_to_retarget')\n"
            f'cname = "{cname}"\n'
            f"rm_s = {rm}\n"
            f"ra_v = {ra}\n"
            f"tm_s = {tm}\n"
            f"ta_v = {ta}\n"
            f"ef_v = {ef}\n"
            "new_chains = []\n"
            "found = False\n"
            "for c in chains:\n"
            "    name = str(c.get_editor_property('target_chain_name'))\n"
            "    ns = unreal.RetargetFKChainSettings()\n"
            "    ns.set_editor_property('target_chain_name', c.get_editor_property('target_chain_name'))\n"
            "    ns.set_editor_property('enable_fk', c.get_editor_property('enable_fk'))\n"
            "    ns.set_editor_property('rotation_mode', c.get_editor_property('rotation_mode'))\n"
            "    ns.set_editor_property('rotation_alpha', c.get_editor_property('rotation_alpha'))\n"
            "    ns.set_editor_property('translation_mode', c.get_editor_property('translation_mode'))\n"
            "    ns.set_editor_property('translation_alpha', c.get_editor_property('translation_alpha'))\n"
            "    if name == cname:\n"
            "        found = True\n"
            "        if rm_s is not None:\n"
            "            ns.set_editor_property('rotation_mode', getattr(unreal.FKChainRotationMode, rm_s))\n"
            "        if ra_v is not None:\n"
            "            ns.set_editor_property('rotation_alpha', float(ra_v))\n"
            "        if tm_s is not None:\n"
            "            ns.set_editor_property('translation_mode', getattr(unreal.FKChainTranslationMode, tm_s))\n"
            "        if ta_v is not None:\n"
            "            ns.set_editor_property('translation_alpha', float(ta_v))\n"
            "        if ef_v is not None:\n"
            "            ns.set_editor_property('enable_fk', bool(ef_v))\n"
            "    new_chains.append(ns)\n"
            "if not found:\n"
            f'    raise ValueError(f"target_chain_name not found: {cname}")\n'
            "stg.set_editor_property('chains_to_retarget', new_chains)\n"
            "fk.set_settings(stg)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            "# Verify\n"
            "verify = {}\n"
            "for c in fk.get_settings().get_editor_property('chains_to_retarget'):\n"
            "    if str(c.get_editor_property('target_chain_name')) == cname:\n"
            "        verify = {\n"
            "            'rotation_mode': str(c.get_editor_property('rotation_mode')).split('.')[-1].split(':')[0].strip(),\n"
            "            'rotation_alpha': float(c.get_editor_property('rotation_alpha')),\n"
            "            'translation_mode': str(c.get_editor_property('translation_mode')).split('.')[-1].split(':')[0].strip(),\n"
            "            'translation_alpha': float(c.get_editor_property('translation_alpha')),\n"
            "            'enable_fk': bool(c.get_editor_property('enable_fk')),\n"
            "        }\n"
            "        break\n"
            'print("__MCP_RESULT__" + json.dumps({"chain": cname, "saved": saved, "settings": verify}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

    @server.tool(
        name="bulk_set_fk_rotation_mode",
        description=(
            "Apply the same FK rotation_mode to multiple chains in one call. "
            "Common use: set ONE_TO_ONE on all 4 limb chains "
            "(LeftArm, RightArm, LeftLeg, RightLeg) after auto_align_all_bones "
            "(which resets rotation_mode to INTERPOLATED as a side-effect). "
            "rotation_mode: INTERPOLATED | ONE_TO_ONE | ONE_TO_ONE_REVERSED."
        ),
    )
    async def bulk_set_fk_rotation_mode(
        retargeter_path: str,
        chain_names: list,
        rotation_mode: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        if rotation_mode.upper() not in _VALID_ROT_MODES:
            return _err(f"rotation_mode must be one of {sorted(_VALID_ROT_MODES)}; got {rotation_mode!r}")
        if not chain_names:
            return _err("chain_names must be a non-empty list of target chain names")

        rtp = escape_string(retargeter_path)
        names_json = json.dumps([str(n) for n in chain_names])
        mode_upper = rotation_mode.upper()

        script = wrap_script(
            "import unreal\n"
            f'rtg = unreal.load_asset("{rtp}")\n'
            "if rtg is None:\n"
            f'    raise ValueError("IKRetargeter not found: {rtp}")\n'
            "ctrl = unreal.IKRetargeterController.get_controller(rtg)\n"
            "fk_idx = -1\n"
            "for i in range(ctrl.get_num_retarget_ops()):\n"
            "    oc = ctrl.get_op_controller(i)\n"
            "    if type(oc).__name__ == 'IKRetargetFKChainsController':\n"
            "        fk_idx = i\n"
            "        break\n"
            "if fk_idx < 0:\n"
            '    raise ValueError("No Retarget FK Chains op found in retargeter")\n'
            "fk = ctrl.get_op_controller(fk_idx)\n"
            "stg = fk.get_settings()\n"
            "chains = stg.get_editor_property('chains_to_retarget')\n"
            f"targets = set({names_json})\n"
            f"mode_enum = unreal.FKChainRotationMode.{mode_upper}\n"
            "new_chains = []\n"
            "applied = []\n"
            "for c in chains:\n"
            "    name = str(c.get_editor_property('target_chain_name'))\n"
            "    ns = unreal.RetargetFKChainSettings()\n"
            "    ns.set_editor_property('target_chain_name', c.get_editor_property('target_chain_name'))\n"
            "    ns.set_editor_property('enable_fk', c.get_editor_property('enable_fk'))\n"
            "    ns.set_editor_property('rotation_mode', c.get_editor_property('rotation_mode'))\n"
            "    ns.set_editor_property('rotation_alpha', c.get_editor_property('rotation_alpha'))\n"
            "    ns.set_editor_property('translation_mode', c.get_editor_property('translation_mode'))\n"
            "    ns.set_editor_property('translation_alpha', c.get_editor_property('translation_alpha'))\n"
            "    if name in targets:\n"
            "        ns.set_editor_property('rotation_mode', mode_enum)\n"
            "        applied.append(name)\n"
            "    new_chains.append(ns)\n"
            "stg.set_editor_property('chains_to_retarget', new_chains)\n"
            "fk.set_settings(stg)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            f'saved = bool(ed.save_asset("{rtp}", only_if_is_dirty=False))\n'
            "missing = sorted(list(targets - set(applied)))\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"applied_to": applied, "not_found": missing,'
            f' "rotation_mode": "{mode_upper}", "saved": saved'
            '}))'
        )
        result = safe_execute(conn, script)
        return _ok(result)

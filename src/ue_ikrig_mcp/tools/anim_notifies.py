"""Batch AnimNotify insertion across a folder of AnimSequences.

Modes:
  * fixed_time       — insert a notify at one or more fixed times per asset
  * normalized_time  — insert at relative positions (0..1) of each asset's length
  * every_n_frames   — insert at frame interval (useful for foot-contact tags)

For curve-threshold-triggered notifies (e.g. foot plant detection from a
'LeftFoot_Plant' float curve), this v0 release exposes the fixed/normalized
modes and leaves the curve-evaluation path as a follow-up — that needs the
IAnimationDataController read API, which is unstable across UE minor versions.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_VALID_MODES = {"fixed_time", "normalized_time", "every_n_frames"}


def register(server):
    @server.tool(
        name="batch_insert_anim_notify",
        description=(
            "Insert an AnimNotify on every AnimSequence in a folder. "
            "mode='fixed_time' with times=[0.5, 1.25] inserts at absolute "
            "seconds. mode='normalized_time' with times=[0.0, 0.5, 1.0] inserts "
            "at relative positions along each asset (0=start, 1=end). "
            "mode='every_n_frames' with interval=N inserts every N frames. "
            "track_name is the notify track to create/use (default 'Notifies'). "
            "notify_class_name (optional) names a UAnimNotify/UAnimNotifyState "
            "subclass; omit for a plain named notify."
        ),
    )
    async def batch_insert_anim_notify(
        folder_path: str,
        notify_name: str,
        mode: str = "fixed_time",
        times: list = None,
        interval_frames: int = 0,
        track_name: str = "Notifies",
        notify_class_name: str = "",
        duration: float = 0.0,
        recursive: bool = True,
    ) -> list[TextContent]:
        if mode not in _VALID_MODES:
            return _err(f"mode must be one of {sorted(_VALID_MODES)}")
        if mode in ("fixed_time", "normalized_time") and not times:
            return _err(f"mode={mode} requires non-empty times list")
        if mode == "every_n_frames" and interval_frames <= 0:
            return _err("mode=every_n_frames requires interval_frames > 0")

        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        fp = escape_string(folder_path)
        nn = escape_string(notify_name)
        tn = escape_string(track_name)
        ncn = escape_string(notify_class_name) if notify_class_name else ""
        mode_s = f'"{mode}"'
        times_json = json.dumps([float(t) for t in (times or [])])
        intvl = str(int(interval_frames))
        rec = "True" if recursive else "False"
        dur = str(float(duration))

        script = wrap_script(
            "import unreal\n"
            f'folder = "{fp}"\n'
            f'notify_name = "{nn}"\n'
            f'track_name = "{tn}"\n'
            f'notify_class_name = "{ncn}"\n'
            f"mode = {mode_s}\n"
            f"times = {times_json}\n"
            f"interval = {intvl}\n"
            f"duration = {dur}\n"
            f"rec_v = {rec}\n"
            "eal = unreal.EditorAssetLibrary\n"
            "paths = eal.list_assets(folder, recursive=rec_v, include_folder=False)\n"
            "ed = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)\n"
            "succeeded = []\n"
            "failed = []\n"
            "notify_cls = None\n"
            "if notify_class_name:\n"
            "    try:\n"
            "        notify_cls = unreal.load_class(None, notify_class_name)\n"
            "    except Exception:\n"
            "        notify_cls = None\n"
            "for p in paths:\n"
            "    d = eal.find_asset_data(p)\n"
            "    try: cls = str(d.asset_class_path.asset_name)\n"
            "    except Exception: cls = str(d.asset_class)\n"
            "    if cls != 'AnimSequence': continue\n"
            "    try:\n"
            "        anim = unreal.load_asset(p)\n"
            "        if anim is None:\n"
            "            failed.append({'path': p, 'err': 'load_asset returned None'}); continue\n"
            "        # Ensure track exists\n"
            "        existing = []\n"
            "        try:\n"
            "            existing = unreal.AnimationLibrary.get_animation_track_names(anim)\n"
            "        except Exception:\n"
            "            pass\n"
            "        if track_name not in existing:\n"
            "            try:\n"
            "                unreal.AnimationLibrary.add_anim_notify_track(anim, track_name, unreal.LinearColor(0.4, 0.8, 0.4))\n"
            "            except Exception: pass\n"
            "        # Compute insertion times\n"
            "        length = float(anim.get_editor_property('sequence_length')) if hasattr(anim, 'sequence_length') else None\n"
            "        if length is None:\n"
            "            try: length = float(anim.get_play_length())\n"
            "            except Exception: length = 0.0\n"
            "        if length <= 0:\n"
            "            failed.append({'path': p, 'err': 'cannot determine sequence length'}); continue\n"
            "        # Determine rate for every_n_frames\n"
            "        rate = 30.0\n"
            "        try:\n"
            "            fr = anim.get_sampling_frame_rate()\n"
            "            rate = fr.as_decimal() if hasattr(fr, 'as_decimal') else float(fr)\n"
            "        except Exception: pass\n"
            "        insert_times = []\n"
            "        if mode == 'fixed_time':\n"
            "            insert_times = [t for t in times if 0.0 <= t <= length]\n"
            "        elif mode == 'normalized_time':\n"
            "            insert_times = [max(0.0, min(length, t * length)) for t in times]\n"
            "        elif mode == 'every_n_frames':\n"
            "            frames = int(length * rate)\n"
            "            insert_times = [(f / rate) for f in range(0, frames + 1, interval) if (f / rate) <= length]\n"
            "        inserted = 0\n"
            "        for t in insert_times:\n"
            "            try:\n"
            "                if notify_cls is not None:\n"
            "                    unreal.AnimationLibrary.add_anim_notify_event(anim, track_name, t, duration, notify_cls, notify_name)\n"
            "                else:\n"
            "                    unreal.AnimationLibrary.add_anim_notify_event(anim, track_name, t, duration, notify_name)\n"
            "                inserted += 1\n"
            "            except Exception:\n"
            "                pass\n"
            "        ed.save_asset(p, only_if_is_dirty=False)\n"
            "        succeeded.append({'path': p, 'inserted': inserted})\n"
            "    except Exception as e:\n"
            "        failed.append({'path': p, 'err': str(e)[:200]})\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"succeeded_count": len(succeeded),'
            '"succeeded_sample": succeeded[:10],'
            '"failed_count": len(failed),'
            '"failed_sample": failed[:10]'
            '}))'
        )
        result = conn.execute(script)
        return _ok(result)

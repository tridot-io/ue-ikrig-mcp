"""Retargeter preview capture without a C++ plugin.

The earlier ``capture_viewport`` tool failed because UE's Python
``take_high_res_screenshot`` / the ``HighResShot`` console command both
target the level editor viewport — not the IK Retargeter asset editor's
preview viewport (which lives in a detached Slate window that only a
compiled C++ module can reach).

Workaround: reconstruct the preview in the level editor. Spawn the source
mesh and the target mesh as ``SkeletalMeshActor`` pairs at an offset,
apply the retarget pose's rotation offsets directly to the target's bones
(so it visibly matches the retargeter's target-pose preview), frame both
with the editor camera, and capture via ``HighResShot`` — which works
reliably for the level viewport. Returns the PNG as inline MCP
``ImageContent`` so the caller sees the result without a human in the
loop.

Cleanup is automatic — the spawned actors are destroyed after capture
unless ``keep_actors=True`` is passed (for when you want to iterate on
tuning with the preview actors staying in the scene).
"""

import asyncio
import base64
import json
import os
import time

from mcp.types import ImageContent, TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string


def _ok_text(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


_PREVIEW_SCRIPT = r"""
import unreal
import os as _os
import time as _time
import json as _json

rtg = unreal.load_asset("{rtp}")
if rtg is None:
    raise ValueError("IKRetargeter not found: {rtp}")
ctrl = unreal.IKRetargeterController.get_controller(rtg)
SRC = unreal.RetargetSourceOrTarget.SOURCE
TGT = unreal.RetargetSourceOrTarget.TARGET

src_rig = ctrl.get_ik_rig(SRC)
tgt_rig = ctrl.get_ik_rig(TGT)
if src_rig is None or tgt_rig is None:
    raise ValueError("Retargeter is missing source or target IK Rig")
src_mesh = unreal.IKRigController.get_controller(src_rig).get_skeletal_mesh()
tgt_mesh = unreal.IKRigController.get_controller(tgt_rig).get_skeletal_mesh()
if src_mesh is None or tgt_mesh is None:
    raise ValueError("IK Rig(s) have no skeletal mesh assigned")

# Clean up any leftover preview actors from an earlier run with the same tag
_PREVIEW_TAG = "__ue_ikrig_mcp_preview__"
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem) if hasattr(unreal, 'EditorActorSubsystem') else None
world_actors = []
try:
    if eas is not None:
        world_actors = eas.get_all_level_actors()
except Exception:
    world_actors = []
cleaned = 0
for a in world_actors:
    try:
        if a and a.tags and unreal.Name(_PREVIEW_TAG) in a.tags:
            unreal.EditorLevelLibrary.destroy_actor(a)
            cleaned += 1
    except Exception:
        pass

# Spawn source and target actors at an X offset so they sit side by side.
spacing = float({spacing_cm})
src_loc = unreal.Vector(0.0, 0.0, 0.0)
tgt_loc = unreal.Vector(0.0, spacing, 0.0)
src_actor = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, src_loc, unreal.Rotator(0,0,0))
tgt_actor = unreal.EditorLevelLibrary.spawn_actor_from_class(unreal.SkeletalMeshActor, tgt_loc, unreal.Rotator(0,0,0))

# Tag for cleanup
for a in (src_actor, tgt_actor):
    try:
        a.tags = [unreal.Name(_PREVIEW_TAG)]
    except Exception:
        pass
    # Make them transient so they don't dirty the map
    try:
        a.set_editor_property("is_temporarily_hidden_in_editor", False)
    except Exception:
        pass

src_smc = src_actor.get_component_by_class(unreal.SkeletalMeshComponent)
tgt_smc = tgt_actor.get_component_by_class(unreal.SkeletalMeshComponent)
src_smc.set_skeletal_mesh_asset(src_mesh)
tgt_smc.set_skeletal_mesh_asset(tgt_mesh)

# Apply target retarget pose offsets directly to the target actor's bones.
# The rotation offsets are stored as world-space Quat in the retarget pose;
# we apply them bone-local since set_bone_rotation_by_name in BONE_SPACE
# composes on top of the ref pose, which is what the retargeter editor
# visualizes.
applied_bones = 0
for i in range(tgt_smc.get_num_bones()):
    b = tgt_smc.get_bone_name(i)
    try:
        q = ctrl.get_rotation_offset_for_retarget_pose_bone(b, TGT)
        rot = q.rotator()
        # Skip near-identity offsets
        if abs(rot.roll) < 0.01 and abs(rot.pitch) < 0.01 and abs(rot.yaw) < 0.01:
            continue
        tgt_smc.set_bone_rotation_by_name(b, rot, unreal.EBoneSpaces.BONE_SPACE)
        applied_bones += 1
    except Exception:
        pass

# Also apply the target root translation offset if present
try:
    root_off = ctrl.get_root_offset_in_retarget_pose(TGT)
    if abs(root_off.x) + abs(root_off.y) + abs(root_off.z) > 0.001:
        tgt_actor.set_actor_location(
            unreal.Vector(tgt_loc.x + root_off.x, tgt_loc.y + root_off.y, tgt_loc.z + root_off.z),
            False, False)
except Exception:
    pass

# Frame the level editor camera to show both characters.
# Combine both meshes' bounding spheres; aim camera a few meters back.
center_x = 0.5 * (src_loc.x + tgt_loc.x)
center_y = 0.5 * (src_loc.y + tgt_loc.y)
center_z = 100.0  # roughly character waist height
cam_loc = unreal.Vector(center_x - 300.0, center_y, center_z + 30.0)
cam_rot = unreal.Rotator(roll=0.0, pitch=-5.0, yaw=0.0)
try:
    if hasattr(unreal, 'UnrealEditorSubsystem'):
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        ues.set_level_viewport_camera_info(cam_loc, cam_rot)
except Exception:
    pass

# Force the viewport to redraw before the screenshot request
try:
    if hasattr(unreal, 'LevelEditorSubsystem'):
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        les.editor_invalidate_viewports()
        les.editor_set_viewport_realtime(True, False)
except Exception:
    pass

# Capture via HighResShot (level editor viewport). Default output folder:
# Saved/Screenshots/Windows/HighresScreenshot_####.png
saved_dir = unreal.Paths.project_saved_dir()
ss_dir = _os.path.join(saved_dir, "Screenshots", "Windows")
_os.makedirs(ss_dir, exist_ok=True)
before_files = set(_os.listdir(ss_dir)) if _os.path.isdir(ss_dir) else set()

cmd = "HighResShot {width}x{height}"
unreal.SystemLibrary.execute_console_command(None, cmd)

print("__MCP_RESULT__" + _json.dumps({{
    "src_actor_path": src_actor.get_path_name(),
    "tgt_actor_path": tgt_actor.get_path_name(),
    "applied_bones": applied_bones,
    "cleaned_prior_preview_actors": cleaned,
    "screenshot_dir": ss_dir,
    "before_count": len(before_files),
}}))
"""


_CLEANUP_SCRIPT = r"""
import unreal
_PREVIEW_TAG = "__ue_ikrig_mcp_preview__"
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem) if hasattr(unreal, 'EditorActorSubsystem') else None
removed = 0
actors = []
try:
    actors = eas.get_all_level_actors() if eas else []
except Exception:
    pass
for a in actors:
    try:
        if a and a.tags and unreal.Name(_PREVIEW_TAG) in a.tags:
            unreal.EditorLevelLibrary.destroy_actor(a)
            removed += 1
    except Exception:
        pass
print("__MCP_RESULT__" + __import__('json').dumps({"removed": removed}))
"""


def register(server):
    @server.tool(
        name="preview_retargeter_pose",
        description=(
            "Pure-Python visual preview of a retargeter's target pose (no "
            "C++ plugin required). Spawns the source and target skeletal "
            "meshes as level-editor actors side by side, applies the target "
            "retarget pose's rotation offsets to the target's bones so it "
            "visually matches the IK Retargeter asset editor's preview, "
            "frames both in the editor camera, and captures the level "
            "viewport via HighResShot. Returns the PNG inline as "
            "ImageContent. "
            "spacing_cm controls how far apart the two characters are "
            "spawned. keep_actors=True leaves the preview actors in the "
            "scene for iterative tuning; default cleans them up after "
            "capture. Note: this modifies the currently-open level "
            "(transient actors tagged for cleanup); save your work first "
            "if you're worried about dirtying the scene."
        ),
    )
    async def preview_retargeter_pose(
        retargeter_path: str,
        spacing_cm: float = 200.0,
        width: int = 1920,
        height: int = 1080,
        keep_actors: bool = False,
    ) -> list:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return [TextContent(type="text", text=str(e))]

        rtp = escape_string(retargeter_path)
        script = _PREVIEW_SCRIPT.format(
            rtp=rtp,
            spacing_cm=float(spacing_cm),
            width=int(width),
            height=int(height),
        )
        script = wrap_script(script)

        t_start = time.time()
        spawn_result = conn.execute(script)
        parsed = spawn_result.get("parsed") if isinstance(spawn_result, dict) else None
        if parsed is None:
            return _err(f"preview_retargeter_pose: spawn/capture request failed. Raw: {spawn_result!r}")

        ss_dir = parsed.get("screenshot_dir")
        before_count = parsed.get("before_count", 0)
        if not ss_dir:
            return _err(f"preview_retargeter_pose: no screenshot_dir returned. Parsed: {parsed!r}")

        # Poll for a new PNG to appear in the screenshots dir (UE writes the
        # shot asynchronously on the next render tick, so we have to wait).
        deadline = time.time() + 15.0
        new_file = None
        while time.time() < deadline:
            try:
                if os.path.isdir(ss_dir):
                    current = sorted(os.listdir(ss_dir))
                    if len(current) > before_count:
                        # Pick the newest .png
                        pngs = [p for p in current if p.lower().endswith(".png")]
                        if pngs:
                            newest = max(pngs, key=lambda n: os.path.getmtime(os.path.join(ss_dir, n)))
                            if os.path.getsize(os.path.join(ss_dir, newest)) > 0:
                                new_file = os.path.join(ss_dir, newest)
                                break
            except OSError:
                pass
            await asyncio.sleep(0.25)

        contents: list = []
        if new_file and os.path.exists(new_file):
            try:
                with open(new_file, "rb") as fh:
                    data = fh.read()
                b64 = base64.b64encode(data).decode("ascii")
                contents.append(ImageContent(type="image", data=b64, mimeType="image/png"))
                contents.append(TextContent(
                    type="text",
                    text=json.dumps({
                        "ok": True,
                        "screenshot": new_file,
                        "bytes": len(data),
                        "applied_bones": parsed.get("applied_bones"),
                        "elapsed_s": round(time.time() - t_start, 2),
                    }, indent=2),
                ))
            except Exception as e:
                contents.append(TextContent(type="text", text=f"Read/encode failed: {e}. Screenshot at {new_file}"))
        else:
            contents.append(TextContent(
                type="text",
                text=json.dumps({
                    "warning": "Screenshot did not appear within 15s. Actors spawned; run HighResShot manually or check Saved/Screenshots/Windows/.",
                    "screenshot_dir": ss_dir,
                    "spawn_result": parsed,
                }, indent=2),
            ))

        if not keep_actors:
            # Clean up the transient actors so we don't leave the scene dirty
            conn.execute(wrap_script(_CLEANUP_SCRIPT))

        return contents

    @server.tool(
        name="cleanup_retargeter_preview_actors",
        description=(
            "Remove any leftover preview actors spawned by "
            "preview_retargeter_pose (tagged with __ue_ikrig_mcp_preview__). "
            "Use this if a previous preview call was cancelled or if you "
            "passed keep_actors=True and want to clean up now."
        ),
    )
    async def cleanup_retargeter_preview_actors() -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        result = conn.execute(wrap_script(_CLEANUP_SCRIPT))
        return _ok_text(result)
# ue-ikrig-mcp — Feature Roadmap

## Context
Starting point: `v0.3.0`. Current surface is ~36 tools covering connection, IK rig CRUD, retargeter CRUD, bone-offset fine-tuning, batch retarget, inline screenshot (level-editor only), and three advanced tools promoted during the asooni→MetaHuman session (`inspect_retargeter_full`, `set_ik_chain_static_offset`, `measure_bone_ref_pose_delta`).

This roadmap is scoped from the actual friction observed during that session. Ordering is by value-per-unit-complexity, with explicit dependencies so phases can ship independently.

## Goals
1. Cut time-to-tune for a new retargeter by ≥ 80% (from manual per-bone to preset-and-verify).
2. Make symmetric tuning (L ↔ R) a single tool call, not two.
3. Make "what is wrong with this retargeter?" answerable without a human eyeball.
4. Mutate the UE 5.6 op stack as first-class data (add/remove/reconfigure ops).
5. Save / load whole retargeter configurations as JSON, with built-in presets for the common rig pairings.

## Non-Goals (this iteration)
- Generic UE editor automation beyond IK rig / retargeter domain
- Runtime gameplay retargeting (the MCP is editor-only)
- Animation authoring / keyframe editing

## Dependency Graph
```
Phase 1 (Ergonomics) ────────┐
                             ├─► Phase 4 (Presets & I/O)
Phase 2 (Validation) ────────┤
                             │
Phase 3 (Op Stack) ──────────┘

Phase 5 (Visual Feedback) — independent, can start any time;
                            flagged as spike because of Slate complexity
```

Phases 1–3 are independent and parallelizable. Phase 4 depends on the read/write APIs from 1–3. Phase 5 is orthogonal; keep it on a separate track so Slate-widget unknowns don't block value shipping.

---

## Phase 1 — Tuning Ergonomics
**Goal**: cut per-iteration friction during bone-offset fine-tuning.

### Tools

**`mirror_bone_offsets(retargeter_path, side: 'LtoR'|'RtoL', bones: list[str] | None = None)`**
- Reads offsets from L bones (e.g. `hand_l`, `foot_l`, `upperarm_l`) and writes mirrored quaternions to R counterparts.
- Mirror formula: negate Y and Z components of the rotation axis (standard symmetry across the YZ plane for UE's default skeleton orientation).
- `bones=None` → auto-detect pairs by `_l/_r` suffix.
- Returns dict `{source_bone: [offset_deg], target_bone: [mirrored_offset_deg]}` for every pair.

**`batch_set_bone_rotation_offset(retargeter_path, offsets: dict[str, list[float]], source_or_target='Target')`**
- `offsets` is `{bone_name: [roll, pitch, yaw]}` in degrees.
- One UE round-trip instead of N. Returns before/after for each bone.

**`duplicate_retarget_pose(retargeter_path, new_pose_name, source_or_target='Target', set_current=False)`**
- Calls `ctrl.duplicate_retarget_pose(current, new_pose_name, side)`.
- If `set_current=True`, also switches to the new pose so subsequent tweaks don't clobber the snapshot.

**`reset_bones_to_reference(retargeter_path, bones: list[str], source_or_target='Target')`**
- Sets every listed bone's offset back to identity quaternion. Useful for reverting a tuning branch without affecting the rest of the pose.

### Acceptance Criteria
- `mirror_bone_offsets` produces visually-mirrored poses for all 6 standard humanoid L/R pairs (upperarm, lowerarm, hand, thigh, calf, foot) in a single call.
- `batch_set_bone_rotation_offset` applies 10 offsets in one call and under one second (network + UE Python round-trip budget).
- `duplicate_retarget_pose` appears as a selectable entry in the retargeter editor's pose dropdown after the call.
- `reset_bones_to_reference` restores offset `[0, 0, 0]` for every listed bone, verified via `get_bone_rotation_offset`.

### Risks
- **Mirror formula**: UE mirrors via `Quat(x=-q.x, y=q.y, z=q.z, w=-q.w)` for standard Y-axis-plane symmetry. Need to verify against actual skeleton orientation. *Mitigation*: unit-test against a known L/R pair by round-tripping (mirror L→R→L should equal input).
- **Name suffix conventions vary**: Mixamo uses `Left/Right` prefix, MetaHuman uses `_l/_r` suffix, UE4 Mannequin uses `_l/_r`. *Mitigation*: accept a custom regex / explicit bone list parameter.

### Files touched
- New: `src/ue_ikrig_mcp/tools/ergonomics.py`
- Modified: `src/ue_ikrig_mcp/server.py` (register)
- Bump to `0.4.0`

---

## Phase 2 — Validation & Diagnostics
**Goal**: detect retargeter problems before the artist's eyeball does.

### Tools

**`validate_retargeter(retargeter_path) → list[Warning]`**
Checks:
- **Structural**: any target chains are mapped to `None`, any source chains unused
- **Balance**: L/R offset asymmetry greater than N° on paired bones (suggests tuning missed the mirror)
- **IK sanity**: any IK chain with `EnableIK=False` (probably a mistake), any IK chain with `StaticOffset` magnitude > 50 cm (probably a typo), any IK goal bone missing from target rig
- **Op stack sanity**: disabled Pelvis Motion (character won't move), missing Run IK Rig op (IK goals won't actuate), ScaleSourceFactor outside [0.5, 2.0]
- **Pose reference**: target retarget pose name doesn't match any built-in MH preset (warn only, not error)

Each warning: `{severity: 'error'|'warn'|'info', code: str, message: str, fix_hint: str}`.

**`diff_retargeters(retargeter_a, retargeter_b) → Diff`**
- Side-by-side JSON diff of `inspect_retargeter_full` output for two retargeters.
- Groups diffs by category: mappings, poses, per-chain IK, op-stack settings, bone offsets.
- Useful for reviewing teammate changes or comparing two tuning experiments.

**`detect_skeleton_convention(ik_rig_path) → {convention: str, confidence: float, template_suggestions: list[str]}`**
- Fingerprints a skeleton via bone-name matching:
  - Mixamo: `Hips`, `Spine`, `LeftUpLeg`, `LeftArm`, ...
  - MetaHuman: `pelvis`, `spine_01..05`, `thigh_l`, `upperarm_l`, `root`, twist chains
  - UE5 Mannequin: `pelvis`, `spine_01..05`, `thigh_l`, `upperarm_l`, no twist chains
  - Mannequin (UE4): `pelvis`, `spine_01..03`, `thigh_l`, `upperarm_l`
- Returns suggested preset name(s) for Phase 4.

### Acceptance Criteria
- `validate_retargeter` on the `RTG_asooni` asset as it was pre-fix returns at least 3 warnings: `unmapped_target_chain`, `unset_target_pose_body_focus` (face-focused pose), `missing_ik_static_offset_on_legs`.
- `diff_retargeters` between a pre-fix and post-fix snapshot of the same retargeter lists every change we made this session.
- `detect_skeleton_convention` returns `{convention: 'metahuman', confidence: ≥ 0.9}` for `IK_MetaHuman_f_tal_unw` and `{convention: 'mixamo', confidence: ≥ 0.8}` for `IK_asooni`.

### Risks
- **L/R balance thresholds are subjective**: one-handed dance moves legitimately break symmetry. *Mitigation*: warn-level only; threshold configurable.
- **Skeleton fingerprinting is heuristic**: custom rigs won't match. *Mitigation*: return `unknown` with low confidence rather than misclassify; let users tag their own convention in a future custom-preset system.

### Files touched
- New: `src/ue_ikrig_mcp/tools/validation.py`
- New: `src/ue_ikrig_mcp/data/skeleton_fingerprints.json` (bone-name sets per convention)
- Modified: `server.py`
- Bump to `0.5.0`

---

## Phase 3 — Op-Stack Operations
**Goal**: make UE 5.6's new op-stack architecture first-class.

### Tools

**`add_speed_planting_op(retargeter_path, chains=['LeftLeg', 'RightLeg'], speed_threshold=15.0, stiffness=250.0)`**
- Adds a Speed Planting op at the end of the stack.
- Populates `ChainsToSpeedPlant` with the given target chain names.
- This is the UE-sanctioned fix for "feet slide during slow frames" — we skipped it in-session by using `StaticOffset`, but for locomotion dances this is the better knob.

**`add_stride_warping_op(retargeter_path, direction_source='Goals', forward_axis='Y')`**
- Adds a Stride Warping op with sensible defaults for locomotion retarget.

**`configure_pelvis_motion(retargeter_path, **settings)`**
- Typed setter for the Pelvis Motion op's settings struct: `translation_alpha`, `scale_horizontal`, `scale_vertical`, `affect_ik_horizontal`, `affect_ik_vertical`, `rotation_alpha`, `blend_to_source_translation`.
- One call instead of `get_settings → mutate → set_settings`.

**`remove_op(retargeter_path, op_name_or_type)`** / **`move_op(retargeter_path, op_name, new_index)`**
- Pull ops out or reorder. Useful when copying a stack template from another retargeter.

### Acceptance Criteria
- `add_speed_planting_op` produces a stack where `IKRetargetSpeedPlantingController` is present at the specified index, with `ChainsToSpeedPlant` populated from the argument.
- `configure_pelvis_motion(translation_alpha=0.5)` halves vertical bobbing on a vertical-heavy animation, confirmed via `inspect_retargeter_full` showing the updated field.
- `remove_op` then re-add produces the same `inspect_retargeter_full` output as the original (modulo op index).

### Risks
- **Op-stack index semantics**: ops are ordered (execution order matters — e.g. Pelvis Motion must run before IK Goals). *Mitigation*: `add_speed_planting_op` defaults to "end of stack, after Run IK Rig"; document ordering constraints.
- **UE API discovery**: `add_retarget_op` signature needs probing. *Mitigation*: do the introspection in a spike before coding the typed wrapper.

### Files touched
- New: `src/ue_ikrig_mcp/tools/op_stack.py`
- Modified: `server.py`
- Bump to `0.6.0`

---

## Phase 4 — Presets & Config I/O
**Goal**: save whole retargeter configurations as JSON; ship presets that bypass 90% of manual setup.

### Tools

**`export_retargeter_config(retargeter_path) → JSON`**
- Dumps: source/target rig paths, all chain mappings, all named retarget poses (with per-bone offsets in degrees), per-chain IK settings, every op's serialized settings, root offsets.
- Version-tagged (`{"schema_version": 1, ...}`) so imports can validate compatibility.

**`import_retargeter_config(retargeter_path, config: dict, apply: list[str] = None)`**
- Applies a previously-exported config to a (possibly fresh) retargeter.
- `apply` selectively chooses what to restore: `['mappings', 'poses', 'ik_chain_settings', 'ops']`. Default: everything.
- Errors gracefully on schema mismatch.

**`apply_preset(retargeter_path, preset_name: str)`**
- Built-in presets (shipped as JSON files under `src/ue_ikrig_mcp/data/presets/`):
  - `mixamo_to_metahuman_female_tall`
  - `mixamo_to_metahuman_male_tall`
  - `ue5_mannequin_to_metahuman`
  - `ue4_mannequin_to_metahuman`
- Each preset baked from the corresponding tuned retargeter (including the asooni→MH setup from this session).

**`list_available_presets() → list[{name, source_convention, target_convention, description}]`**
- Returns metadata about shipped presets plus any user-supplied ones in a configurable directory.

### Acceptance Criteria
- `export_retargeter_config` on `RTG_asooni` produces a JSON object that, when passed to `import_retargeter_config` on a fresh retargeter, results in byte-identical `inspect_retargeter_full` output (modulo unique IDs).
- `apply_preset(rtg, 'mixamo_to_metahuman_female_tall')` on a freshly-created, empty retargeter produces a working retargeter that would have skipped most of this session's manual work.
- Preset import is idempotent: calling it twice produces the same state as calling it once.

### Risks
- **Asset references in JSON**: preset must not hard-code `/Game/DLC/Dance/NoPackage/IK/IK_asooni` since that's project-specific. *Mitigation*: presets only encode mappings, offsets, op settings; the user supplies source/target rig assets as arguments. Presets name the expected skeleton convention, not specific assets.
- **Offset universality**: the 1.08 cm foot offset we computed for asooni might not apply to every Mixamo character. *Mitigation*: preset includes a "call `measure_bone_ref_pose_delta` and use delta" hook rather than a hard-coded number, OR skip offsets from the preset and leave them to a post-import tuning pass.

### Files touched
- New: `src/ue_ikrig_mcp/tools/config_io.py`
- New: `src/ue_ikrig_mcp/data/presets/*.json`
- New: `src/ue_ikrig_mcp/data/schema/retargeter_config_v1.json`
- Modified: `server.py`
- Bump to `0.7.0`

---

## Phase 5 — Visual Feedback Loop (Spike + Maybe-Build)
**Goal**: `capture_viewport` currently captures level-editor only, not the IK Retargeter's asset-editor preview. Fixing this properly requires Slate-widget work.

### Spike (first)
- Investigate three capture paths:
  1. **Slate widget walk** — `FSlateApplication::Get().FindWidgetInWindows(...)` + `TakeScreenshot`. Not Python-exposed; may need a C++ side module.
  2. **Render target** — spawn a `USceneCaptureComponent2D` aimed at the retarget preview actors; less accurate but Python-accessible.
  3. **Docked level viewport** — spawn source + target skeletal meshes into a hidden sublevel, drive them via a preview anim BP, and use level-editor `HighResShot` which already works. Most tractable, least complex.
- Deliverable: a memo recommending one of the three, with a prototype of the chosen path.

### Tools (if spike succeeds)
- **`capture_retargeter_preview(retargeter_path, frame=None)`** — produces an inline PNG of the retargeter's preview at the specified animation frame.
- **`ab_capture(retargeter_path, changes_a: dict, changes_b: dict)`** — applies `changes_a`, captures; reverts; applies `changes_b`, captures; returns two images + the diff of settings.
- **`render_animation_key_frames(retargeter_path, anim_path, frames=[0, 25, 50, 75, 100])`** — renders multiple frames through the retargeter, returns as a vertical-stitched inline PNG.

### Acceptance Criteria (if building)
- `capture_retargeter_preview` returns an inline PNG containing both source and target characters, where the previously-failed `capture_viewport` returned an empty level editor.
- `ab_capture` produces two images showing identical viewing angles with only the retarget-pose offsets differing.

### Risks
- **Slate path is C++-only**: would require shipping a compiled editor plugin alongside the Python MCP. *Mitigation*: prefer Path 3 (level-viewport) even though it's hackier.
- **Frame-accurate animation drive**: syncing the preview's animation scrubber to a specific frame from Python may not be supported. *Mitigation*: spike validates this before build commits.

### Files touched (tentative)
- New: `src/ue_ikrig_mcp/tools/preview.py`
- Possibly new: C++ plugin module under `Plugins/UEIKRigMCPCapture/` in the user's UE project
- Bump to `0.8.0` (or `0.9.0` if we skip this phase)

---

## Verification
Every phase ships with:
- Unit-level verification: new tool probed in UE editor end-to-end, result matches acceptance criteria
- Integration: run `inspect_retargeter_full` before and after the phase's operations; diff via `diff_retargeters` (Phase 2) to show exactly what changed
- Regression: previous-phase tools still pass their own acceptance criteria after the new phase lands
- Version bump + commit + push to `origin/main`; user `/reload-plugins` to pick up

## Follow-ups (post-roadmap)
- **Custom skeleton conventions**: let users register their own fingerprint + preset so Phase 4 presets cover proprietary rigs.
- **Retargeter tuning session recorder**: log every tool call with timestamps; replay on a different retargeter as a "macro". Near-free given the op-stack + config I/O infrastructure.
- **Integration with `oh-my-claudecode`'s `trace` skill**: every tuning session auto-snapshots state at decision points, traceable for post-mortem.
- **MCP-layer caching**: `inspect_retargeter_full` is heavy; cache with invalidation on any setter call from the MCP. Reduces round-trips during validation sweeps.

## Changelog
- 2026-04-20 — initial roadmap draft (Claude, post asooni→MH session)
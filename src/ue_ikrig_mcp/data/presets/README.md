# Retargeter Presets

JSON presets consumable by `apply_preset(retargeter_path, preset_name)`.

## Creating a new preset

Once you've tuned a retargeter to your liking, export it straight into this directory:

```python
export_retargeter_config(
    retargeter_path="/Game/DLC/Dance/NoPackage/RTG/RTG_asooni.RTG_asooni",
    save_as_preset="mixamo_to_metahuman_female_tall",
)
```

That writes `mixamo_to_metahuman_female_tall.json` next to this README. It strips the source/target rig asset paths (those are project-specific) so the preset works for any rig following the matching skeleton convention.

After saving, the preset will show up in `list_available_presets()` and is applicable via:

```python
apply_preset(
    retargeter_path="/Game/SomeOther/RTG_NewRetargeter.RTG_NewRetargeter",
    preset_name="mixamo_to_metahuman_female_tall",
)
```

## Naming convention

`<source_convention>_to_<target_convention>[_<variant>].json`

Examples:
- `mixamo_to_metahuman_female_tall.json`
- `mixamo_to_metahuman_male_tall.json`
- `ue5_mannequin_to_metahuman.json`
- `ue4_mannequin_to_metahuman.json`
- `metahuman_to_metahuman.json`

## What gets applied

By default `apply_preset` restores three sections:

- `mappings` — chain mappings (target chain → source chain)
- `poses` — all named retarget poses on both sides, with root offsets and non-identity per-bone rotation offsets in degrees
- `ik_chain_settings` — per-chain IK `StaticOffset`, `BlendToSource*`, `EnableIK`, etc. from the Retarget IK Goals op

Opt-in via `apply_sections=['ops']` also replaces the entire op stack (structure + settings). Excluded by default because it's destructive — the caller usually wants to keep their existing ops and just layer the skeleton-specific tuning on top.

## Schema

See `../schema/retargeter_config_v1.json` for the schema reference. Schema version is currently `1`. Presets with mismatched versions are rejected at import time with a clear error.
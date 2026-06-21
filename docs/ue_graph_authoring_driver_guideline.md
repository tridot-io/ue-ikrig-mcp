# Driver Guideline: UE Graph Authoring by Asset Family

> The driver **must not** treat all "Blueprint node work" as one generic capability.
> Classify the **asset family** first, then the **action**, then route to the correct Unreal surface.

## Decision flow

1. Call `blueprint_family_capabilities` before graph/node authoring.
2. Classify the asset family:
   - Blueprint / K2
   - WidgetBlueprint
   - AnimBlueprint
   - Control Rig
   - Material
   - MaterialFunction
   - MaterialInstance
3. Classify the requested action:
   - inspect
   - create asset
   - create node
   - wire pin
   - set defaults / properties
   - compile / recompile / update
   - save
4. Use the family-specific Unreal surface and current repo support label.
5. Treat unsupported or repo-not-exposed mutation paths as stop conditions unless a disposable fixture and dedicated adapter already exist.

## Guardrails

- The driver **must not** invent Unreal APIs. Resolve symbols through `search_unreal_api` / `describe_unreal_api` or verified official docs first.
- The driver **must not** borrow semantics across families. Control Rig, AnimBlueprint, WidgetBlueprint, Blueprint/K2, Material, MaterialFunction, and MaterialInstance are not interchangeable graph systems.
- Unsupported paths **must** fail explicitly. The driver **must not** simulate success or silently downgrade behavior.
- When a family requires compile/update + save, treat those steps as part of the mutation and verify them with disposable fixtures, dirty-package checks, re-query evidence, and result reporting.
- TAPython graph APIs are interactive editor UI / Chameleon-context bound in this MCP. TAPython is a diagnostic/capture surface here, not an arbitrary asset-path graph authoring fallback.

## Capability matrix

| Asset family | Canonical UE surface | Inspect | Create asset | Create node | Wire pin | Set defaults / properties | Compile / recompile | Save | Current driver stance |
|---|---|---|---|---|---|---|---|---|---|
| **Blueprint / K2** | `BlueprintEditorLibrary` + Blueprint asset/graph APIs | **Partial today** via catalog / `describe_unreal_api`; future `blueprint_describe_asset(asset_path, include_graphs=True)` | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **Partial** for asset-level vars / properties | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | Do not reuse Control Rig, AnimGraph, Material, or TAPython graph semantics. Generic K2 node creation/wiring must stop until a K2-specific adapter is fixture-tested. |
| **WidgetBlueprint** | WidgetBlueprint asset + WidgetTree/designer tree + Blueprint graph semantics | **Partial today** via catalog / describe; future `widget_describe_tree` | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | Classify designer-tree child vs graph node vs binding/MVVM before acting. Do not collapse WidgetBlueprint into generic Blueprint/K2. |
| **AnimBlueprint** | AnimBlueprint / AnimationGraph inspection APIs; targeted existing-node tools | **Partial today** for supported existing nodes such as TwoBoneIK | **Unknown / out of current scope** | **Unsupported headless authoring** | **Unsupported headless authoring** | **Partial** through targeted existing-node tools | **UE-supported, repo-not-exposed** | **Partial** through targeted update tools only | Stock UE exposes useful inspection APIs, but arbitrary AnimGraph node creation/wiring is unsupported through this MCP. TAPython creation/dump/apply stubs intentionally refuse arbitrary asset-path graph authoring. |
| **Control Rig** | `RigVMController` + `ControlRigBlueprintLibrary.recompile_vm` | **Supported today** | **Supported today** | **Supported today** | **Supported today** | **Supported today** | **Supported today** | **Supported today** | This is the repo’s mature graph-authoring family. Use dedicated `cr_*` tools and verify with graph dump + compile/save. |
| **Material** | `MaterialEditingLibrary` expression / connection APIs | **Engine-supported, repo-not-exposed**; project material assets are now catalog-backed | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | Material expression graphs are not Blueprint/K2 or RigVM graphs. Add read-only `material_describe_graph` before mutation adapters. |
| **MaterialFunction** | Material function expression APIs through `MaterialEditingLibrary` | **Engine-supported, repo-not-exposed**; project material functions are now catalog-backed | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | MaterialFunction expression graphs are distinct from Material assets. Add read-only `material_function_describe_graph` before mutation adapters. |
| **MaterialInstance** | Material instance parameter overrides; no expression graph | **Partial today** via catalog / describe; future `material_instance_describe_parameters` | **UE-supported, repo-not-exposed** | **Unsupported** | **Unsupported** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | **UE-supported, repo-not-exposed** | MaterialInstance assets are first-class catalog/router entries, but they do not have expression nodes or pins. Treat parameter overrides as a separate fixture-gated surface. |

## Project catalog kinds

`build_api_catalog` / `search_unreal_api` project-layer kinds currently include:

- `blueprint`
- `widget`
- `animbp`
- `material`
- `material_function`
- `material_instance`
- `struct`
- `enum`
- `dataasset`

Catalog visibility is **not** mutation support. It only means agents can find and describe project assets without guessing paths or dumping `dir(unreal)`.

## Support labels

- **Supported today**: the repo exposes a dedicated tool with verification expectations.
- **Partial today**: only a bounded subset is supported; do not generalize beyond the stated subset.
- **UE-supported, repo-not-exposed**: Unreal has an engine/API surface, but this repo does not yet expose a dedicated safe adapter or fixture contract.
- **Unsupported**: fail explicitly instead of guessing.
- **Unknown / out of current scope**: do not proceed without a new fixture-backed design.

## Dirty-package verification pattern

Before claiming a mutation was cleanly compiled/saved, fixture scripts should capture dirty package state through:

```python
dirty_content = unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
dirty_maps = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
dirty_names = [str(pkg.get_name()) for pkg in dirty_content + dirty_maps]
```

Do not rely on guessed `Package.is_dirty()` helpers in generated scripts.

## Execution shape

```text
family = classify_asset_family(asset)
action = classify_requested_action(request)
cell = blueprint_family_capabilities(family, action)

if cell.support == "supported":
    route_to_family_specific_adapter(family, action)
elif cell.support in {"partial", "experimental"}:
    route_only_with_explicit_family_rules_and_caveats()
else:
    fail_explicitly_with_supported_fallback_message()
```

## Bottom line

The driver should answer these questions in order:

1. What asset family is this?
2. What action is being requested?
3. What is the correct Unreal surface for that family/action pair?
4. Is that path supported in this repo today?
5. What fixture, dirty-package, compile/save, and re-query evidence is required before a mutation claim?

Only then should the driver decide what node, graph, parameter, or asset mutation to perform.

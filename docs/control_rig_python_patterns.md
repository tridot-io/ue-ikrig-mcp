# Control Rig Python Authoring — Patterns & Pitfalls

Captured from hands-on authoring against UE 5.6, validated against the shipping
`CreateAutoJCMControlRig.py` in `daz3d/DazToUnreal`. This doc is the "learning
log" for the `cr_author.py` MCP tools — read this before reaching for the raw
Python API to avoid the traps below.

---

## 1. Node-creation API is split by node family

Not all "add a node" verbs go through `add_unit_node_from_struct_path`.

| Node family | Correct Python verb |
|---|---|
| RigUnit (plain `USTRUCT + RIGVM_METHOD`) | `controller.add_unit_node_from_struct_path(struct_path, method, pos, name)` |
| Template (variadic, e.g. `Make Relative`) | `controller.add_template_node(notation, pos, name)` |
| Array ops (Get/Set/Add/Length/...) | `controller.add_array_node(RigVMOpCode.ARRAY_*, element_cpp_type, element_cpp_type_object, pos, name)` |
| Variable get/set | `controller.add_variable_node(var_name, cpp_type, cpp_type_object, is_getter, default, pos, name)` |
| BP-level input variable | `rig_bp.add_member_variable(name, cpp_type, is_input, is_output, default)` |

**Trap:** Attempting array operations via `add_unit_node_from_struct_path` always
fails with `Cannot find struct for path '/Script/ControlRig.RigUnit_ArrayGetAtIndex'`
because array ops are dispatch nodes, not plain unit nodes. The struct literally
does not exist — the runtime node is synthesized from an opcode + element type.

---

## 2. Struct paths drift between engine versions

Paths for common math units have moved between `/Script/ControlRig.RigUnit_*` and
`/Script/RigVM.RigVMFunction_*` across UE 5.0 → 5.6. Always author with a
**fallback list** and try each until one resolves:

```python
PATHS = [
    "/Script/RigVM.RigVMFunction_MathFloatMul",       # UE 5.2+
    "/Script/ControlRig.RigUnit_MathFloatMul",        # UE 5.0 / 5.1
]
```

For a full list see `cr_author.py`'s `STRUCT_PATHS` table in the project script.

**Discovery:** `dir(unreal.RigVMController)` and browsing `/Script/ControlRig`
and `/Script/RigVM` in the Content Browser's "Show Engine Content" mode.

---

## 3. RigVMOpCode values for array ops (UE 5.6)

From the Python API reference. Marked "(DEPRECATED)" — but still functional as of
5.6. The modern "replacement" is the same op_code routed through a dispatch
node, which `add_array_node` resolves to automatically.

| Enum | Use |
|---|---|
| `ARRAY_GET_AT_INDEX` | Read `arr[i]` |
| `ARRAY_SET_AT_INDEX` | Write `arr[i] = v` |
| `ARRAY_GET_NUM` / `ARRAY_SET_NUM` | Get / set length |
| `ARRAY_ADD` | Append one element |
| `ARRAY_APPEND` | Concat arrays |
| `ARRAY_CLONE`, `ARRAY_INSERT`, `ARRAY_REMOVE`, `ARRAY_RESET`, `ARRAY_REVERSE` | Other standard ops |
| `ARRAY_FIND`, `ARRAY_ITERATOR` | Search / iterate |
| `ARRAY_DIFFERENCE`, `ARRAY_INTERSECTION`, `ARRAY_UNION` | Set-like ops |

Signature reminder: `add_array_node(op, element_cpp_type, element_cpp_type_object, pos, name)`.
The `element_cpp_type` is the ELEMENT type — `"float"` for `TArray<float>`, not
`"TArray<float>"`. For struct element types (FVector, FTransform, FRigElementKey),
pass the struct class path as `element_cpp_type_object`.

---

## 4. TMap is not a supported rig input type

`Map<Name, Float>` and similar cannot be added as a rig input variable in UE 5.6 —
the CR variable-type picker filters them out. Workaround: use `TArray<T>` with
a documented, stable index order:

- Producer (C++) fills `TArray<float>` entries in a known order.
- Consumer (CR) reads by literal index per output bone.

Store a `TArray<FName>` alongside as a debug-only mapping if you need to verify
index-to-name correspondence at runtime.

---

## 5. Pin names on RigUnits are not always what the UI suggests

`FRigUnit_OffsetTransformForItem` looks like it has a `Space` pin in the editor
details panel, but attempting `set_pin_default_value('Node.Space', ...)` errors
with `Cannot find pin 'Node.Space'`. Reason: the offset is always applied in the
item's local space by design, so the struct has no `Space` member. UI "hints"
that suggest otherwise are red herrings.

**Debug tactic when a pin fails:** `cr_dump_graph` shows the exact pins on each
node after authoring. Compare expected vs actual.

Other nesting gotchas:
- `FTransform` pin expands to `.Translation`, `.Rotation`, `.Scale3D`.
- `FRigElementKey` pin expands to `.Type` (enum: `Bone`, `Control`, etc.) and `.Name`.
- Struct defaults use UE text-property form: `'(Type=Bone,Name="hand_l")'`.
  Quotes around the name are required if the name is not a simple identifier.

---

## 6. Execution flow uses `.ExecuteContext` pins on both sides

Not `.Exec` or `.Then` (those are BP conventions). In RigVM:

```python
controller.add_link(f"{node_a.get_node_path()}.ExecuteContext",
                    f"{node_b.get_node_path()}.ExecuteContext")
```

BeginExecution's output `.ExecuteContext` is the start of the Forward Solve.
Chain multiple mutating nodes in series; non-executing pure nodes (math, array
reads) do not need execution links — just data-pin links.

---

## 7. Recompile + save as one unit

After batch edits, always:

```python
unreal.ControlRigBlueprintLibrary.recompile_vm(rig_bp)
unreal.EditorAssetLibrary.save_asset(rig_path)
```

Without `recompile_vm`, the generated bytecode reflects the last compile, not
the new graph — runtime behavior won't match what you just wrote. And without
`save_asset` the asset stays dirty and can lose changes on editor restart.

---

## 8. Runtime errors != authoring errors

A fresh "just built" graph that errors with

    Array Index (N) out of bounds (count 0)

is **working**. The compiler is running the script against an empty input —
the array isn't populated until something upstream (AnimBP) feeds it. Distinguish
authoring-time errors (struct path, pin path, cpp type) from execution-time
errors (input values, index out-of-range, divide-by-zero).

---

## 9. `suspend_notifications` for large batches

```python
rig_bp.suspend_notifications(True)
try:
    # N node-additions, pin sets, links
finally:
    rig_bp.suspend_notifications(False)
```

Each individual edit fires a Modified event that the CR editor UI consumes. For
200+ node graphs, skipping these during the build cuts wall-clock by ~10x.

---

## 10. Idempotent scripts = delete then create

The CR blueprint asset cannot be cleanly "re-authored in place" — existing
nodes aren't replaced, they stack. For regen:

```python
if unreal.EditorAssetLibrary.does_asset_exist(path):
    unreal.EditorAssetLibrary.delete_asset(path)
# ... then create fresh
```

Matching MCP tool: `cr_delete_blueprint` followed by `cr_create_blueprint`.

---

## 11. `p4` auto-add on save

If your project has Perforce, the editor's save path auto-runs
`p4 add <uasset>` on first save. For scripted authoring this is usually fine,
but note that subsequent edits require `p4 edit` (which the Python flow does
not call — save_asset will silently fail to overwrite a read-only file).

Workaround in scripts: guard save with `set_file_attributes` to clear read-only
if the asset was checked in, or rely on `cr_delete_blueprint` to wipe the asset
entirely before rebuilding.

---

## Appendix — Minimal working "hello world"

The smallest self-contained CR graph authored via Python:

```python
import unreal
bp = unreal.load_asset('/Game/MyRig.MyRig')
ctrl = bp.get_controller_by_name('RigVMModel')
begin = ctrl.add_unit_node_from_struct_path(
    '/Script/ControlRig.RigUnit_BeginExecution', 'Execute',
    unreal.Vector2D(0, 0), 'BeginExecution')
# Empty graph, but compiles cleanly.
unreal.ControlRigBlueprintLibrary.recompile_vm(bp)
unreal.EditorAssetLibrary.save_asset('/Game/MyRig')
```

From there, chain `add_unit_node_from_struct_path`, `add_array_node`, and
`add_variable_node` calls to build out your Forward Solve.

---

## Changelog

- 2026-04-21: Captured from finger-curl Control Rig authoring attempt on UE 5.6.
  Validated patterns against DazToUnreal's CreateAutoJCMControlRig.py.
  `cr_author.py` MCP tool module landed in v0.13.0 reflecting these lessons.

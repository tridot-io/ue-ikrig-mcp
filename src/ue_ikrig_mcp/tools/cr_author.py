"""Control Rig authoring — programmatic graph construction via UE's Python API.

Exposes low-level wrappers around `ControlRigBlueprint` / `RigVMController` so
LLMs (and humans via the MCP surface) can build and mutate Control Rig graphs
without writing Python-in-Unreal boilerplate each time.

These tools are intentionally small and composable — one call per API verb.
Higher-level recipes (e.g., "make a 10-chain finger curl rig") should be built
on top by chaining these calls, not baked into a monolithic tool.

Observed from shipping examples (DazToUnreal, UE 5.6 built-ins):
  * `blueprint.get_controller_by_name('RigVMModel')` is the canonical way to get
    the graph controller. Fall back to `get_controller()` if named lookup fails.
  * Struct paths drift between engine versions. The `struct_paths` arg on
    `add_unit_node` accepts a list so callers can provide a fallback chain
    (first that succeeds wins).
  * Pin default values for nested USTRUCTs use string literal form like
    '(Type=Bone,Name="hand_l")' — same as the UE text property serializer.
  * Execution links use `.ExecuteContext` as the pin name on both sides.
  * After batch edits call `ControlRigBlueprintLibrary.recompile_vm(bp)` then
    `EditorAssetLibrary.save_asset(path)`.
"""

import json

from mcp.types import TextContent

from ..ue_connection import get_connection, UENotRunningError
from ..ue_scripts import wrap_script, escape_string, safe_execute, validate_cpp_type


def _ok(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": True, "message": msg}, indent=2))]


def _load_rig_preamble(need_controller: bool = True) -> str:
    """Editor-side preamble (H6): load `rig_bp` from the in-editor `rig_path`,
    assert it is a loadable ControlRigBlueprint, and optionally acquire a non-null
    RigVMController as `ctrl`. Raises a clear error (surfaced by unwrap) instead of
    the cryptic `'NoneType' object has no attribute 'get_controller_by_name'` an
    agent hits today when the asset is missing or the wrong type. Expects the
    generated script to have already defined the `rig_path` variable."""
    lines = (
        "rig_bp = unreal.load_asset(rig_path)\n"
        "if rig_bp is None:\n"
        "    raise ValueError('ControlRigBlueprint not loadable: %s' % rig_path)\n"
        "if type(rig_bp).__name__ != 'ControlRigBlueprint':\n"
        "    raise ValueError('Asset is not a ControlRigBlueprint (got %s): %s' % (type(rig_bp).__name__, rig_path))\n"
    )
    if need_controller:
        lines += (
            "ctrl = rig_bp.get_controller_by_name('RigVMModel') or rig_bp.get_controller()\n"
            "if ctrl is None:\n"
            "    raise RuntimeError('Could not acquire RigVMController for %s' % rig_path)\n"
        )
    return lines


def register(server):
    # ------------------------------------------------------------------
    # Lifecycle: create / delete the blueprint asset
    # ------------------------------------------------------------------

    @server.tool(
        name="cr_create_blueprint",
        description=(
            "Create a new ControlRigBlueprint asset at rig_path, initialized against "
            "the provided skeleton or skeletal mesh. Overwrites any existing asset at "
            "rig_path. Returns the created asset's path. The rig starts with an empty "
            "Forward Solve graph (no nodes beyond the hidden BeginExecution stub)."
        ),
    )
    async def cr_create_blueprint(
        rig_path: str,
        skeleton_or_mesh_path: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))

        rp = escape_string(rig_path)
        sp = escape_string(skeleton_or_mesh_path)
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'src_path = "{sp}"\n'
            "src = unreal.load_asset(src_path)\n"
            "if src is None:\n"
            "    raise ValueError('Source asset not loadable: %s' % src_path)\n"
            # H7: do NOT delete an existing asset up front — a failed create would
            # then have destroyed the original. Create the new BP first (the factory
            # places it at a derived path), confirm it exists, and only then delete +
            # rename over the old one.
            "existed = unreal.EditorAssetLibrary.does_asset_exist(rig_path)\n"
            "factory = unreal.ControlRigBlueprintFactory()\n"
            "rig_bp = None\n"
            "try:\n"
            "    rig_bp = factory.create_control_rig_from_skeletal_mesh_or_skeleton(selected_object=src)\n"
            "except Exception:\n"
            "    rig_bp = None\n"
            "if rig_bp is None:\n"
            "    target_dir  = rig_path.rsplit('/', 1)[0]\n"
            "    target_name = rig_path.rsplit('/', 1)[1]\n"
            # create to a temp name when occupied, so the original survives until the new asset is confirmed
            "    tmp_name = (target_name + '_MCPNew') if existed else target_name\n"
            "    tools = unreal.AssetToolsHelpers.get_asset_tools()\n"
            "    rig_bp = tools.create_asset(tmp_name, target_dir, unreal.ControlRigBlueprint, factory)\n"
            "if rig_bp is None:\n"
            "    raise RuntimeError('Failed to create ControlRigBlueprint (original at %s left intact)' % rig_path)\n"
            "cur = rig_bp.get_path_name().split('.')[0]\n"
            "if cur != rig_path:\n"
            "    if existed:\n"
            "        unreal.EditorAssetLibrary.delete_asset(rig_path)\n"
            "    unreal.EditorAssetLibrary.rename_asset(cur, rig_path)\n"
            "    rig_bp = unreal.load_asset(rig_path)\n"
            "if rig_bp is None:\n"
            "    raise RuntimeError('Rename to %s did not yield a loadable blueprint' % rig_path)\n"
            "saved = bool(unreal.EditorAssetLibrary.save_asset(rig_path))\n"
            'print("__MCP_RESULT__" + json.dumps({"rig_path": rig_bp.get_path_name(), "saved": saved, "replaced_existing": existed}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_delete_blueprint",
        description="Delete a ControlRigBlueprint asset. Safe if missing.",
    )
    async def cr_delete_blueprint(rig_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            "deleted = False\n"
            "if unreal.EditorAssetLibrary.does_asset_exist(rig_path):\n"
            "    deleted = bool(unreal.EditorAssetLibrary.delete_asset(rig_path))\n"
            'print("__MCP_RESULT__" + json.dumps({"deleted": deleted}))'
        )
        return _ok(safe_execute(conn, script))

    # ------------------------------------------------------------------
    # Variables (BP-level inputs that the owning AnimBP writes per frame)
    # ------------------------------------------------------------------

    @server.tool(
        name="cr_add_member_variable",
        description=(
            "Add a member variable to the ControlRigBlueprint. cpp_type uses UE's "
            "CPP naming: 'float', 'int32', 'FVector', 'FName', 'TArray<float>', "
            "'TMap<FName,float>' (note: TMap is not always consumable from RigVM pins "
            "in UE 5.x — prefer TArray<T>). is_input=True makes it an input pin on the "
            "rig node in the owning AnimBP."
        ),
    )
    async def cr_add_member_variable(
        rig_path: str,
        variable_name: str,
        cpp_type: str,
        is_input: bool = True,
        default_value: str = "",
    ) -> list[TextContent]:
        # D1: an unrecognised cpp_type trips a C++ check inside add_member_variable
        # that hard-crashes the editor (the editor-side try/except below cannot catch
        # a native assert), so reject bad types here before sending anything.
        bad = validate_cpp_type(cpp_type)
        if bad:
            return _err(bad)
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp  = escape_string(rig_path)
        vn  = escape_string(variable_name)
        tp  = escape_string(cpp_type)
        dv  = escape_string(default_value)
        inp = "True" if is_input else "False"
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'name = "{vn}"\n'
            f'tp   = "{tp}"\n'
            f'dv   = "{dv}"\n'
            f'is_in = {inp}\n'
            + _load_rig_preamble(need_controller=False)
            + "err = None\n"
            "ok = False\n"
            "try:\n"
            "    rig_bp.add_member_variable(name, tp, is_in, False, dv)\n"
            "    ok = True\n"
            "except Exception as e:\n"
            "    err = str(e)\n"
            # H5: do NOT save on failure (was saving unconditionally and dropping
            # `err`); raise so unwrap surfaces the reason to the caller.
            "if not ok:\n"
            '    raise RuntimeError("add_member_variable failed: %s" % err)\n'
            "saved = bool(unreal.EditorAssetLibrary.save_asset(rig_path))\n"
            'print("__MCP_RESULT__" + json.dumps({"added": ok, "saved": saved, "name": name, "cpp_type": tp}))'
        )
        return _ok(safe_execute(conn, script))

    # ------------------------------------------------------------------
    # Graph mutation: add nodes, set pin defaults, add links
    # ------------------------------------------------------------------

    @server.tool(
        name="cr_add_unit_node",
        description=(
            "Add a RigUnit (USTRUCT with RIGVM_METHOD) node to the Forward Solve graph. "
            "struct_paths is a LIST of candidate paths — the first that succeeds is used. "
            "Useful for engine-version portability, e.g. ["
            "'/Script/RigVM.RigVMFunction_MathFloatMul', "
            "'/Script/ControlRig.RigUnit_MathFloatMul']. "
            "Returns the actual node name assigned (may be suffixed if collision)."
        ),
    )
    async def cr_add_unit_node(
        rig_path: str,
        struct_paths: list,
        node_name: str,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        method_name: str = "Execute",
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        if not struct_paths:
            return _err("struct_paths must be a non-empty list")
        rp  = escape_string(rig_path)
        nn  = escape_string(node_name)
        mn  = escape_string(method_name)
        paths_json = json.dumps([str(p) for p in struct_paths])
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'node_name = "{nn}"\n'
            f'method_name = "{mn}"\n'
            f'paths = {paths_json}\n'
            f'pos = unreal.Vector2D({float(pos_x)}, {float(pos_y)})\n'
            + _load_rig_preamble()
            + "added = None; used_path = None; last_err = None\n"
            "for p in paths:\n"
            "    try:\n"
            "        n = ctrl.add_unit_node_from_struct_path(p, method_name, pos, node_name)\n"
            "        if n is not None:\n"
            "            added = n; used_path = p; break\n"
            "    except Exception as e:\n"
            "        last_err = str(e)\n"
            "if added is None:\n"
            '    raise RuntimeError(f"add_unit_node failed across {paths}: {last_err}")\n'
            'print("__MCP_RESULT__" + json.dumps({"node": added.get_node_path(), "struct_path": used_path}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_add_array_op_node",
        description=(
            "Add an array-operation node (Get/Set/Add/Length/Append/etc). Uses the "
            "dedicated RigVMOpCode API — NOT add_unit_node, because ARRAY ops don't "
            "have static struct paths (they're dispatch nodes, element-type-parameterized).\n\n"
            "op_code: name of an RigVMOpCode enum value. Common: 'ARRAY_GET_AT_INDEX', "
            "'ARRAY_SET_AT_INDEX', 'ARRAY_ADD', 'ARRAY_GET_NUM', 'ARRAY_ITERATOR'.\n\n"
            "element_cpp_type: the ELEMENT type, not the array. For PRIMITIVES pass it "
            "alone: 'float', 'int32', 'FVector', 'FName', 'FTransform'. For a STRUCT/ENUM "
            "element (e.g. 'FRigElementKey') you must ALSO pass element_cpp_type_object "
            "(below) — a bare unrecognized type string is refused because it can crash "
            "the editor.\n\n"
            "element_cpp_type_object: asset path for struct/enum element types. E.g. for "
            "an FRigElementKey element pass element_cpp_type='FRigElementKey' AND "
            "element_cpp_type_object='/Script/ControlRig.RigElementKey'. Empty for primitives.\n\n"
            "These enums are marked deprecated in some UE versions (5.5+) but still "
            "functional — the modern replacement is DISPATCH_RigVMDispatch_Array* which "
            "also works via this call."
        ),
    )
    async def cr_add_array_op_node(
        rig_path: str,
        op_code: str,
        element_cpp_type: str,
        node_name: str,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        element_cpp_type_object: str = "",
    ) -> list[TextContent]:
        # D1: a junk element type can hard-crash the editor (native check). When an
        # object path is supplied it is the authority for struct/enum elements, so we
        # only require it to look like a real object path; otherwise the element type
        # must be a known scalar/container.
        if element_cpp_type_object.strip():
            if not element_cpp_type_object.strip().startswith(("/Script/", "/Game/")):
                return _err(
                    "element_cpp_type_object must be a '/Script/...' or '/Game/...' "
                    "path, got %r" % element_cpp_type_object
                )
        else:
            bad = validate_cpp_type(element_cpp_type)
            if bad:
                return _err(bad)
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        oc = escape_string(op_code)
        ct = escape_string(element_cpp_type)
        co = escape_string(element_cpp_type_object)
        nn = escape_string(node_name)
        script = wrap_script(
            "import unreal\n"
            f'rig_path  = "{rp}"\n'
            f'op_name   = "{oc}"\n'
            f'cpp_type  = "{ct}"\n'
            f'type_obj  = "{co}"\n'
            f'node_name = "{nn}"\n'
            f'pos = unreal.Vector2D({float(pos_x)}, {float(pos_y)})\n'
            + _load_rig_preamble()
            + "try:\n"
            "    op = getattr(unreal.RigVMOpCode, op_name)\n"
            "except AttributeError:\n"
            '    raise ValueError(f"Unknown RigVMOpCode: {op_name}. Try dir(unreal.RigVMOpCode) in console.")\n'
            "# Primary: from_object_path variant if element type is a UObject path.\n"
            "n = None\n"
            "if type_obj:\n"
            "    try:\n"
            "        n = ctrl.add_array_node_from_object_path(op, cpp_type, type_obj, pos, node_name)\n"
            "    except Exception: pass\n"
            "if n is None:\n"
            "    n = ctrl.add_array_node(op, cpp_type, None, pos, node_name)\n"
            "if n is None:\n"
            '    raise RuntimeError("add_array_node returned None")\n'
            'print("__MCP_RESULT__" + json.dumps({"node": n.get_node_path(), "op": op_name, "element_type": cpp_type}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_add_template_node",
        description=(
            "Add a template (variadic) node — signatures look like "
            "'Make Relative::Execute(in Global,in Parent,out Local)'. Used when the "
            "unit supports multiple pin-type combinations; the signature disambiguates."
        ),
    )
    async def cr_add_template_node(
        rig_path: str,
        template_notation: str,
        node_name: str,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        tn = escape_string(template_notation)
        nn = escape_string(node_name)
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'notation = "{tn}"\n'
            f'node_name = "{nn}"\n'
            f'pos = unreal.Vector2D({float(pos_x)}, {float(pos_y)})\n'
            + _load_rig_preamble()
            + "n = ctrl.add_template_node(notation, pos, node_name)\n"
            "if n is None:\n"
            '    raise RuntimeError("add_template_node returned None")\n'
            'print("__MCP_RESULT__" + json.dumps({"node": n.get_node_path(), "template": notation}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_add_variable_node",
        description=(
            "Add a variable-get or variable-set node to the graph (not the BP member "
            "variable — that's cr_add_member_variable). is_getter=True for Get, "
            "False for Set."
        ),
    )
    async def cr_add_variable_node(
        rig_path: str,
        variable_name: str,
        cpp_type: str,
        is_getter: bool = True,
        pos_x: float = 0.0,
        pos_y: float = 0.0,
        node_name: str = "",
    ) -> list[TextContent]:
        # D1: gate the type string before it reaches add_variable_node (native crash
        # risk on an unrecognised type).
        bad = validate_cpp_type(cpp_type)
        if bad:
            return _err(bad)
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        vn = escape_string(variable_name)
        tp = escape_string(cpp_type)
        nn = escape_string(node_name)
        is_g = "True" if is_getter else "False"
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'var_name = "{vn}"\n'
            f'cpp_type = "{tp}"\n'
            f'node_name = "{nn}"\n'
            f'is_g = {is_g}\n'
            f'pos = unreal.Vector2D({float(pos_x)}, {float(pos_y)})\n'
            + _load_rig_preamble()
            + "n = ctrl.add_variable_node(var_name, cpp_type, None, is_g, '', pos, node_name)\n"
            "if n is None:\n"
            '    raise RuntimeError("add_variable_node returned None")\n'
            'print("__MCP_RESULT__" + json.dumps({"node": n.get_node_path()}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_set_pin_default",
        description=(
            "Set a pin's default value. pin_path is '<node_name>.<PinName>' (dot-"
            "separated for nested subpins, e.g. 'Off_hand_l.OffsetTransform.Rotation'). "
            "Struct pins accept UE text form, e.g. '(Type=Bone,Name=\"hand_l\")' for a "
            "FRigElementKey, '(X=0.0,Y=0.0,Z=1.0)' for FVector."
        ),
    )
    async def cr_set_pin_default(
        rig_path: str,
        pin_path: str,
        value: str,
        resize_arrays: bool = False,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        pp = escape_string(pin_path)
        vv = escape_string(value)
        ra = "True" if resize_arrays else "False"
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'pin_path = "{pp}"\n'
            f'value = "{vv}"\n'
            f'ra = {ra}\n'
            + _load_rig_preamble()
            + "ok = ctrl.set_pin_default_value(pin_path, value, ra)\n"
            'print("__MCP_RESULT__" + json.dumps({"set": bool(ok), "pin": pin_path}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_add_link",
        description=(
            "Connect two pins. from_pin is the output pin (e.g. 'Mul_hand_l.Result'), "
            "to_pin is the input (e.g. 'Quat_hand_l.Angle'). For execution chains use "
            "'NodeA.ExecuteContext' -> 'NodeB.ExecuteContext'."
        ),
    )
    async def cr_add_link(
        rig_path: str,
        from_pin: str,
        to_pin: str,
    ) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        fp = escape_string(from_pin)
        tp = escape_string(to_pin)
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            f'fp = "{fp}"\n'
            f'tp = "{tp}"\n'
            + _load_rig_preamble()
            + "ok = ctrl.add_link(fp, tp)\n"
            'print("__MCP_RESULT__" + json.dumps({"linked": bool(ok), "from": fp, "to": tp}))'
        )
        return _ok(safe_execute(conn, script))

    # ------------------------------------------------------------------
    # Introspection / finalize
    # ------------------------------------------------------------------

    @server.tool(
        name="cr_dump_graph",
        description=(
            "List all nodes and links in the Forward Solve graph. Useful for verifying "
            "a scripted build, diffing between versions, or checking what a rig contains "
            "before modifying it."
        ),
    )
    async def cr_dump_graph(rig_path: str) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        script = wrap_script(
            "import unreal\n"
            f'rig_path = "{rp}"\n'
            + _load_rig_preamble()
            + "graph = ctrl.get_graph() if hasattr(ctrl, 'get_graph') else rig_bp.get_rig_vm_graph()\n"
            "nodes_out = []\n"
            "links_out = []\n"
            "try:\n"
            "    for n in graph.get_nodes():\n"
            "        entry = {'name': n.get_node_path()}\n"
            "        try: entry['title'] = n.get_node_title()\n"
            "        except Exception: pass\n"
            "        try: entry['kind'] = type(n).__name__\n"
            "        except Exception: pass\n"
            "        nodes_out.append(entry)\n"
            "except Exception as e:\n"
            "    nodes_out.append({'err': str(e)})\n"
            "try:\n"
            "    for l in graph.get_links():\n"
            "        try:\n"
            "            links_out.append({'from': l.get_source_pin().get_pin_path(),\n"
            "                              'to':   l.get_target_pin().get_pin_path()})\n"
            "        except Exception as e:\n"
            "            links_out.append({'err': str(e)})\n"
            "except Exception as e:\n"
            "    links_out.append({'err': str(e)})\n"
            'print("__MCP_RESULT__" + json.dumps({"node_count": len(nodes_out), "nodes": nodes_out[:200], "link_count": len(links_out), "links": links_out[:400]}))'
        )
        return _ok(safe_execute(conn, script))

    @server.tool(
        name="cr_compile_and_save",
        description=(
            "Recompile the RigVM and save the asset. Call this after a batch of graph "
            "edits so changes are persisted and any compile errors surface. Tails the "
            "active UE log during the compile window and returns structured warnings "
            "and errors (category + message) plus the blueprint's post-compile status. "
            "Python stdout alone misses UE_LOG-level diagnostics; the log tail is what "
            "makes compile failures visible. timeout_seconds>0 overrides the default "
            "command timeout for large rigs (a compile holds the single editor command "
            "slot for its whole duration, so keep it as low as the rig allows)."
        ),
    )
    async def cr_compile_and_save(rig_path: str, timeout_seconds: float = 0.0) -> list[TextContent]:
        try:
            conn = get_connection()
        except UENotRunningError as e:
            return _err(str(e))
        rp = escape_string(rig_path)
        script = wrap_script(
            "import unreal, os, glob\n"
            f'rig_path = "{rp}"\n'
            "rig_bp = unreal.load_asset(rig_path)\n"
            "if rig_bp is None:\n"
            f'    raise ValueError("ControlRigBlueprint not found: {rp}")\n'
            # Locate the active UE log file (most recently modified .log in the project log dir).
            "log_dir = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_log_dir())\n"
            "active_log = None\n"
            "pre_size = 0\n"
            "try:\n"
            "    candidates = [(p, os.path.getmtime(p)) for p in glob.glob(os.path.join(log_dir, '*.log'))]\n"
            "    candidates.sort(key=lambda t: t[1], reverse=True)\n"
            "    if candidates:\n"
            "        active_log = candidates[0][0]\n"
            "        pre_size = os.path.getsize(active_log)\n"
            "except Exception:\n"
            "    pass\n"
            # Compile — catch Python-level errors separately from C++ UE_LOG diagnostics.
            "compile_err = None\n"
            "try:\n"
            "    unreal.ControlRigBlueprintLibrary.recompile_vm(rig_bp)\n"
            "except Exception as _e:\n"
            "    compile_err = str(_e)\n"
            # H4/D6: recompile_vm updates only the RigVM, leaving the Blueprint
            # `status` enum stale (it read null before). Run the BP compile too so
            # `status`/`status_message` reflect this compile.
            "try:\n"
            "    if hasattr(unreal, 'BlueprintEditorLibrary'):\n"
            "        unreal.BlueprintEditorLibrary.compile_blueprint(rig_bp)\n"
            "except Exception as _e:\n"
            "    if compile_err is None:\n"
            "        compile_err = str(_e)\n"
            # Tail the log for compile-category diagnostics written during the window.
            "warnings_out = []\n"
            "errors_out = []\n"
            # log_read_ok gates the 'OK' verdict: if we could NOT read the log tail,
            # an absence of errors does NOT prove a clean compile (errors are UE_LOG
            # lines, not Python exceptions), so we must report UNKNOWN, not OK.
            "log_read_ok = False\n"
            "_CATS = ('LogControlRig', 'LogRigVM', 'LogBlueprint', 'LogClass', 'LogAsset', 'LogPython', 'LogKismet')\n"
            "if active_log:\n"
            "    try:\n"
            "        with open(active_log, 'r', encoding='utf-8', errors='ignore') as _fh:\n"
            "            _fh.seek(pre_size)\n"
            "            _tail = _fh.read()\n"
            "        for _line in _tail.splitlines():\n"
            "            _line = _line.strip()\n"
            "            if not _line:\n"
            "                continue\n"
            "            if not any(_c in _line for _c in _CATS):\n"
            "                continue\n"
            "            if ': Error:' in _line:\n"
            "                errors_out.append(_line)\n"
            "            elif ': Warning:' in _line:\n"
            "                warnings_out.append(_line)\n"
            "        log_read_ok = True\n"
            "    except Exception:\n"
            "        log_read_ok = False\n"
            # Post-compile blueprint status — enum name + human-readable summary.
            # H4/D6: ControlRigBlueprint exposes no readable 'status' property in UE
            # 5.x (get_editor_property('status') raises), so it always read null.
            # Derive a meaningful status from the signals that DO work — the compile
            # exception and the log-tail diagnostics — instead of a perpetual null.
            "if compile_err is not None or errors_out:\n"
            "    status_name = 'ERROR'\n"
            "elif warnings_out:\n"
            "    status_name = 'WARNING'\n"
            "elif log_read_ok:\n"
            "    status_name = 'OK'\n"
            "else:\n"
            # No Python error and no parsed log lines, but we could not read the log
            # tail — a UE_LOG compile error would be invisible here, so we cannot
            # claim OK. Report UNKNOWN and treat success as unconfirmed.
            "    status_name = 'UNKNOWN'\n"
            "if compile_err:\n"
            "    status_message = compile_err\n"
            "elif not log_read_ok:\n"
            "    status_message = 'compile log tail unavailable - clean status NOT confirmed (%d error(s), %d warning(s) seen)' % (len(errors_out), len(warnings_out))\n"
            "else:\n"
            "    status_message = '%d error(s), %d warning(s)' % (len(errors_out), len(warnings_out))\n"
            "saved = bool(unreal.EditorAssetLibrary.save_asset(rig_path))\n"
            "_success = compile_err is None and not errors_out and log_read_ok\n"
            'print("__MCP_RESULT__" + json.dumps({'
            '"saved": saved, "compile_error": compile_err, '
            '"status": status_name, "status_message": status_message, '
            '"errors": errors_out, "warnings": warnings_out, '
            '"error_count": len(errors_out), "warning_count": len(warnings_out), '
            '"success": _success, "log_tail_available": log_read_ok, "log_file": active_log'
            '}))'
        )
        _to = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        return _ok(safe_execute(conn, script, timeout=_to))

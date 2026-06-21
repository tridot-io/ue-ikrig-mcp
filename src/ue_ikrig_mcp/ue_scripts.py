"""Code generation helpers producing UE Python script strings."""

import json


def escape_string(s: str) -> str:
    """Escape a string for safe embedding inside a Python string literal.

    Uses ``json.dumps`` so quotes, backslashes AND control characters (newlines,
    tabs, etc.) are all escaped — the old hand-rolled version only handled quotes
    and backslashes, so a value containing a newline could terminate the generated
    literal early (breaking the script) or inject statements that still passed the
    syntax pre-flight. The surrounding quotes ``json.dumps`` adds are stripped so
    the result drops into an existing ``"..."`` (or ``'...'``) literal at the call
    sites; single quotes are additionally escaped so both quoting conventions used
    across the codebase stay safe.
    """
    return json.dumps(s)[1:-1].replace("'", "\\'")


def unwrap(result):
    """Collapse a ``conn.execute`` transport dict into payload-or-error.

    ``execute`` returns ``{success, result, output, parsed, hints}``. Because the
    editor-side ``wrap_script`` catches exceptions and *prints* an error sentinel,
    transport ``success`` can be True while the script actually failed (its
    ``parsed`` carries ``error: true``). Callers that did ``_ok(conn.execute(...))``
    therefore reported editor-side failures as success-shaped blobs. Route results
    through this so a failure always surfaces as ``{"error": true, ...}`` and a
    success returns just the structured payload.
    """
    if not isinstance(result, dict):
        return {"error": True, "message": "unexpected result type: %s" % type(result).__name__}
    if not result.get("success"):
        return {
            "error": True,
            "message": result.get("result") or "editor execution failed",
            "hints": result.get("hints"),
        }
    parsed = result.get("parsed")
    if parsed is None:
        return {
            "error": True,
            "message": "script returned no __MCP_RESULT__ sentinel",
            "raw": (result.get("output") or "")[:500],
            "hints": result.get("hints"),
        }
    if isinstance(parsed, dict) and parsed.get("error"):
        return parsed
    return parsed


def safe_execute(conn, code, mode="ExecuteFile", timeout=None):
    """Execute editor code and return an unwrapped payload-or-error envelope.

    Combines two guarantees so the tool layer never has to do either by hand:

    * **D2 (transport)** — a dropped/dying editor makes ``conn.execute`` raise
      ``UEConnectionError`` / ``UENotRunningError``. Without this, that exception
      escapes the tool function and the MCP call dies with an opaque stack trace.
      Here it becomes ``{"error": true, "transport": true, ...}`` so the agent gets
      an actionable message and the server stays up.
    * **H1/G2 (editor-side)** — the returned transport dict is routed through
      :func:`unwrap`, so an editor-side failure surfaces as ``{"error": true, ...}``
      instead of a success-shaped blob.

    Tools should prefer ``return _ok(safe_execute(conn, script))`` over
    ``_ok(unwrap(conn.execute(script)))``.
    """
    # Lazy import to avoid a ue_scripts <-> ue_connection import cycle.
    from .ue_connection import UEConnectionError, UENotRunningError

    try:
        result = conn.execute(code, mode=mode, timeout=timeout)
    except (UEConnectionError, UENotRunningError) as exc:
        return {
            "error": True,
            "transport": True,
            "message": "%s: %s" % (type(exc).__name__, exc),
        }
    return unwrap(result)


# Member-variable / pin C++ types that ``add_member_variable`` accepts directly.
# A type string the editor does not recognise can hard-crash UE (a C++ check /
# assert that bypasses Python try/except), so we gate on a conservative allow-list
# before ever sending it to the editor — see :func:`validate_cpp_type`.
_CR_SCALAR_TYPES = frozenset({
    "bool", "int32", "int64", "uint8", "uint32", "float", "double",
    "FString", "FName", "FText",
    "FVector", "FVector2D", "FVector4", "FVector2f", "FVector3f", "FVector4f",
    "FRotator", "FQuat", "FTransform", "FMatrix", "FPlane",
    "FLinearColor", "FColor", "FIntPoint", "FIntVector",
})


def validate_cpp_type(cpp_type):
    """Return ``None`` if ``cpp_type`` is safe to pass to ``add_member_variable``.

    Otherwise return a human-readable rejection message. Bad type strings do not
    raise a catchable Python exception inside the editor — they trip a C++ check
    and **crash the running editor** (observed live with ``"NotARealType"``). So
    every mutation that forwards a user-supplied type string must pre-validate it
    here and refuse anything that is not either a known scalar, a container of the
    form ``TArray<...>`` / ``TMap<...>`` / ``TSet<...>``, or a fully-qualified
    object/struct path (``/Script/...`` or ``/Game/...``).
    """
    t = (cpp_type or "").strip()
    if not t:
        return "cpp_type is empty"
    if t in _CR_SCALAR_TYPES:
        return None
    if t.startswith(("TArray<", "TMap<", "TSet<")) and t.endswith(">"):
        return None
    if t.startswith(("/Script/", "/Game/")):
        return None
    return (
        "unrecognized cpp_type %r — an unknown type can crash the editor, so it is "
        "refused. Use a scalar (e.g. 'float', 'int32', 'bool', 'FVector', 'FString', "
        "'FTransform'), a container ('TArray<float>'), or a fully-qualified "
        "'/Script/...' or '/Game/...' object/struct path." % cpp_type
    )


def wrap_script(code: str) -> str:
    """Wrap code in a try/except that emits __MCP_RESULT__ JSON on error."""
    return (
        "import json\n"
        "try:\n"
        + "\n".join(f"    {line}" for line in code.splitlines())
        + "\n"
        "except Exception as __e:\n"
        "    import traceback\n"
        '    print("__MCP_RESULT__" + json.dumps({"error": True, "message": str(__e), "traceback": traceback.format_exc()}))\n'
    )


def build_load_asset(asset_path: str) -> str:
    """Build a script that loads an asset by path and emits it as __MCP_RESULT__."""
    p = escape_string(asset_path)
    return wrap_script(
        f'asset = unreal.load_asset("{p}")\n'
        "if asset is None:\n"
        f'    raise ValueError("Asset not found: {p}")\n'
        'print("__MCP_RESULT__" + json.dumps({"path": asset.get_path_name(), "class": asset.get_class().get_name()}))'
    )


def build_create_ik_rig(package_path: str, asset_name: str) -> str:
    """Build a script that creates an IKRigDefinition asset."""
    pp = escape_string(package_path)
    an = escape_string(asset_name)
    return wrap_script(
        "import unreal\n"
        "factory = unreal.IKRigDefinitionFactory()\n"
        "asset_tools = unreal.AssetToolsHelpers.get_asset_tools()\n"
        f'asset = asset_tools.create_asset("{an}", "{pp}", None, factory)\n'
        "if asset is None:\n"
        f'    raise ValueError("Failed to create IKRig asset at {pp}/{an}")\n'
        'print("__MCP_RESULT__" + json.dumps({"path": asset.get_path_name(), "class": asset.get_class().get_name()}))'
    )


def build_create_retargeter(package_path: str, asset_name: str) -> str:
    """Build a script that creates an IKRetargeter asset."""
    pp = escape_string(package_path)
    an = escape_string(asset_name)
    return wrap_script(
        "import unreal\n"
        "factory = unreal.IKRetargetFactory()\n"
        "asset_tools = unreal.AssetToolsHelpers.get_asset_tools()\n"
        f'asset = asset_tools.create_asset("{an}", "{pp}", None, factory)\n'
        "if asset is None:\n"
        f'    raise ValueError("Failed to create IKRetargeter asset at {pp}/{an}")\n'
        'print("__MCP_RESULT__" + json.dumps({"path": asset.get_path_name(), "class": asset.get_class().get_name()}))'
    )


def build_get_ik_rig_controller(asset_path: str) -> str:
    """Build a script that loads an IKRig asset and retrieves its controller."""
    p = escape_string(asset_path)
    return wrap_script(
        "import unreal\n"
        f'ik_rig = unreal.load_asset("{p}")\n'
        "if ik_rig is None:\n"
        f'    raise ValueError("IKRig asset not found: {p}")\n'
        "controller = unreal.IKRigController.get_controller(ik_rig)\n"
        "if controller is None:\n"
        f'    raise ValueError("Could not get controller for: {p}")\n'
        'print("__MCP_RESULT__" + json.dumps({"path": ik_rig.get_path_name()}))'
    )


def build_get_retargeter_controller(asset_path: str) -> str:
    """Build a script that loads an IKRetargeter asset and retrieves its controller."""
    p = escape_string(asset_path)
    return wrap_script(
        "import unreal\n"
        f'retargeter = unreal.load_asset("{p}")\n'
        "if retargeter is None:\n"
        f'    raise ValueError("IKRetargeter asset not found: {p}")\n'
        "controller = unreal.IKRetargeterController.get_controller(retargeter)\n"
        "if controller is None:\n"
        f'    raise ValueError("Could not get controller for: {p}")\n'
        'print("__MCP_RESULT__" + json.dumps({"path": retargeter.get_path_name()}))'
    )


def build_asset_registry_query(class_path: str, path_filter: str = "") -> str:
    """Build a script that queries the asset registry for assets of a given class.

    class_path is a fully-qualified UE class path such as
    "/Script/Engine.SkeletalMesh" or "/Script/IKRig.IKRigDefinition".
    ARFilter.class_names / recursive_classes were deprecated in UE 5.5+;
    use class_paths with TopLevelAssetPath instead.
    """
    cp = escape_string(class_path)
    pf = escape_string(path_filter)
    return wrap_script(
        "import unreal\n"
        "ar = unreal.AssetRegistryHelpers.get_asset_registry()\n"
        f'_module, _klass = "{cp}".rsplit(".", 1)\n'
        # UE 5.5+: ARFilter list properties (class_paths, package_paths) cannot be
        # set by attribute assignment ("cannot be edited on instances") — they must
        # be passed to the constructor.
        + (
            f'ar_filter = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath(_module, _klass)], recursive_paths=True, package_paths=["{pf}"])\n'
            if path_filter else
            "ar_filter = unreal.ARFilter(class_paths=[unreal.TopLevelAssetPath(_module, _klass)], recursive_paths=True)\n"
        )
        + "assets = ar.get_assets(ar_filter)\n"
        'result = [{"path": str(a.package_name), "name": str(a.asset_name)} for a in assets]\n'
        'print("__MCP_RESULT__" + json.dumps(result))'
    )


def build_save_asset(asset_path: str) -> str:
    """Build a script that saves an asset."""
    p = escape_string(asset_path)
    return wrap_script(
        "import unreal\n"
        f'asset = unreal.load_asset("{p}")\n'
        "if asset is None:\n"
        f'    raise ValueError("Asset not found: {p}")\n'
        "unreal.EditorAssetLibrary.save_asset(asset.get_path_name())\n"
        'print("__MCP_RESULT__" + json.dumps({"saved": True, "path": asset.get_path_name()}))'
    )

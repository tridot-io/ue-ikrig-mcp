"""Code generation helpers producing UE Python script strings."""


def escape_string(s: str) -> str:
    """Escape quotes and backslashes for safe Python string embedding."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")


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
        "controller = unreal.IKRetargetController.get_controller(retargeter)\n"
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
        "ar_filter = unreal.ARFilter()\n"
        f'_module, _klass = "{cp}".rsplit(".", 1)\n'
        "ar_filter.class_paths = [unreal.TopLevelAssetPath(_module, _klass)]\n"
        "ar_filter.recursive_paths = True\n"
        + (f'ar_filter.package_paths = ["{pf}"]\n' if path_filter else "")
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

"""Blueprint-family capability router.

This tool is deliberately read-only. It gives future agents a single first stop
for graph/Blueprint-like requests so they classify the asset family and action
before reaching for Control Rig, TAPython, generic K2, or execute_python
semantics.
"""

import copy
import json
from typing import Any

from mcp.types import TextContent

_SUPPORT_LABELS = [
    "supported",
    "partial",
    "experimental",
    "engine_supported_repo_not_exposed",
    "unsupported",
    "unknown",
]

_ACTION_ALIASES = {
    "describe": "inspect",
    "list": "inspect",
    "list_graphs": "inspect",
    "inspect_existing_nodes": "inspect",
    "connect": "wire_pin",
    "connect_pin": "wire_pin",
    "connect_expression": "wire_pin",
    "link": "wire_pin",
    "set_default": "set_defaults",
    "set_property": "set_defaults",
    "set_properties": "set_defaults",
    "recompile": "compile",
    "update": "compile",
    "capture": "capture_viewport",
    "capture_active_viewport": "capture_viewport",
    "graph_create": "create_node",
    "dump": "dump_graph",
    "apply": "apply_graph",
}

_FAMILY_ALIASES = {
    "widget_blueprint": "widget",
    "widgetblueprint": "widget",
    "anim_blueprint": "animbp",
    "animblueprint": "animbp",
    "animation_blueprint": "animbp",
    "controlrig": "control_rig",
    "control_rig_blueprint": "control_rig",
    "materialfunction": "material_function",
    "material_function": "material_function",
    "materialinstance": "material_instance",
    "material_instance": "material_instance",
    "blueprint_k2": "blueprint",
    "k2": "blueprint",
}


def _cell(
    support: str,
    recommended_tool: str | None,
    next_action: str,
    why: str,
    *,
    requires_editor: bool = False,
    verification: list[str] | None = None,
    caveats: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "support": support,
        "recommended_tool": recommended_tool,
        "next_action": next_action,
        "why": why,
        "requires_editor": requires_editor,
        "verification": verification or [],
        "caveats": caveats or [],
    }


# Keep every asset-family row structurally complete. Tests assert these actions
# exist so the matrix cannot silently become Blueprint/Control-Rig-only.
_FAMILIES: dict[str, dict[str, Any]] = {
    "blueprint": {
        "capability_surface": "asset",
        "display_name": "Blueprint / K2",
        "canonical_surface": "BlueprintEditorLibrary + Blueprint asset/graph APIs",
        "actions": {
            "inspect": _cell(
                "partial",
                "search_unreal_api|describe_unreal_api|future blueprint_describe_asset",
                "Use search_unreal_api/describe_unreal_api first; add blueprint_describe_asset(asset_path, include_graphs=True) before deeper Blueprint tooling.",
                "Project Blueprints are catalogued and describe can report parent/generated class/variables, but BP-defined functions and K2 topology are not fully reflected.",
                verification=["catalog/static result unless live fields are requested"],
                caveats=[
                    "BP-defined functions are not reflected by UE Python catalog describes."
                ],
            ),
            "create_asset": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Stop or implement a Blueprint-specific create tool with fixture validation.",
                "BlueprintEditorLibrary exposes create helpers, but this repo has no safe generic Blueprint create adapter yet.",
            ),
            "create_node": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Do not borrow Control Rig or TAPython semantics; add a K2-specific adapter only after disposable fixture proof.",
                "K2 node creation is not exposed as a safe repo MCP tool by asset path.",
                caveats=["Control Rig RigVM node APIs are not K2 node APIs."],
            ),
            "wire_pin": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Stop until a Blueprint/K2-specific wiring surface is selected, implemented, and fixture-tested.",
                "The repo has no canonical K2 pin wiring tool; generic graph wiring would be a false abstraction.",
            ),
            "set_defaults": _cell(
                "partial",
                "describe_unreal_api|future blueprint_describe_asset",
                "Limit to verified asset-level properties/variables; do not infer graph-node defaults.",
                "Blueprint asset metadata and member variables are partially inspectable, but graph node defaults are not a generic repo-supported surface.",
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; compile changes editor/content state and must not be part of the first read-only pass.",
                "Blueprint compile is side-effecting and needs result reporting, dirty-package checks, and fixture validation before being marked supported.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "compile result",
                    "dirty-package check",
                    "re-query",
                    "save policy",
                ],
            ),
            "save": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; never save real project assets from the router.",
                "Saving is mutating content state and is intentionally outside the read-only router.",
                requires_editor=True,
                verification=["disposable fixture", "dirty-package check", "re-query"],
            ),
        },
    },
    "widget": {
        "capability_surface": "asset",
        "display_name": "WidgetBlueprint",
        "canonical_surface": "WidgetBlueprint asset + WidgetTree/designer tree + Blueprint graph semantics",
        "actions": {
            "inspect": _cell(
                "partial",
                "search_unreal_api|describe_unreal_api|future widget_describe_tree",
                "Use catalog/describe now; add read-only widget_describe_tree for root/named widgets/tree structure.",
                "WidgetBlueprint assets are catalogued, and WidgetTree APIs exist, but this repo has no dedicated tree describe tool yet.",
                caveats=["Designer tree requests are not generic K2 graph requests."],
            ),
            "create_asset": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer until WidgetBlueprint-specific create fixture exists.",
                "No repo-exposed WidgetBlueprint creation adapter is available.",
            ),
            "create_node": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Classify whether this means designer-tree child, graph node, binding, or MVVM edit; do not route generically.",
                "WidgetBlueprint authoring spans designer tree and Blueprint graph semantics.",
            ),
            "wire_pin": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Stop until a WidgetBlueprint graph-specific wiring adapter exists.",
                "No repo canonical widget graph wiring path exists.",
            ),
            "set_defaults": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Read-only tree/property describe first; mutate only after fixture validation.",
                "Widget defaults/properties can affect designer state and need a dedicated adapter.",
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after compile.",
                "WidgetBlueprint compile is side-effecting and not part of read-only routing.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "compile result",
                    "re-query",
                ],
            ),
            "save": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after save.",
                "Saving WidgetBlueprints mutates content state.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "save result",
                    "re-query",
                ],
            ),
        },
    },
    "animbp": {
        "capability_surface": "asset",
        "display_name": "AnimBlueprint",
        "canonical_surface": "AnimBlueprint/AnimationGraph inspection APIs; targeted existing-node tools",
        "actions": {
            "inspect": _cell(
                "partial",
                "list_animbp_twobone_ik_nodes|search_unreal_api|describe_unreal_api",
                "Use existing-node inspection for supported node classes; use catalog for API discovery.",
                "The repo supports targeted existing TwoBoneIK inspection/update, not arbitrary graph topology authoring.",
                requires_editor=True,
                verification=["tool response", "no mutation for list calls"],
            ),
            "create_asset": _cell(
                "unknown",
                None,
                "Out of current scope unless a dedicated AnimBlueprint fixture plan is added.",
                "AnimBlueprint asset creation is not a current repo focus.",
            ),
            "create_node": _cell(
                "unsupported",
                None,
                "Do not call TAPython graph create for arbitrary assets; report unsupported headless graph authoring.",
                "Stock UE exposes no Python API to create AnimGraph nodes; TAPython graph APIs are UI-context-bound in this MCP.",
            ),
            "wire_pin": _cell(
                "unsupported",
                None,
                "Stop and report unsupported until a dedicated non-focused-editor wiring surface exists.",
                "No reliable repo-exposed AnimGraph pin wiring path exists.",
            ),
            "set_defaults": _cell(
                "partial",
                "set_twobone_ik_node_bones",
                "Only update known fields on existing supported nodes after locating them.",
                "The existing tool updates selected TwoBoneIK bone/space fields only.",
                requires_editor=True,
                verification=["before/after response", "save result"],
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated; current existing-node update notes recompile may be needed, with dirty-package verification.",
                "Compile behavior is not generalized for AnimBlueprint graph edits in this repo.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "compile result",
                    "re-query",
                ],
            ),
            "save": _cell(
                "partial",
                "set_twobone_ik_node_bones",
                "Save occurs inside targeted existing-node update; do not use as generic save support, and verify dirty packages on fixtures.",
                "The supported update path saves the AnimBlueprint but is not a generic save tool.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "before/after response",
                    "save result",
                ],
            ),
        },
    },
    "control_rig": {
        "capability_surface": "asset",
        "display_name": "Control Rig",
        "canonical_surface": "RigVMController + ControlRigBlueprintLibrary.recompile_vm",
        "actions": {
            "inspect": _cell(
                "supported",
                "cr_dump_graph",
                "Use cr_dump_graph for RigVM graph inspection.",
                "Control Rig has dedicated headless RigVM graph tools.",
                requires_editor=True,
            ),
            "create_asset": _cell(
                "supported",
                "cr_create_blueprint",
                "Use cr_create_blueprint with a skeleton or skeletal mesh source.",
                "Dedicated ControlRigBlueprint creation tool exists.",
                requires_editor=True,
                verification=["created path", "saved result"],
            ),
            "create_node": _cell(
                "supported",
                "cr_add_unit_node|cr_add_array_op_node|cr_add_template_node|cr_add_variable_node",
                "Use the family-specific cr_* node tool matching the RigVM node kind.",
                "Control Rig graph authoring is controller-centric and supported by dedicated tools.",
                requires_editor=True,
                verification=["cr_dump_graph", "cr_compile_and_save"],
            ),
            "wire_pin": _cell(
                "supported",
                "cr_add_link",
                "Use cr_add_link, then compile/save and dump to verify.",
                "RigVMController links are supported for Control Rig graphs.",
                requires_editor=True,
                verification=["cr_dump_graph", "cr_compile_and_save"],
            ),
            "set_defaults": _cell(
                "supported",
                "cr_set_pin_default|cr_add_member_variable",
                "Use Control Rig-specific variable/default tools.",
                "Dedicated cr_* tools set pin defaults and member variables.",
                requires_editor=True,
                verification=["cr_dump_graph", "cr_compile_and_save"],
            ),
            "compile": _cell(
                "supported",
                "cr_compile_and_save",
                "Use cr_compile_and_save after graph mutations; validate new patterns on disposable fixtures and check dirty packages.",
                "ControlRigBlueprintLibrary.recompile_vm path is exposed.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "compile response",
                    "save result",
                ],
            ),
            "save": _cell(
                "supported",
                "cr_compile_and_save",
                "Save through Control Rig compile/save flow; validate new patterns on disposable fixtures and check dirty packages.",
                "Control Rig save is part of the dedicated compile/save tool.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "save result",
                ],
            ),
        },
    },
    "material": {
        "capability_surface": "asset",
        "display_name": "Material",
        "canonical_surface": "MaterialEditingLibrary expression / connection APIs",
        "actions": {
            "inspect": _cell(
                "engine_supported_repo_not_exposed",
                "search_unreal_api|describe_unreal_api|future material_describe_graph",
                "Use API catalog now; add material_describe_graph before mutation.",
                "MaterialEditingLibrary is discoverable, but project Material asset catalog/tooling is not repo-exposed yet.",
            ),
            "create_asset": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer until Material-specific fixture exists.",
                "No repo Material asset creation adapter exists.",
            ),
            "create_node": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Do not route through Blueprint or Control Rig; implement MaterialEditingLibrary adapter with fixtures first.",
                "Material expressions use MaterialEditingLibrary, not K2/RigVM nodes.",
            ),
            "wire_pin": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Use no mutation now; future support should call MaterialEditingLibrary.connect_material_expressions/property with fixtures.",
                "Engine API exists but repo exposes no safe material connection tool yet.",
            ),
            "set_defaults": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer to material-specific read-only describe then fixtures.",
                "Expression defaults are material-graph-specific.",
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated through MaterialEditingLibrary.recompile_material with dirty-package verification.",
                "Material recompile is side-effecting and not repo-exposed as a tool.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "compile result",
                    "re-query",
                ],
            ),
            "save": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after save.",
                "Saving material assets mutates content state.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "save result",
                    "re-query",
                ],
            ),
        },
    },
    "material_function": {
        "capability_surface": "asset",
        "display_name": "MaterialFunction",
        "canonical_surface": "MaterialFunction graph via MaterialEditingLibrary function expression APIs",
        "actions": {
            "inspect": _cell(
                "engine_supported_repo_not_exposed",
                "search_unreal_api|describe_unreal_api|future material_function_describe_graph",
                "Use API catalog now; add read-only function graph describe before mutation.",
                "MaterialFunction APIs are distinct and not project-catalog-backed yet.",
            ),
            "create_asset": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer until MaterialFunction-specific fixture exists.",
                "No repo MaterialFunction creation adapter exists.",
            ),
            "create_node": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Implement MaterialFunction expression adapter only after fixtures.",
                "MaterialFunction expressions are not Blueprint/K2 or RigVM nodes.",
            ),
            "wire_pin": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Future support must use MaterialEditingLibrary function APIs with fixtures.",
                "Engine-level connection functions exist, but repo support is not exposed.",
            ),
            "set_defaults": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer to function-specific describe and fixtures.",
                "Expression defaults are material-function-specific.",
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after function update/recompile.",
                "Function update/recompile is side-effecting and not repo-exposed.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "compile result",
                    "re-query",
                ],
            ),
            "save": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after save.",
                "Saving material functions mutates content state.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "save result",
                    "re-query",
                ],
            ),
        },
    },
    "material_instance": {
        "capability_surface": "asset",
        "display_name": "MaterialInstance",
        "canonical_surface": "MaterialInstance parameter overrides; no expression graph",
        "actions": {
            "inspect": _cell(
                "partial",
                "search_unreal_api|describe_unreal_api|future material_instance_describe_parameters",
                "Use catalog/describe now; add read-only parameter inventory before mutation.",
                "MaterialInstance assets are catalogued, but this repo has no dedicated parameter describe tool yet.",
                caveats=[
                    "MaterialInstance assets do not expose a Material expression graph."
                ],
            ),
            "create_asset": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Defer until MaterialInstance-specific fixture exists.",
                "No repo MaterialInstance creation adapter exists.",
            ),
            "create_node": _cell(
                "unsupported",
                None,
                "Report unsupported; MaterialInstance assets do not have expression nodes to create.",
                "MaterialInstance editing is parameter override work, not graph-node authoring.",
            ),
            "wire_pin": _cell(
                "unsupported",
                None,
                "Report unsupported; MaterialInstance assets do not have expression pins to wire.",
                "MaterialInstance assets inherit parent material graphs rather than authoring new graph links.",
            ),
            "set_defaults": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Add read-only parameter describe and disposable fixtures before exposing parameter override mutation.",
                "MaterialInstance parameter overrides are a distinct surface from Material expression defaults.",
            ),
            "compile": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after any parent/update refresh.",
                "MaterialInstance update behavior is side-effecting and not repo-exposed as a tool.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "update result",
                    "re-query",
                ],
            ),
            "save": _cell(
                "engine_supported_repo_not_exposed",
                None,
                "Fixture-gated only; verify dirty packages before and after save.",
                "Saving material instances mutates content state.",
                requires_editor=True,
                verification=[
                    "disposable fixture",
                    "dirty-package check",
                    "save result",
                    "re-query",
                ],
            ),
        },
    },
}

_CAPABILITY_SURFACES: dict[str, dict[str, Any]] = {
    "tapython": {
        "canonical_surface": "TAPython diagnostics/capture; graph APIs are interactive UI-context-bound",
        "actions": {
            "status": _cell(
                "supported",
                "tapython_status",
                "Call tapython_status to discover installed libs and capability caveats.",
                "Status is diagnostic and does not mutate assets.",
                requires_editor=True,
            ),
            "capture_viewport": _cell(
                "supported",
                "tapython_capture_active_viewport",
                "Use only when active viewport capture is needed and TAPython is installed.",
                "Viewport capture is the TAPython capability that is generally usable through this MCP.",
                requires_editor=True,
            ),
            "create_node": _cell(
                "unsupported",
                None,
                "Report unsupported headless graph authoring; do not target arbitrary assets through TAPython graph APIs.",
                "TAPython graph APIs require interactive Chameleon/focused editor UI context.",
                caveats=[
                    "Use cr_* only for Control Rig graphs, not as a generic fallback."
                ],
            ),
            "dump_graph": _cell(
                "unsupported",
                None,
                "Report unsupported topology dump for arbitrary asset paths.",
                "get_graph_panel_nodes/get_all_k2_nodes are UI/focused-editor bound.",
            ),
            "apply_graph": _cell(
                "unsupported",
                None,
                "Report unsupported graph apply/recreate for arbitrary asset paths.",
                "Applying graph JSON requires UI-bound graph spawning/wiring APIs that this MCP cannot target headlessly.",
            ),
        },
    },
    "api_catalog": {
        "canonical_surface": "Local Unreal API catalog search/describe",
        "actions": {
            "status": _cell(
                "supported",
                "search_unreal_api|describe_unreal_api",
                "Use catalog search/describe before writing unreal.* calls.",
                "Catalog lookup is local/static once harvested and avoids hallucinated APIs.",
            ),
            "inspect": _cell(
                "supported",
                "search_unreal_api|describe_unreal_api",
                "Use search/describe for engine APIs and project Blueprint-like assets.",
                "The catalog is the first evidence surface for API names and project types.",
            ),
        },
    },
}


def _normalise(value: str | None, aliases: dict[str, str]) -> str:
    key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return aliases.get(key, key)


def _infer_family(asset_path: str) -> str:
    lower = (asset_path or "").lower()
    name = lower.rsplit("/", 1)[-1]
    if not lower:
        return ""
    if "controlrig" in lower or name.startswith("cr_"):
        return "control_rig"
    if name.startswith(("abp_", "anim_bp", "animblueprint")) or "animbp" in lower:
        return "animbp"
    if name.startswith(("w_", "wbp_")) or "widget" in lower or "/ui/" in lower:
        return "widget"
    if "materialfunction" in lower or name.startswith(("mf_", "mfn_")):
        return "material_function"
    if "materialinstance" in lower or name.startswith(("mi_", "mic_")):
        return "material_instance"
    if name.startswith("m_") or "/material" in lower or "/materials" in lower:
        return "material"
    if name.startswith(("bp_", "b_")):
        return "blueprint"
    return "unknown"


def _matrix() -> dict[str, Any]:
    return {
        "tool": "blueprint_family_capabilities",
        "query": {
            "asset_path": None,
            "asset_family": None,
            "capability_surface": None,
            "requested_action": None,
            "include_matrix": True,
        },
        "answer_source": "static_matrix",
        "requires_editor": False,
        "matrix_version": 1,
        "support_labels": list(_SUPPORT_LABELS),
        "families": copy.deepcopy(_FAMILIES),
        "capability_surfaces": copy.deepcopy(_CAPABILITY_SURFACES),
        "caveats": [
            "Router output is read-only and must not compile, save, create, delete, rename, or link assets.",
            "Mutation support may only move to supported after disposable fixture validation.",
            "Launch/config hardening is out of scope for this capability matrix.",
        ],
    }


def _project(
    *,
    asset_path: str = "",
    asset_family: str = "",
    capability_surface: str = "",
    requested_action: str = "",
) -> dict[str, Any]:
    family = _normalise(asset_family, _FAMILY_ALIASES)
    surface = _normalise(capability_surface, {})
    action = _normalise(requested_action, _ACTION_ALIASES)
    inferred_family = _infer_family(asset_path)
    if not family and inferred_family != "unknown":
        family = inferred_family
    if not surface and family:
        surface = "asset"

    base = {
        "tool": "blueprint_family_capabilities",
        "query": {
            "asset_path": asset_path or None,
            "asset_family": family or None,
            "capability_surface": surface or None,
            "requested_action": action or None,
            "include_matrix": False,
        },
        "answer_source": "static_matrix",
        "requires_editor": False,
        "matrix_version": 1,
        "support_labels": list(_SUPPORT_LABELS),
        "asset_path_family_guess": inferred_family if asset_path else None,
        "support": "unknown",
        "recommended_tool": None,
        "next_action": "Call without filters for the full matrix, or provide asset_family/capability_surface and requested_action.",
        "why": "No matching family/surface/action cell was selected.",
        "verification": [],
        "caveats": [],
    }

    source: dict[str, Any] | None = None
    if surface and surface != "asset":
        source = _CAPABILITY_SURFACES.get(surface)
        base["capability_surface"] = surface
    elif family:
        source = _FAMILIES.get(family)
        base["asset_family"] = family
        base["capability_surface"] = (
            source.get("capability_surface") if source else surface
        )
    if source is None:
        base["caveats"].append(
            "Unknown family or capability surface; no mutation is supported."
        )
        return base

    base["canonical_surface"] = source.get("canonical_surface", "")
    actions = source.get("actions", {})
    if not action:
        base["available_actions"] = sorted(actions)
        base["next_action"] = (
            "Choose one of available_actions or call with include_matrix=true for all cells."
        )
        base["why"] = (
            "Family/surface was identified, but no requested action was supplied."
        )
        return base

    cell = actions.get(action)
    if cell is None:
        base["available_actions"] = sorted(actions)
        base["next_action"] = (
            "Requested action is not in this row; choose an available action or use the full matrix."
        )
        base["why"] = f"Action {action!r} is not defined for this family/surface."
        return base

    base.update(copy.deepcopy(cell))
    base["requested_action"] = action
    return base


def capability_payload(
    *,
    asset_path: str = "",
    asset_family: str = "",
    capability_surface: str = "",
    requested_action: str = "",
    include_matrix: bool = False,
) -> dict[str, Any]:
    if include_matrix or not any(
        [asset_path, asset_family, capability_surface, requested_action]
    ):
        return _matrix()
    return _project(
        asset_path=asset_path,
        asset_family=asset_family,
        capability_surface=capability_surface,
        requested_action=requested_action,
    )


def _ok(data: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def register(server):
    @server.tool(
        name="blueprint_family_capabilities",
        description=(
            "Read-only capability router for Blueprint-family work. Use this before "
            "authoring graph/node scripts across Blueprint/K2, WidgetBlueprint, "
            "AnimBlueprint, Control Rig, Material/MaterialFunction/MaterialInstance, "
            "or TAPython. "
            "Returns support labels, recommended tools, stop conditions, and "
            "verification requirements. It never compiles, saves, creates, deletes, "
            "renames, links, or mutates assets."
        ),
    )
    async def blueprint_family_capabilities(
        asset_path: str = "",
        asset_family: str = "",
        capability_surface: str = "",
        requested_action: str = "",
        include_matrix: bool = False,
    ) -> list[TextContent]:
        return _ok(
            capability_payload(
                asset_path=asset_path,
                asset_family=asset_family,
                capability_surface=capability_surface,
                requested_action=requested_action,
                include_matrix=include_matrix,
            )
        )

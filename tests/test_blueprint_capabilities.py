import asyncio
import json
import unittest

from ue_ikrig_mcp.tools import blueprint_capabilities


class _CaptureServer:
    def __init__(self):
        self.tools = {}
        self.descriptions = {}

    def tool(self, *a, name=None, description=None, **k):
        def deco(fn):
            tool_name = name or fn.__name__
            self.tools[tool_name] = fn
            self.descriptions[tool_name] = description or ""
            return fn

        return deco


def _build():
    server = _CaptureServer()
    blueprint_capabilities.register(server)
    return server


def _payload(result):
    return json.loads(result[0].text)


class BlueprintCapabilityRouterTests(unittest.TestCase):
    def setUp(self):
        self.server = _build()
        self.tool = self.server.tools["blueprint_family_capabilities"]

    def call(self, **kwargs):
        return _payload(asyncio.run(self.tool(**kwargs)))

    def test_tool_description_is_read_only_and_agent_facing(self):
        desc = self.server.descriptions["blueprint_family_capabilities"]
        self.assertIn("Read-only", desc)
        self.assertIn("Blueprint/K2", desc)
        self.assertIn("TAPython", desc)
        self.assertIn("never compiles", desc)

    def test_no_arg_matrix_shape_and_complete_rows(self):
        out = self.call()
        self.assertEqual(out["tool"], "blueprint_family_capabilities")
        self.assertEqual(out["answer_source"], "static_matrix")
        self.assertFalse(out["requires_editor"])
        self.assertEqual(out["matrix_version"], 1)
        self.assertEqual(
            set(out["support_labels"]),
            {
                "supported",
                "partial",
                "experimental",
                "engine_supported_repo_not_exposed",
                "unsupported",
                "unknown",
            },
        )
        self.assertEqual(
            set(out["families"]),
            {
                "blueprint",
                "widget",
                "animbp",
                "control_rig",
                "material",
                "material_function",
                "material_instance",
            },
        )
        required_actions = {
            "inspect",
            "create_asset",
            "create_node",
            "wire_pin",
            "set_defaults",
            "compile",
            "save",
        }
        for family, row in out["families"].items():
            self.assertEqual(required_actions, set(row["actions"]), family)
            for action, cell in row["actions"].items():
                self.assertIn(cell["support"], out["support_labels"], (family, action))
                for key in [
                    "recommended_tool",
                    "requires_editor",
                    "verification",
                    "caveats",
                ]:
                    self.assertIn(key, cell, (family, action))
        tapython = out["capability_surfaces"]["tapython"]["actions"]
        self.assertTrue(
            {
                "status",
                "capture_viewport",
                "create_node",
                "dump_graph",
                "apply_graph",
            }.issubset(tapython)
        )

    def test_representative_queries(self):
        cr = self.call(asset_family="control_rig", requested_action="wire_pin")
        self.assertEqual(cr["support"], "supported")
        self.assertEqual(cr["recommended_tool"], "cr_add_link")
        self.assertIn("cr_compile_and_save", cr["verification"])

        anim = self.call(asset_family="animbp", requested_action="create_node")
        self.assertEqual(anim["support"], "unsupported")
        self.assertIsNone(anim["recommended_tool"])
        self.assertIn("TAPython", anim["why"])

        bp = self.call(asset_family="blueprint", requested_action="wire_pin")
        self.assertEqual(bp["support"], "engine_supported_repo_not_exposed")
        self.assertIn("K2", bp["why"])

        widget = self.call(asset_family="widget", requested_action="create_node")
        self.assertEqual(widget["support"], "engine_supported_repo_not_exposed")
        self.assertIn("designer", widget["next_action"].lower())

        material = self.call(asset_family="material", requested_action="wire_pin")
        self.assertEqual(material["support"], "engine_supported_repo_not_exposed")
        self.assertIn("MaterialEditingLibrary", material["next_action"])

        material_function = self.call(
            asset_family="material_function", requested_action="create_node"
        )
        self.assertEqual(
            material_function["support"], "engine_supported_repo_not_exposed"
        )
        self.assertIn("MaterialFunction", material_function["canonical_surface"])

        material_instance = self.call(
            asset_family="material_instance", requested_action="create_node"
        )
        self.assertEqual(material_instance["support"], "unsupported")
        self.assertIn("parameter override", material_instance["why"].lower())

        tap_status = self.call(capability_surface="tapython", requested_action="status")
        self.assertEqual(tap_status["support"], "supported")
        self.assertEqual(tap_status["recommended_tool"], "tapython_status")

        tap_node = self.call(
            capability_surface="tapython", requested_action="create_node"
        )
        self.assertEqual(tap_node["support"], "unsupported")
        self.assertIn("UI", tap_node["why"])

    def test_blueprint_guidance_uses_canonical_describe_asset_name(self):
        inspect_cell = self.call(asset_family="blueprint", requested_action="inspect")
        default_cell = self.call(
            asset_family="blueprint", requested_action="set_defaults"
        )

        joined = json.dumps([inspect_cell, default_cell])

        self.assertIn("blueprint_describe_asset", joined)
        self.assertNotIn("bp_describe_asset", joined)

    def test_mutating_asset_actions_include_fixture_and_dirty_package_guidance(self):
        matrix = self.call(include_matrix=True)

        for family, row in matrix["families"].items():
            for action in ("compile", "save"):
                cell = row["actions"][action]
                guidance = " ".join(
                    [
                        cell.get("next_action", ""),
                        cell.get("why", ""),
                        " ".join(cell.get("verification", [])),
                        " ".join(cell.get("caveats", [])),
                    ]
                ).lower()

                self.assertIn("fixture", guidance, (family, action))
                self.assertTrue(
                    "dirty" in guidance or "package" in guidance,
                    (family, action, guidance),
                )

    def test_editor_conditional_and_asset_guessing(self):
        matrix = self.call(include_matrix=True)
        self.assertFalse(matrix["requires_editor"])

        live_only = self.call(asset_family="blueprint", requested_action="compile")
        self.assertTrue(live_only["requires_editor"])
        self.assertEqual(live_only["support"], "engine_supported_repo_not_exposed")

        guessed = self.call(asset_path="/Game/UI/WBP_Menu", requested_action="inspect")
        self.assertEqual(guessed["asset_family"], "widget")
        self.assertEqual(guessed["support"], "partial")

        material_instance = self.call(
            asset_path="/Game/Characters/Hero/Materials/MI_Body",
            requested_action="inspect",
        )
        self.assertEqual(material_instance["asset_family"], "material_instance")
        self.assertEqual(material_instance["support"], "partial")

    def test_payload_contains_no_mutating_code_markers(self):
        # This router is static data only. Keep obvious mutation APIs out of the
        # module so future edits cannot quietly make the router perform work.
        import inspect

        source = inspect.getsource(blueprint_capabilities)
        forbidden = [
            "save_asset(",
            "compile_blueprint(",
            "recompile_material(",
            "delete_asset(",
            "rename_asset(",
            "add_link(",
            "create_asset(",
            "get_connection(",
            "safe_execute(",
        ]
        for marker in forbidden:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()

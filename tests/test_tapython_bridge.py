"""TAPython bridge honesty: the graph-authoring/inspection tools cannot work
headlessly (their APIs are bound to interactive editor UI context), so they must
return a clear capability error WITHOUT touching the editor, and the status script
must probe the real API locations (verified live against TAPython 1.3.3)."""

import asyncio
import json
import unittest

from ue_ikrig_mcp.tools import tapython_bridge
from ue_ikrig_mcp.tools.tapython_bridge import _STATUS_SCRIPT


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
    s = _CaptureServer()
    tapython_bridge.register(s)
    return s


def _payload(result):
    return json.loads(result[0].text)


class TestGraphToolsErrorHonestly(unittest.TestCase):
    """These three were built on unreal.PythonBPLib.spawn_function_to_graph /
    get_graph_panel_nodes — methods that do not exist there — so they always
    failed. They now return an accurate capability error with NO editor round-trip
    (so they're safe even when the editor is down)."""

    def setUp(self):
        self.tools = _build()

    def _call(self, name, **kw):
        return _payload(asyncio.run(self.tools.tools[name](**kw)))

    def test_create_animbp_node_reports_unavailable(self):
        out = self._call("tapython_create_animbp_node",
                         anim_bp_path="/Game/X", node_class_name="AnimGraphNode_ModifyBone")
        self.assertTrue(out["error"])
        self.assertIn("not available", out["message"].lower())
        self.assertIn("cr_*", out["message"])

    def test_apply_animgraph_json_reports_unavailable(self):
        out = self._call("tapython_apply_animgraph_json",
                         anim_bp_path="/Game/X", in_path="/tmp/x.json")
        self.assertTrue(out["error"])
        self.assertIn("ChameleonData", out["message"])

    def test_dump_animgraph_json_reports_unavailable(self):
        out = self._call("tapython_dump_animgraph_json", anim_bp_path="/Game/X")
        self.assertTrue(out["error"])
        self.assertIn("cr_dump_graph", out["message"])

    def test_no_editor_connection_needed(self):
        # If these tried to reach the editor they'd raise/hang here (no editor in
        # the unit-test env); returning cleanly proves they short-circuit.
        for name, kw in [
            ("tapython_create_animbp_node", {"anim_bp_path": "/Game/X", "node_class_name": "N"}),
            ("tapython_dump_animgraph_json", {"anim_bp_path": "/Game/X"}),
            ("tapython_apply_animgraph_json", {"anim_bp_path": "/Game/X", "in_path": "/tmp/x.json"}),
        ]:
            out = self._call(name, **kw)
            self.assertTrue(out["error"])


class TestStatusScriptTargetsRealApis(unittest.TestCase):
    """The status probe must use the live-verified API names, not the old wrong
    ones (get_tapython_version / PythonBPLib.spawn_function_to_graph)."""

    def test_uses_correct_version_call(self):
        self.assertIn("get_ta_python_version", _STATUS_SCRIPT)
        self.assertNotIn("get_tapython_version", _STATUS_SCRIPT)

    def test_probes_chameleon_and_bpasset(self):
        self.assertIn("ChameleonData", _STATUS_SCRIPT)
        self.assertIn("PythonBPAssetLib", _STATUS_SCRIPT)
        self.assertIn("get_all_chameleon_data_paths", _STATUS_SCRIPT)

    def test_does_not_probe_graph_methods_on_pythonbplib(self):
        # The old script checked PythonBPLib for spawn_function_to_graph etc.;
        # those live on ChameleonData, so PythonBPLib must not be the probe target
        # for the graph capability.
        self.assertIn("cd is not None and hasattr(cd, 'spawn_function_to_graph')", _STATUS_SCRIPT)


class TestToolDescriptionsHonest(unittest.TestCase):
    def setUp(self):
        self.server = _build()

    def test_graph_tool_descriptions_do_not_overpromise(self):
        for name in [
            "tapython_create_animbp_node",
            "tapython_dump_animgraph_json",
            "tapython_apply_animgraph_json",
        ]:
            desc = self.server.descriptions[name]
            self.assertIn("NOT available", desc, name)
            self.assertIn("headlessly", desc, name)
            self.assertIn("capability", desc.lower(), name)
        self.assertNotIn("Creates missing nodes and wires pins", self.server.descriptions["tapython_apply_animgraph_json"])
        self.assertNotIn("Useful for diffing", self.server.descriptions["tapython_dump_animgraph_json"])

    def test_status_remains_first_diagnostic_description(self):
        desc = self.server.descriptions["tapython_status"]
        self.assertIn("Use this before any other tapython_* tool", desc)
        self.assertIn("NOT available", desc)


if __name__ == "__main__":
    unittest.main()

"""Every tool builds an editor-side Python script by string-concatenation and
sends it to UE. A bad concatenation (e.g. splicing a helper call into an implicit
literal chain) yields a script that only fails at runtime in the editor. These
tests render each script offline through a fake connection and `ast.parse` it, so
a syntax regression is caught here instead of against a live editor.

Also asserts the mutation-safety guards (H5/H6/H7/H8) are present in the rendered
scripts."""

import ast
import asyncio
import json
import unittest

import ue_ikrig_mcp.ue_connection as uc
from ue_ikrig_mcp.tools import cr_author, batch_ops, op_management


class _CaptureConn:
    """Stands in for the UEConnection singleton: parses each script it is given
    (raising on a syntax error) and returns a benign success envelope."""

    def __init__(self):
        self.scripts = []

    def execute(self, code, mode="ExecuteFile", timeout=None):
        ast.parse(code)  # raises SyntaxError -> test failure if the script is malformed
        self.scripts.append(code)
        return {"success": True, "parsed": {"ok": True}}


class _CaptureServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, name=None, description=None, **k):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return deco


def _registry(*modules):
    s = _CaptureServer()
    for m in modules:
        m.register(s)
    return s


# Representative valid args for every tool that builds an editor script.
_CALLS = {
    # cr_author (11)
    "cr_create_blueprint": dict(rig_path="/Game/T/CR_X", skeleton_or_mesh_path="/Game/M.M"),
    "cr_delete_blueprint": dict(rig_path="/Game/T/CR_X"),
    "cr_add_member_variable": dict(rig_path="/Game/T/CR_X", variable_name="V", cpp_type="float"),
    "cr_add_unit_node": dict(rig_path="/Game/T/CR_X", struct_paths=["/Script/A.B"], node_name="N"),
    "cr_add_array_op_node": dict(rig_path="/Game/T/CR_X", op_code="ARRAY_ADD", element_cpp_type="float", node_name="N"),
    "cr_add_template_node": dict(rig_path="/Game/T/CR_X", template_notation="X::Execute()", node_name="N"),
    "cr_add_variable_node": dict(rig_path="/Game/T/CR_X", variable_name="V", cpp_type="float"),
    "cr_set_pin_default": dict(rig_path="/Game/T/CR_X", pin_path="N.P", value="1.0"),
    "cr_add_link": dict(rig_path="/Game/T/CR_X", from_pin="A.R", to_pin="B.I"),
    "cr_dump_graph": dict(rig_path="/Game/T/CR_X"),
    "cr_compile_and_save": dict(rig_path="/Game/T/CR_X"),
    # batch_ops (3)
    "batch_retargeter_ops": dict(retargeter_path="/Game/T/RTG", ops=[{"op": "save"}]),
    "bulk_adjust_bone_rotation": dict(retargeter_path="/Game/T/RTG", deltas={"b": [1.0, 0.0, 0.0]}),
    "bulk_set_chain_settings": dict(retargeter_path="/Game/T/RTG", settings={"c": {"enable_fk": True}}),
    # op_management (4)
    "set_retarget_op_enabled": dict(retargeter_path="/Game/T/RTG", op_index=0, enabled=True),
    "get_retarget_op_info": dict(retargeter_path="/Game/T/RTG"),
    "set_run_ik_rig_excluded_goals": dict(retargeter_path="/Game/T/RTG", goal_names=["g"]),
    "set_scale_source_factor": dict(retargeter_path="/Game/T/RTG", source_scale_factor=0.9),
}


class TestEditorScriptsParse(unittest.TestCase):
    def setUp(self):
        self._conn = _CaptureConn()
        # Point every tool's get_connection at the capturing fake.
        self._orig = uc.get_connection
        for mod in (uc, cr_author, batch_ops, op_management):
            mod.get_connection = lambda c=self._conn: c
        self.server = _registry(cr_author, batch_ops, op_management)

    def tearDown(self):
        for mod in (uc, cr_author, batch_ops, op_management):
            mod.get_connection = self._orig

    def test_every_script_is_valid_python(self):
        rendered = 0
        for name, kw in _CALLS.items():
            fn = self.server.tools.get(name)
            self.assertIsNotNone(fn, f"tool not registered: {name}")
            out = asyncio.run(fn(**kw))
            payload = json.loads(out[0].text)
            # A pre-send validation _err (no script built) is acceptable for some
            # tools, but none of these valid-arg calls should hit one.
            self.assertFalse(
                isinstance(payload, dict) and payload.get("error"),
                f"{name} returned an error for valid args: {payload}",
            )
            rendered += 1
        self.assertEqual(rendered, len(_CALLS))
        # _CaptureConn.execute already ast.parse'd every script; assert they ran.
        self.assertGreaterEqual(len(self._conn.scripts), rendered)

    def test_mutation_safety_guards_present(self):
        scripts = {}
        for name, kw in _CALLS.items():
            self._conn.scripts.clear()
            asyncio.run(self.server.tools[name](**kw))
            scripts[name] = self._conn.scripts[-1]

        # H6: class guard in CR mutators and retargeter tools.
        self.assertIn("type(rig_bp).__name__ != 'ControlRigBlueprint'", scripts["cr_add_unit_node"])
        self.assertIn("type(rig_bp).__name__ != 'ControlRigBlueprint'", scripts["cr_dump_graph"])
        self.assertIn("type(rtg).__name__ != 'IKRetargeter'", scripts["batch_retargeter_ops"])
        self.assertIn("type(rtg).__name__ != 'IKRetargeter'", scripts["set_scale_source_factor"])
        # H5: member-var must not save on failure (raises instead).
        self.assertIn("add_member_variable failed", scripts["cr_add_member_variable"])
        # H7: create must not delete before the new asset is confirmed.
        s = scripts["cr_create_blueprint"]
        self.assertIn("left intact", s)
        self.assertLess(s.index("create_asset"), s.index("delete_asset"),
                        "H7: create must occur before any delete")
        # H8: batch save gated on prior failures.
        self.assertIn("not saving", scripts["batch_retargeter_ops"])
        # H4: compile path also runs the BP compile to populate status.
        self.assertIn("compile_blueprint", scripts["cr_compile_and_save"])


if __name__ == "__main__":
    unittest.main()

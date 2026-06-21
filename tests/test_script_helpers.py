"""Phase 0 hardening: escape_string control-char safety + unwrap error surfacing.

Also covers the dogfood-validated fixes: safe_execute (D2 transport catch + H1
unwrap), validate_cpp_type (D1 crash gate), and the ARFilter constructor (G1)."""

import unittest

from ue_ikrig_mcp.ue_scripts import (
    escape_string,
    unwrap,
    safe_execute,
    validate_cpp_type,
    build_asset_registry_query,
)
from ue_ikrig_mcp.ue_connection import UEConnectionError, UENotRunningError


class TestEscapeString(unittest.TestCase):
    """escape_string output must round-trip when dropped into a Python literal,
    including control characters the old hand-rolled version missed (H2)."""

    DOUBLE = ['hand_l', 'a\nb', 'tab\there', 'cr\rlf', 'he said "hi"',
              'back\\slash', "O'Brien", 'café', '\x00\x01\x1f',
              'x"); import os; os.system("rm -rf /"); ("']

    def test_roundtrip_in_double_quoted_literal(self):
        for s in self.DOUBLE:
            literal = '"' + escape_string(s) + '"'
            self.assertEqual(eval(literal), s, f"double-quote round-trip failed for {s!r}")

    def test_roundtrip_in_single_quoted_literal(self):
        # Several call sites embed the result in '...'; single quotes are escaped
        # so that context stays safe too.
        for s in self.DOUBLE:
            literal = "'" + escape_string(s) + "'"
            self.assertEqual(eval(literal), s, f"single-quote round-trip failed for {s!r}")

    def test_newline_cannot_terminate_literal(self):
        # The core injection/breakage vector: a newline must be escaped, never raw.
        self.assertNotIn("\n", escape_string("line1\nline2"))


class TestUnwrap(unittest.TestCase):
    """unwrap collapses a transport dict into payload-or-error so editor-side
    failures stop masquerading as success (H1)."""

    def test_transport_failure_becomes_error(self):
        out = unwrap({"success": False, "result": "boom", "parsed": None, "hints": ["h"]})
        self.assertTrue(out["error"])
        self.assertEqual(out["message"], "boom")
        self.assertEqual(out["hints"], ["h"])

    def test_success_but_no_sentinel_is_error(self):
        out = unwrap({"success": True, "output": "noise", "parsed": None})
        self.assertTrue(out["error"])
        self.assertIn("sentinel", out["message"])

    def test_editor_side_error_envelope_surfaces(self):
        # wrap_script printed an error sentinel -> transport success True, parsed.error True.
        parsed = {"error": True, "message": "bad bone", "traceback": "..."}
        out = unwrap({"success": True, "output": "x", "parsed": parsed})
        self.assertTrue(out["error"])
        self.assertEqual(out["message"], "bad bone")

    def test_success_payload_passthrough(self):
        payload = {"added": True, "name": "Foo"}
        self.assertEqual(unwrap({"success": True, "parsed": payload}), payload)

    def test_list_payload_passthrough(self):
        payload = [{"path": "/Game/A"}, {"path": "/Game/B"}]
        self.assertEqual(unwrap({"success": True, "parsed": payload}), payload)

    def test_non_dict_input_is_error(self):
        out = unwrap("not a dict")
        self.assertTrue(out["error"])


class _FakeConn:
    """Minimal conn stand-in: either returns a transport dict or raises."""

    def __init__(self, ret=None, raises=None):
        self._ret = ret
        self._raises = raises
        self.calls = []

    def execute(self, code, mode="ExecuteFile", timeout=None):
        self.calls.append((code, mode, timeout))
        if self._raises is not None:
            raise self._raises
        return self._ret


class TestSafeExecute(unittest.TestCase):
    """safe_execute folds the D2 transport catch into the H1 unwrap so the tool
    layer never has to do either by hand."""

    def test_success_payload_passthrough(self):
        conn = _FakeConn(ret={"success": True, "parsed": {"ok": 1}})
        self.assertEqual(safe_execute(conn, "x"), {"ok": 1})

    def test_editor_side_error_surfaces(self):
        conn = _FakeConn(ret={"success": True, "parsed": {"error": True, "message": "nope"}})
        out = safe_execute(conn, "x")
        self.assertTrue(out["error"])
        self.assertEqual(out["message"], "nope")

    def test_connection_error_becomes_transport_envelope(self):
        conn = _FakeConn(raises=UEConnectionError("reset by peer"))
        out = safe_execute(conn, "x")
        self.assertTrue(out["error"])
        self.assertTrue(out["transport"])
        self.assertIn("reset by peer", out["message"])

    def test_not_running_error_becomes_transport_envelope(self):
        conn = _FakeConn(raises=UENotRunningError("editor down"))
        out = safe_execute(conn, "x")
        self.assertTrue(out["error"])
        self.assertTrue(out["transport"])

    def test_mode_and_timeout_forwarded(self):
        conn = _FakeConn(ret={"success": True, "parsed": []})
        safe_execute(conn, "code", mode="EvaluateStatement", timeout=12)
        self.assertEqual(conn.calls[-1], ("code", "EvaluateStatement", 12))


class TestValidateCppType(unittest.TestCase):
    """D1: bad type strings crash the editor (native check), so they must be
    rejected before any mutation forwards them."""

    def test_scalars_accepted(self):
        for t in ("bool", "int32", "float", "double", "FVector", "FString",
                  "FName", "FTransform", "FLinearColor"):
            self.assertIsNone(validate_cpp_type(t), t)

    def test_containers_accepted(self):
        for t in ("TArray<float>", "TMap<FName,float>", "TSet<int32>"):
            self.assertIsNone(validate_cpp_type(t), t)

    def test_object_paths_accepted(self):
        self.assertIsNone(validate_cpp_type("/Script/Engine.SkeletalMesh"))
        self.assertIsNone(validate_cpp_type("/Game/MyStuff/S_Thing"))

    def test_junk_rejected(self):
        # The exact string that crashed the live editor during dogfood.
        msg = validate_cpp_type("NotARealType")
        self.assertIsInstance(msg, str)
        self.assertIn("NotARealType", msg)

    def test_empty_rejected(self):
        self.assertIsInstance(validate_cpp_type(""), str)
        self.assertIsInstance(validate_cpp_type(None), str)


class TestARFilterConstructor(unittest.TestCase):
    """G1: ARFilter list props (class_paths/package_paths) must be passed to the
    constructor — attribute assignment raises 'cannot be edited on instances' on
    UE 5.5+, which broke list_skeletal_meshes / list_ik_assets live."""

    def test_uses_constructor_not_attribute_assignment(self):
        script = build_asset_registry_query("/Script/Engine.SkeletalMesh")
        self.assertIn("unreal.ARFilter(class_paths=", script)
        self.assertNotIn("ar_filter.class_paths =", script)
        self.assertNotIn("ar_filter.recursive_paths =", script)

    def test_package_paths_only_when_filtering(self):
        with_filter = build_asset_registry_query("/Script/Engine.SkeletalMesh", "/Game/Chars")
        self.assertIn("package_paths=", with_filter)
        without = build_asset_registry_query("/Script/Engine.SkeletalMesh")
        self.assertNotIn("package_paths=", without)


class TestToolModulesImport(unittest.TestCase):
    """Importing the server pulls in every tool module; this catches syntax or
    import regressions from the Phase 0 edits (e.g. the unwrap import)."""

    def test_server_and_tools_import(self):
        from ue_ikrig_mcp import server  # noqa: F401
        from ue_ikrig_mcp.ue_scripts import unwrap as _u  # noqa: F401
        for mod in ("cr_author", "animbp_inspect", "batch_ops",
                    "op_management", "tapython_bridge"):
            __import__(f"ue_ikrig_mcp.tools.{mod}")


if __name__ == "__main__":
    unittest.main()

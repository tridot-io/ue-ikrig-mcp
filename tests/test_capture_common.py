"""Unit tests for capture_common pure-Python helpers."""

import asyncio
import json
import os
import struct
import threading
import time
import unittest
import zlib

from ue_ikrig_mcp.tools.capture_common import (
    parse_mcp_result,
    png_payload,
    resolve_screenshot_path,
    wait_for_stable_file,
)


def _make_1x1_png() -> bytes:
    """Return a minimal valid 1x1 white PNG."""
    def chunk(name: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + name + data
        return c + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\xff\xff"  # filter byte + RGB
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


class TestWaitForStableFile(unittest.TestCase):
    def test_returns_false_on_timeout_when_file_never_appears(self):
        result = asyncio.run(
            wait_for_stable_file("/tmp/__nonexistent_capture_test__.png", timeout_s=0.5, poll_s=0.05)
        )
        self.assertFalse(result)

    def test_returns_true_when_file_appears_and_stays_stable(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
            f.write(b"x" * 64)
        try:
            result = asyncio.run(
                wait_for_stable_file(path, timeout_s=2.0, stable_checks=2, poll_s=0.05)
            )
            self.assertTrue(result)
        finally:
            os.unlink(path)

    def test_returns_false_when_file_keeps_growing(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name

        stop_event = threading.Event()

        def _writer():
            while not stop_event.is_set():
                with open(path, "ab") as fh:
                    fh.write(b"x" * 16)
                time.sleep(0.03)

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        try:
            result = asyncio.run(
                wait_for_stable_file(path, timeout_s=0.6, stable_checks=4, poll_s=0.05)
            )
            self.assertFalse(result)
        finally:
            stop_event.set()
            t.join(timeout=1.0)
            try:
                os.unlink(path)
            except OSError:
                pass


class TestParseMcpResult(unittest.TestCase):
    def test_extracts_from_dict_output_key(self):
        data = {"output": 'junk\n__MCP_RESULT__{"x": 1}\nmore', "result": ""}
        result = parse_mcp_result(data)
        self.assertEqual(result, {"x": 1})

    def test_extracts_from_raw_string(self):
        raw = 'some prefix __MCP_RESULT__{"status": "ok", "count": 42}'
        result = parse_mcp_result(raw)
        self.assertEqual(result, {"status": "ok", "count": 42})

    def test_returns_none_when_marker_absent(self):
        result = parse_mcp_result("no marker here")
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        result = parse_mcp_result("")
        self.assertIsNone(result)

    def test_handles_windows_paths_in_json(self):
        path_val = "C:/Users/test/Saved/Screenshots/Claude/capture_123.png"
        raw = '__MCP_RESULT__' + json.dumps({"path": path_val})
        result = parse_mcp_result(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["path"], path_val)

    def test_extracts_nested_json_correctly(self):
        payload = {"name": "RTG_Body", "class": "IKRetargeter"}
        raw = "__MCP_RESULT__" + json.dumps(payload)
        result = parse_mcp_result(raw)
        self.assertEqual(result["name"], "RTG_Body")
        self.assertEqual(result["class"], "IKRetargeter")

    def test_ignores_trailing_content_after_json_object(self):
        # UE stdout often has log lines after the marker; the depth scanner
        # should stop at the matching close brace.
        raw = '__MCP_RESULT__{"ok": true}\nLogPython: trailing log line'
        result = parse_mcp_result(raw)
        self.assertEqual(result, {"ok": True})


class TestResolveScreenshotPath(unittest.TestCase):
    def test_honors_ue_project_dir_env(self):
        old = os.environ.get("UE_PROJECT_DIR")
        try:
            os.environ["UE_PROJECT_DIR"] = "/fake/project"
            path = resolve_screenshot_path(ts_ms=12345)
            self.assertIn("12345", path)
            self.assertIn("Screenshots", path)
            self.assertIn("Claude", path)
            self.assertTrue(path.startswith("/fake/project"))
        finally:
            if old is None:
                os.environ.pop("UE_PROJECT_DIR", None)
            else:
                os.environ["UE_PROJECT_DIR"] = old

    def test_project_dir_arg_overrides_env(self):
        old = os.environ.get("UE_PROJECT_DIR")
        try:
            os.environ["UE_PROJECT_DIR"] = "/should/be/ignored"
            path = resolve_screenshot_path(ts_ms=99999, project_dir="/explicit/dir")
            self.assertTrue(path.startswith("/explicit/dir"))
            self.assertIn("99999", path)
        finally:
            if old is None:
                os.environ.pop("UE_PROJECT_DIR", None)
            else:
                os.environ["UE_PROJECT_DIR"] = old

    def test_format_contains_saved_screenshots_claude(self):
        path = resolve_screenshot_path(ts_ms=55555, project_dir="/p")
        self.assertIn("Saved", path)
        self.assertIn("Screenshots", path)
        self.assertIn("Claude", path)
        self.assertIn("capture_55555.png", path)

    def test_fallback_when_env_unset(self):
        old = os.environ.pop("UE_PROJECT_DIR", None)
        try:
            path = resolve_screenshot_path(ts_ms=1)
            self.assertTrue(path.endswith("capture_1.png"))
        finally:
            if old is not None:
                os.environ["UE_PROJECT_DIR"] = old


class TestPngPayload(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        self._tmp.write(_make_1x1_png())
        self._tmp.close()
        self.png_path = self._tmp.name

    def tearDown(self):
        try:
            os.unlink(self.png_path)
        except OSError:
            pass

    def test_returns_two_element_list(self):
        result = png_payload(self.png_path)
        self.assertEqual(len(result), 2)

    def test_first_element_is_image_content(self):
        from mcp.types import ImageContent
        result = png_payload(self.png_path)
        self.assertIsInstance(result[0], ImageContent)
        self.assertEqual(result[0].mimeType, "image/png")

    def test_second_element_is_text_content_with_valid_json(self):
        from mcp.types import TextContent
        result = png_payload(self.png_path)
        self.assertIsInstance(result[1], TextContent)
        parsed = json.loads(result[1].text)
        self.assertIn("captured_bytes", parsed)
        self.assertIsInstance(parsed["captured_bytes"], int)
        self.assertGreater(parsed["captured_bytes"], 0)

    def test_extra_text_merged_into_json(self):
        result = png_payload(self.png_path, extra_text={"asset_name": "RTG_Body"})
        parsed = json.loads(result[1].text)
        self.assertEqual(parsed["asset_name"], "RTG_Body")
        self.assertIn("captured_bytes", parsed)

    def test_image_content_is_base64_encoded_png(self):
        import base64
        result = png_payload(self.png_path)
        raw = base64.b64decode(result[0].data)
        self.assertTrue(raw.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()

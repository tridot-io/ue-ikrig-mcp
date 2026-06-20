"""Regression guard for the default EditorOps native delegation path.

WHY THIS FILE EXISTS
--------------------
The broker's EditorOps class execs ``ue_connection._EDITOR_PROTOCOL_SCRIPT`` into
a private namespace to reuse daemon_execute / discover / editor_process_check /
close_all_channels VERBATIM.  That script body is a raw string embedded in
ue_connection.py; its ``def`` statements are NOT module-level attributes of
ue_connection.  A previous implementation called ``uc.daemon_execute`` (an
AttributeError) because CI never exercised the real path -- every other test
injects a fake editor.

The two tests below make the real delegation path a permanent CI gate:

  1. Bridge-script completeness: exec the script into a fresh namespace and
     assert all four expected callables are defined.  Catches any future edit
     that renames, removes, or syntax-breaks a function inside the script.

  2. Default EditorOps live path: construct an *uninjected* EditorOps() and
     call ``discover`` and ``process_check`` on this CI box (no real Unreal
     Editor).  Both must return a dict without raising AttributeError, proving
     the exec-into-namespace delegation wiring is correct end-to-end.

CI notes:
  - discover(timeout=0.2) returns {'ok': False, 'nodes': []} on a box without
    an Unreal Editor -- that is the expected editorless result; we only assert
    it is a dict (not an exception).
  - editor_process_check() shells out to ``tasklist`` on Windows; on Linux/WSL
    it catches the FileNotFoundError and returns
    {'editor_process_alive': None, ...} -- a dict, never an exception.  We
    assert it is a dict with an 'editor_process_alive' key, not that the value
    is True/False, so the test is OS-agnostic.
"""

import unittest

from ue_ikrig_mcp import broker as bk
from ue_ikrig_mcp import ue_connection as uc


class BridgeScriptCompletenessTests(unittest.TestCase):
    """Guard: exec the embedded bridge script and assert all expected callables
    are defined in the resulting namespace."""

    def setUp(self):
        self._ns: dict = {'__name__': 'test_bridge_completeness'}
        exec(uc._EDITOR_PROTOCOL_SCRIPT, self._ns)

    def test_daemon_execute_is_defined_and_callable(self):
        self.assertIn('daemon_execute', self._ns,
                      'daemon_execute missing from _EDITOR_PROTOCOL_SCRIPT namespace')
        self.assertTrue(callable(self._ns['daemon_execute']),
                        'daemon_execute is defined but not callable')

    def test_discover_is_defined_and_callable(self):
        self.assertIn('discover', self._ns,
                      'discover missing from _EDITOR_PROTOCOL_SCRIPT namespace')
        self.assertTrue(callable(self._ns['discover']),
                        'discover is defined but not callable')

    def test_editor_process_check_is_defined_and_callable(self):
        self.assertIn('editor_process_check', self._ns,
                      'editor_process_check missing from _EDITOR_PROTOCOL_SCRIPT namespace')
        self.assertTrue(callable(self._ns['editor_process_check']),
                        'editor_process_check is defined but not callable')

    def test_close_all_channels_is_defined_and_callable(self):
        self.assertIn('close_all_channels', self._ns,
                      'close_all_channels missing from _EDITOR_PROTOCOL_SCRIPT namespace')
        self.assertTrue(callable(self._ns['close_all_channels']),
                        'close_all_channels is defined but not callable')

    def test_all_four_callables_defined_in_one_exec(self):
        # Belt-and-suspenders: one exec with the exact same namespace the
        # production EditorOps._namespace() uses; assert all four at once so a
        # single missing name surfaces clearly.
        ns: dict = {'__name__': 'ue_ikrig_mcp_broker_editor_ops'}
        exec(uc._EDITOR_PROTOCOL_SCRIPT, ns)
        missing = [
            name for name in
            ('daemon_execute', 'discover', 'editor_process_check', 'close_all_channels')
            if name not in ns or not callable(ns[name])
        ]
        self.assertEqual(
            missing, [],
            'These names are missing or non-callable after exec(_EDITOR_PROTOCOL_SCRIPT): %r'
            % missing,
        )


class DefaultEditorOpsDelegationTests(unittest.TestCase):
    """Guard: the default (uninjected) EditorOps resolves via exec-into-namespace.

    These tests construct a REAL EditorOps() with no fake injected, proving the
    exec wiring is correct end-to-end.  A regression to ``uc.daemon_execute``
    (AttributeError) would surface here immediately rather than at the first
    native dispatch in production.
    """

    def setUp(self):
        self._ops = bk.EditorOps()

    def test_discover_returns_dict_without_AttributeError(self):
        # On CI (no Unreal Editor), discover times out fast and returns a dict
        # with ok=False and nodes=[].  The assertion is type only -- we do not
        # require a live editor.
        result = self._ops.discover({'op': 'discover', 'timeout': 0.2})
        self.assertIsInstance(
            result, dict,
            'EditorOps.discover did not return a dict (possible AttributeError or exec failure)',
        )
        # Must have at least an 'ok' key -- confirms the real discover function ran.
        self.assertIn('ok', result,
                      "discover result dict missing 'ok' key")

    def test_process_check_returns_dict_with_editor_process_alive_key(self):
        # On Windows: shells out to tasklist, returns True/False.
        # On Linux/WSL: catches FileNotFoundError, returns {'editor_process_alive': None, ...}.
        # Either way: must be a dict with the key, never an exception.
        result = self._ops.process_check()
        self.assertIsInstance(
            result, dict,
            'EditorOps.process_check did not return a dict',
        )
        self.assertIn(
            'editor_process_alive', result,
            "process_check result missing 'editor_process_alive' key",
        )

    def test_namespace_is_cached_after_first_call(self):
        # _namespace() should exec once and reuse.  Calling discover twice must
        # return the same namespace object (not re-exec the script each time).
        self._ops.discover({'op': 'discover', 'timeout': 0.2})
        ns1 = self._ops._ns
        self._ops.discover({'op': 'discover', 'timeout': 0.2})
        ns2 = self._ops._ns
        self.assertIs(ns1, ns2,
                      'EditorOps re-execs the bridge script on every call (namespace not cached)')

    def test_close_all_does_not_raise(self):
        # close_all() wraps close_all_channels() in a bare except so it must
        # never propagate.  Trigger namespace init first (via discover) then call.
        self._ops.discover({'op': 'discover', 'timeout': 0.2})
        # Must not raise regardless of channel state.
        try:
            self._ops.close_all()
        except Exception as exc:  # pragma: no cover
            self.fail('EditorOps.close_all() raised unexpectedly: %r' % exc)


if __name__ == '__main__':
    unittest.main()

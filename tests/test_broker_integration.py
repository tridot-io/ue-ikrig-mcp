"""UEConnection <-> broker integration coverage.

This CI/dev box IS WSL, so ``ue_connection._broker_supported()`` is gated OFF by
default (``_is_wsl()`` returns True). To exercise the native broker path here we
monkeypatch ``ue_connection._is_wsl`` to return False -- the clean seam the
implementer used in the smoke tests -- and inject a fake broker client/module so
no real broker process is spawned.

We stand up a REAL in-process broker (loopback + injected fake editor) and point
``broker.connect_or_spawn`` at it via monkeypatch, so the UEConnection client path
(_connect_via_broker / _execute_via_broker / get_status broker section /
_probe_broker_liveness) runs end-to-end against a genuine broker.
"""

import os
import tempfile
import threading
import time
import unittest

from ue_ikrig_mcp import broker as bk
from ue_ikrig_mcp import ue_connection as uc


class _RecordingEditor:
    def __init__(self):
        self.lock = threading.Lock()
        self.sendall_count = 0
        self.run_calls = []
        self._result = {'ok': True, 'result': {'value': 'ok'}}

    def set_result(self, result):
        self._result = result

    def run_command(self, payload):
        with self.lock:
            self.sendall_count += 1
            self.run_calls.append(payload)
        return self._result

    def discover(self, payload):
        return {'ok': True, 'nodes': [{'node_id': 'fake-node'}]}

    def process_check(self):
        return {'editor_process_alive': True}

    def close_all(self):
        pass


class BrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ue-broker-int-')
        self.addCleanup(self._tmp.cleanup)
        self._saved_dir = os.environ.get('UE_BROKER_DIR')
        os.environ['UE_BROKER_DIR'] = self._tmp.name
        self.addCleanup(self._restore_dir)

        # Force the native broker path on this WSL box.
        self._orig_is_wsl = uc._is_wsl
        uc._is_wsl = lambda *a, **k: False
        self.addCleanup(lambda: setattr(uc, '_is_wsl', self._orig_is_wsl))

        self.editor = _RecordingEditor()
        self.advert_file = os.path.join(self._tmp.name, 'broker.advert.json')
        self.broker = bk.Broker(
            host=bk.BROKER_HOST, port=0, editor=self.editor,
            advert_file=self.advert_file, grace_seconds=30.0,
        )
        self.broker.bind()
        self.port = self.broker.port
        self._serve_thread = threading.Thread(target=self.broker.serve, daemon=True)
        self._serve_thread.start()
        self._wait(lambda: bk.read_advert(self.advert_file) is not None,
                   'broker did not advertise')
        self.addCleanup(self.broker.shutdown)

        # Point connect_or_spawn at our live in-process broker.
        self._orig_connect_or_spawn = bk.connect_or_spawn

        def fake_connect_or_spawn(*a, **k):
            c = bk.BrokerClient(bk.BROKER_HOST, self.port)
            if not c.connect(timeout=2.0):
                return None
            return c

        bk.connect_or_spawn = fake_connect_or_spawn
        self.addCleanup(
            lambda: setattr(bk, 'connect_or_spawn', self._orig_connect_or_spawn))

    def _restore_dir(self):
        if self._saved_dir is None:
            os.environ.pop('UE_BROKER_DIR', None)
        else:
            os.environ['UE_BROKER_DIR'] = self._saved_dir

    @staticmethod
    def _wait(pred, msg, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.01)
        raise AssertionError(msg)

    def test_broker_supported_true_when_wsl_patched_off(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._broker_supported())

    def test_connect_via_broker_pins_discovered_node(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        ok = conn._connect_via_broker(node_id=None, timeout=2.0)
        self.assertTrue(ok)
        self.assertEqual(conn._remote_node_id, 'fake-node')
        self.assertTrue(conn._broker_connected)

    def test_execute_via_broker_runs_command_once(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._connect_via_broker(node_id='fake-node', timeout=2.0))
        result = conn._execute_via_broker('print(1)', 'exec', timeout=5.0)
        self.assertIsInstance(result, dict)
        self.assertEqual(self.editor.sendall_count, 1)

    def test_get_status_broker_section_reports_connected(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._connect_via_broker(node_id='fake-node', timeout=2.0))
        section = conn._broker_status_section()
        self.assertTrue(section['enabled'])
        self.assertTrue(section['supported'])
        self.assertTrue(section['connected'])
        self.assertTrue(section['available'])
        self.assertEqual(section['transport'], 'loopback_tcp')
        self.assertEqual(section['endpoint'], [bk.BROKER_HOST, self.port])
        # Advert is peeked from disk without forcing a spawn.
        self.assertEqual(section['advert']['port'], self.port)

    def test_probe_broker_liveness_reports_broker_connected(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._connect_via_broker(node_id='fake-node', timeout=2.0))
        live = conn._probe_broker_liveness()
        self.assertTrue(live['ok'])
        self.assertEqual(live['state'], 'broker_connected')
        self.assertEqual(live['transport'], 'broker')
        self.assertEqual(live['node_id'], 'fake-node')

    def test_probe_broker_liveness_surfaces_death_after_broker_shutdown(self):
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._connect_via_broker(node_id='fake-node', timeout=2.0))
        self.broker.shutdown()
        self._wait(lambda: not conn._broker_request({'op': 'ping'}, timeout=0.5),
                   'broker still answering after shutdown', timeout=5.0)
        live = conn._probe_broker_liveness()
        self.assertFalse(live['ok'])
        self.assertEqual(live['state'], 'broker_unreachable')
        # The dead client was dropped so the next connect re-elects.
        self.assertFalse(conn._broker_connected)

    def test_execute_via_broker_surfaces_not_retries_broker_poisoned_result(self):
        # MAJOR-1 end-to-end: when the broker returns delivered:False AND
        # broker_poisoned:True, _execute_via_broker must SURFACE a UEConnectionError
        # to the caller -- it must NOT auto-retry (which would re-hit the same
        # contended slot) and must NOT silently re-pin + resend.
        #
        # The auto-retry condition in _execute_via_broker is:
        #   delivered is False AND NOT broker_busy AND NOT broker_poisoned
        # This test proves broker_poisoned is excluded from that condition.
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertTrue(conn._connect_via_broker(node_id='fake-node', timeout=2.0))

        # Inject a broker_poisoned result from the editor-op layer.
        self.editor.set_result({
            'ok': False,
            'error': 'broker channel poisoned (prior command timed out)',
            'delivered': False,
            'broker_poisoned': True,
        })

        resend_count = {'n': 0}
        orig_run = self.editor.run_command

        def counting_run(payload):
            resend_count['n'] += 1
            return orig_run(payload)

        self.editor.run_command = counting_run

        # _execute_via_broker must raise UEConnectionError (surfaced), not retry.
        with self.assertRaises(uc.UEConnectionError) as ctx:
            conn._execute_via_broker('print(1)', 'exec', timeout=5.0)

        err = str(ctx.exception)
        self.assertIn('poisoned', err.lower(),
                      'surfaced error should mention the poisoned condition')
        # The command was dispatched at most once (the initial attempt that
        # produced broker_poisoned). The auto-retry path (which would send again)
        # must NOT have fired.
        self.assertEqual(resend_count['n'], 1,
                         '_execute_via_broker resent the command into a poisoned slot')

        # The client's reconnect/resend path (_reconnect_after_restart_broker)
        # must NOT have been called for a broker_poisoned result. Verify by
        # confirming the remote_node_id was NOT cleared (reconnect resets it to None).
        self.assertEqual(conn._remote_node_id, 'fake-node',
                         '_reconnect_after_restart_broker was called for broker_poisoned')

    def test_broker_supported_false_under_real_wsl(self):
        # Restore the real WSL detection for this one assertion: on this box it is
        # True, so the broker must be gated OFF (it is native-Windows-only).
        uc._is_wsl = self._orig_is_wsl
        conn = uc.UEConnection()
        self.addCleanup(conn.disconnect)
        self.assertFalse(conn._broker_supported())
        # A connect attempt under the WSL gate records the unavailable reason (the
        # reason is populated by the attempt, not by _broker_supported() alone), so
        # the fallback to the per-process direct path is never a silent no-op.
        self.assertFalse(conn._try_connect_broker())
        self.assertEqual(conn._broker_unavailable_reason, 'wsl_unsupported')
        section = conn._broker_status_section()
        self.assertEqual(section['reason'], 'wsl_unsupported')


if __name__ == '__main__':
    unittest.main()

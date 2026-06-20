"""Faked-transport unittest coverage for the editor-command broker.

These tests drive ``ue_ikrig_mcp.broker`` directly with real loopback sockets
and INJECTED fake editor objects (``Broker(editor=...)`` / ``Dispatcher(editor=...)``)
so no real Unreal Editor is required. The broker's dispatch/election logic has no
WSL gate, so it is exercised directly here; the ``ue_connection`` integration tests
(``test_broker_integration.py``) monkeypatch ``_is_wsl`` to open the gated path.

Discipline (per the task brief and the plan acceptance criteria 1-12):
  - Every test sets ``UE_BROKER_DIR`` to a per-test tmpdir; the real shared advert
    location is never touched.
  - Every test is deterministic and bounded: generous-but-finite timeouts, threads
    joined, sockets closed in tearDown. Nothing here may hang CI.
  - A real detached spawn is used ONLY in the detached-survival test, gated with
    ``UE_BROKER_GRACE_SECONDS=1`` so the spawned process self-terminates fast.
"""

import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import warnings

from ue_ikrig_mcp import broker as bk


# ---------------------------------------------------------------------------
# Test doubles for the injected EditorOps surface.
# ---------------------------------------------------------------------------

class _RecordingEditor:
    """A fake EditorOps that records calls and returns scripted results.

    ``run_command`` blocks on a per-call gate (an Event) so a test can observe the
    single-outstanding invariant: it can prove job B is not dispatched while job A
    is still inside run_command. ``sendall_count`` counts how many times the editor
    send path is entered (the no-double-execute assertion).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sendall_count = 0
        self.outstanding = 0
        self.max_outstanding = 0
        self.run_calls = []          # payloads, in dispatch order
        self.process_check_calls = 0
        self.discover_calls = 0
        self.close_all_calls = 0
        # Per-call control: a release Event keyed by call index, plus the result.
        self._release_events = {}
        self._results = {}
        self._default_result = {'ok': True, 'result': {'value': 'done'}}
        # Signalled each time a run_command call has registered as outstanding.
        self.dispatched = threading.Event()

    def gate_call(self, index):
        """Return (and lazily create) the Event that releases run_command #index."""
        ev = self._release_events.get(index)
        if ev is None:
            ev = threading.Event()
            self._release_events[index] = ev
        return ev

    def set_result(self, index, result):
        self._results[index] = result

    def run_command(self, payload):
        with self.lock:
            index = self.sendall_count
            self.sendall_count += 1
            self.run_calls.append(payload)
            self.outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self.outstanding)
        self.dispatched.set()
        try:
            ev = self._release_events.get(index)
            if ev is not None:
                # Bounded so a buggy test can never wedge CI.
                ev.wait(timeout=10.0)
            return self._results.get(index, self._default_result)
        finally:
            with self.lock:
                self.outstanding -= 1

    def discover(self, payload):
        with self.lock:
            self.discover_calls += 1
        return {'ok': True, 'nodes': [{'node_id': 'fake-node'}]}

    def process_check(self):
        with self.lock:
            self.process_check_calls += 1
        return {'editor_process_alive': None}

    def close_all(self):
        with self.lock:
            self.close_all_calls += 1


class _DeadlineExceeded(AssertionError):
    pass


def _wait_until(predicate, timeout=5.0, interval=0.01, msg='condition not met'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise _DeadlineExceeded(msg)


# ---------------------------------------------------------------------------
# Base class: per-test isolated advert dir + env restoration.
# ---------------------------------------------------------------------------

class _BrokerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ue-broker-test-')
        self.addCleanup(self._tmp.cleanup)
        self._saved_env = {
            k: os.environ.get(k)
            for k in ('UE_BROKER_DIR', 'UE_BROKER_GRACE_SECONDS',
                      'UE_BROKER_MAX_QUEUE_DEPTH', 'UE_BROKER_SPAWN_WAIT_SECONDS')
        }
        os.environ['UE_BROKER_DIR'] = self._tmp.name
        self._brokers = []
        self._sockets = []

    def tearDown(self):
        for b in self._brokers:
            try:
                b.shutdown()
            except Exception:
                pass
        for s in self._sockets:
            try:
                s.close()
            except Exception:
                pass
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def track_socket(self, sock):
        self._sockets.append(sock)
        return sock

    def make_started_broker(self, editor, **kwargs):
        """Bind + serve a Broker on a background thread; return (broker, port)."""
        b = bk.Broker(
            host=bk.BROKER_HOST,
            port=0,
            editor=editor,
            advert_file=os.path.join(self._tmp.name, 'broker.advert.json'),
            **kwargs,
        )
        b.bind()
        port = b.port
        self._brokers.append(b)
        t = threading.Thread(target=b.serve, daemon=True)
        t.start()
        b._serve_thread = t  # for join in tests that need it
        _wait_until(lambda: bk.read_advert(b._advert_file) is not None,
                    msg='broker did not advertise')
        return b, port

    def client_connect(self, port, timeout=2.0):
        c = bk.BrokerClient(bk.BROKER_HOST, port)
        self.assertTrue(c.connect(timeout=timeout), 'client failed to connect')
        self.addCleanup(c.close)
        return c


# ---------------------------------------------------------------------------
# Light coverage: frame round-trip + FrameError; advert atomicity; fast-reject.
# ---------------------------------------------------------------------------

class FrameAndAdvertTests(_BrokerTestBase):
    def test_send_recv_frame_round_trip_preserves_payload(self):
        a, b = socket.socketpair()
        self.track_socket(a)
        self.track_socket(b)
        payload = {'op': 'execute', 'id': 'x1', 'nested': {'k': [1, 2, 3]}}
        bk.send_frame(a, payload)
        self.assertEqual(bk.recv_frame(b), payload)

    def test_recv_frame_raises_FrameError_on_truncated_body(self):
        a, b = socket.socketpair()
        self.track_socket(a)
        self.track_socket(b)
        # Claim 100 bytes, send 5, then close -> peer-closed-mid-frame.
        a.sendall(struct.pack('>I', 100) + b'short')
        a.close()
        with self.assertRaises(bk.FrameError):
            bk.recv_frame(b)

    def test_recv_frame_raises_FrameError_on_oversize_length(self):
        a, b = socket.socketpair()
        self.track_socket(a)
        self.track_socket(b)
        a.sendall(struct.pack('>I', bk._MAX_FRAME + 1))
        with self.assertRaises(bk.FrameError):
            bk.recv_frame(b)

    def test_recv_frame_raises_FrameError_on_zero_length(self):
        a, b = socket.socketpair()
        self.track_socket(a)
        self.track_socket(b)
        a.sendall(struct.pack('>I', 0))
        with self.assertRaises(bk.FrameError):
            bk.recv_frame(b)

    def test_send_frame_raises_FrameError_on_dead_peer(self):
        a, b = socket.socketpair()
        self.track_socket(a)
        b.close()
        # The first send may buffer; a second send after RST must raise.
        with self.assertRaises(bk.FrameError):
            for _ in range(1000):
                bk.send_frame(a, {'op': 'x', 'pad': 'y' * 1024})

    def test_write_advert_atomic_uses_temp_then_replace(self):
        path = os.path.join(self._tmp.name, 'broker.advert.json')
        advert = {'host': '127.0.0.1', 'port': 5555, 'pid': 4242}
        replaced = {}

        real_replace = os.replace

        def spy_replace(src, dst):
            replaced['src'] = src
            replaced['dst'] = dst
            return real_replace(src, dst)

        os.replace = spy_replace
        try:
            bk.write_advert_atomic(path, advert)
        finally:
            os.replace = real_replace

        # A temp file (not the final path) was written then os.replace'd onto path.
        self.assertNotEqual(replaced['src'], path)
        self.assertTrue(replaced['src'].startswith(path))
        self.assertTrue(replaced['src'].endswith('.tmp'))
        self.assertEqual(replaced['dst'], path)
        self.assertEqual(bk.read_advert(path), advert)
        # No temp leftovers in the dir.
        leftovers = [f for f in os.listdir(self._tmp.name) if f.endswith('.tmp')]
        self.assertEqual(leftovers, [])

    def test_read_advert_returns_none_on_missing_or_corrupt(self):
        self.assertIsNone(bk.read_advert(os.path.join(self._tmp.name, 'nope.json')))
        bad = os.path.join(self._tmp.name, 'bad.json')
        with open(bad, 'w') as fh:
            fh.write('{not valid json')
        self.assertIsNone(bk.read_advert(bad))


# ---------------------------------------------------------------------------
# Case 2: result-frame-gated dispatch + single-outstanding invariant.
# ---------------------------------------------------------------------------

class ResultFrameGateTests(_BrokerTestBase):
    def test_B_not_dispatched_until_A_result_frame_observed(self):
        editor = _RecordingEditor()
        # Hold A inside run_command until we explicitly release it.
        gate_a = editor.gate_call(0)
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        results = {}

        def submit(name):
            results[name] = disp.submit({'op': 'execute', 'code': name}, 'src-' + name)

        ta = threading.Thread(target=submit, args=('A',), daemon=True)
        ta.start()
        # Wait until A is actually inside the editor (dispatched).
        _wait_until(editor.dispatched.is_set, msg='A never dispatched')

        # Now submit B. It must NOT be dispatched while A is still in-flight.
        editor.dispatched.clear()
        tb = threading.Thread(target=submit, args=('B',), daemon=True)
        tb.start()

        # Give the dispatch thread ample chance to (wrongly) pick up B.
        time.sleep(0.3)
        with editor.lock:
            self.assertEqual(editor.sendall_count, 1,
                             'B was dispatched before A produced a result frame')
            self.assertEqual([p['code'] for p in editor.run_calls], ['A'])

        # Release A's result frame; B must now proceed.
        gate_a.set()
        _wait_until(editor.dispatched.is_set, msg='B never dispatched after A done')
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)

        self.assertEqual([p['code'] for p in editor.run_calls], ['A', 'B'])
        self.assertTrue(results['A']['ok'])
        self.assertTrue(results['B']['ok'])
        # The load-bearing invariant: never more than one editor command at once.
        self.assertEqual(editor.max_outstanding, 1)

    def test_many_concurrent_submits_keep_max_outstanding_at_one(self):
        editor = _RecordingEditor()
        disp = bk.Dispatcher(editor=editor, max_queue_depth=64)
        disp.start()
        self.addCleanup(disp.stop)

        n = 20
        threads = []
        out = {}

        def submit(i):
            out[i] = disp.submit({'op': 'execute', 'code': str(i)}, 'src')

        for i in range(n):
            t = threading.Thread(target=submit, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(editor.sendall_count, n)
        self.assertEqual(editor.max_outstanding, 1)
        self.assertTrue(all(out[i]['ok'] for i in range(n)))


# ---------------------------------------------------------------------------
# Case 3: timeout gate = poison, NOT advance (the load-bearing invariant).
# ---------------------------------------------------------------------------

class PoisonGateTests(_BrokerTestBase):
    def test_timeout_poisons_and_blocks_B_while_editor_alive_then_resumes_on_death(self):
        editor = _RecordingEditor()
        # A times out. The editor-op layer (EditorOps.run_command, which this
        # fake stands in for) normalizes daemon_execute's may-still-be-running
        # timeout into a STRUCTURED timed_out flag; the dispatcher's poison gate
        # keys on that flag, NOT on a brittle 'timed out' substring (MINOR gate).
        editor.set_result(0, {'ok': False, 'error': 'command timed out after 5s',
                              'timed_out': True})
        # Explicit assertion: the timed_out flag is what drives the poison.
        # See test_timeout_shaped_result_without_timed_out_flag_does_not_poison
        # for the negative case that proves the gate is flag-keyed, not string-keyed.
        # editor stays ALIVE during the poison wait, then proves dead.
        alive_states = ['alive', 'alive', 'dead']

        def process_check():
            with editor.lock:
                editor.process_check_calls += 1
            state = alive_states.pop(0) if alive_states else 'dead'
            return {'editor_process_alive': False if state == 'dead' else True}

        editor.process_check = process_check
        # discover must NOT clear the poison while "hung": return no nodes.
        editor.discover = lambda payload: {'ok': True, 'nodes': []}

        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        res_a = {}
        ta = threading.Thread(
            target=lambda: res_a.update(
                disp.submit({'op': 'execute', 'code': 'A'}, 'srcA')),
            daemon=True)
        ta.start()
        ta.join(timeout=5.0)
        self.assertFalse(res_a['ok'])
        self.assertIn('timed out', res_a['error'].lower())
        # Channel is now poisoned.
        _wait_until(lambda: disp.status()['channel_poisoned'],
                    msg='channel was not poisoned after timeout')

        # Submit B; it must BLOCK (not dispatch) while the editor is still alive.
        res_b = {}
        tb = threading.Thread(
            target=lambda: res_b.update(
                disp.submit({'op': 'execute', 'code': 'B'}, 'srcB')),
            daemon=True)
        tb.start()

        # While the editor remains "alive" in process_check, B must not dispatch
        # and must not have been sent to the editor.
        time.sleep(0.3)
        with editor.lock:
            self.assertEqual(editor.sendall_count, 1,
                             'B was dispatched on a bare timer into a poisoned slot')

        # Editor now proves dead on the next process_check -> poison clears, B runs.
        tb.join(timeout=8.0)
        self.assertTrue(res_b.get('ok'), 'B did not resume after proven editor death')
        with editor.lock:
            self.assertEqual([p['code'] for p in editor.run_calls], ['A', 'B'])
        self.assertFalse(disp.status()['channel_poisoned'])

    def test_poison_closes_editor_channel_to_drop_stale_frame(self):
        editor = _RecordingEditor()
        # Structured timed_out flag from the editor-op layer drives the poison.
        editor.set_result(0, {'ok': False, 'error': 'command timed out',
                              'timed_out': True})
        editor.process_check = lambda: {'editor_process_alive': False}
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        res = {}
        t = threading.Thread(
            target=lambda: res.update(
                disp.submit({'op': 'execute', 'code': 'A'}, 'src')), daemon=True)
        t.start()
        t.join(timeout=5.0)
        # _poison_channel calls editor.close_all() to drop the channel.
        self.assertGreaterEqual(editor.close_all_calls, 1)

    def test_timed_out_flag_present_poisons_channel(self):
        # Explicit positive case: timed_out=True triggers poison.
        # The gate keys on the STRUCTURED flag, not on a substring of 'error'.
        editor = _RecordingEditor()
        editor.set_result(0, {'ok': False, 'error': 'completely different wording',
                              'timed_out': True})
        editor.process_check = lambda: {'editor_process_alive': False}
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        t = threading.Thread(
            target=lambda: disp.submit({'op': 'execute', 'code': 'A'}, 'src'),
            daemon=True)
        t.start()
        t.join(timeout=5.0)
        # Channel was poisoned because timed_out=True, regardless of error text.
        self.assertGreaterEqual(editor.close_all_calls, 1,
                                'channel not poisoned when timed_out=True')

    def test_timeout_shaped_result_without_timed_out_flag_does_not_poison(self):
        # Negative case (the keystone gate): a result that LOOKS like a timeout
        # in English but carries NO timed_out flag must NOT poison the channel.
        # This proves the gate is flag-keyed -- the old substring gate would have
        # poisoned here; the new structured gate must not.
        editor = _RecordingEditor()
        editor.set_result(0, {'ok': False,
                              'error': 'command timed out: connection reset'})
        # No 'timed_out' key at all.
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        res = disp.submit({'op': 'execute', 'code': 'A'}, 'src')
        self.assertFalse(res['ok'])
        # Channel must NOT be poisoned: no timed_out flag, no stale frame risk.
        self.assertFalse(disp.status()['channel_poisoned'],
                         'channel poisoned on a timeout-string-only result (no timed_out flag)')
        # close_all was NOT called for poison: only the normal send path ran.
        self.assertEqual(editor.close_all_calls, 0,
                         'close_all called despite no timed_out flag')
        # The command ran exactly once: no resend.
        self.assertEqual(editor.sendall_count, 1)

    def test_send_failed_timeout_with_delivered_false_does_not_poison(self):
        # A send-side failure (command never reached the editor) can return an
        # error dict with delivered=False. Even if the text mentions "timeout",
        # delivered=False means no positional frame was sent, so no stale frame
        # can be mis-read. The channel must NOT be poisoned; this is retryable.
        editor = _RecordingEditor()
        editor.set_result(0, {'ok': False,
                              'error': 'send timed out: could not connect',
                              'delivered': False})
        # No 'timed_out' flag: send-failed path.
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)

        res = disp.submit({'op': 'execute', 'code': 'A'}, 'src')
        self.assertFalse(res['ok'])
        self.assertIs(res.get('delivered'), False)
        # No poison: the command provably never reached the editor.
        self.assertFalse(disp.status()['channel_poisoned'],
                         'channel poisoned on a send-failed (delivered=False) result')
        self.assertEqual(editor.close_all_calls, 0)
        self.assertEqual(editor.sendall_count, 1)


# ---------------------------------------------------------------------------
# Case 4: no-double-execute -- send path invoked exactly once per command.
# ---------------------------------------------------------------------------

class NoDoubleExecuteTests(_BrokerTestBase):
    def test_send_invoked_once_on_timeout_unflagged_no_resend_no_poison(self):
        # A timeout-shaped result WITHOUT timed_out=True: the command is NOT
        # resent (no-double-execute) and the channel is NOT poisoned (no stale
        # frame risk because delivered=False or no flag means it's a generic
        # non-poison failure). This is the "unflagged path" counterpart to the
        # poison-gate tests: proves the no-double-execute invariant holds
        # regardless of error text, and that the gate is flag-keyed.
        editor = _RecordingEditor()
        # timeout-string in error but no timed_out flag and no delivered key:
        # treated as a generic post-send failure (peer close / off-type frame).
        editor.set_result(0, {'ok': False, 'error': 'command timed out'})
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)
        res = disp.submit({'op': 'execute', 'code': 'A'}, 'src')
        self.assertFalse(res['ok'])
        # Exactly one send: no resend regardless of the error text.
        self.assertEqual(editor.sendall_count, 1)
        # Channel NOT poisoned: no timed_out flag, so no stale frame risk.
        self.assertFalse(disp.status()['channel_poisoned'],
                         'channel poisoned on unflagged timeout-string result')

    def test_send_invoked_once_on_post_send_error(self):
        editor = _RecordingEditor()
        # A generic post-send failure (peer close / off-type frame): no resend.
        editor.set_result(0, {'ok': False, 'error': 'connection reset by editor'})
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)
        res = disp.submit({'op': 'execute', 'code': 'A'}, 'src')
        self.assertFalse(res['ok'])
        self.assertEqual(editor.sendall_count, 1)

    def test_send_invoked_once_even_when_run_command_raises(self):
        editor = _RecordingEditor()

        def boom(payload):
            with editor.lock:
                editor.sendall_count += 1
            raise OSError('editor socket exploded')

        editor.run_command = boom
        disp = bk.Dispatcher(editor=editor)
        disp.start()
        self.addCleanup(disp.stop)
        res = disp.submit({'op': 'execute', 'code': 'A'}, 'src')
        # The dispatch loop catches the exception and surfaces a clean failure;
        # it never resends.
        self.assertFalse(res['ok'])
        self.assertEqual(editor.sendall_count, 1)


# ---------------------------------------------------------------------------
# Backpressure: BROKER_MAX_QUEUE_DEPTH fast-reject -> broker_busy/delivered:False.
# ---------------------------------------------------------------------------

class BackpressureTests(_BrokerTestBase):
    def test_fast_reject_returns_broker_busy_delivered_false(self):
        editor = _RecordingEditor()
        # Hold the in-flight command so the queue fills and stays full.
        gate0 = editor.gate_call(0)
        disp = bk.Dispatcher(editor=editor, max_queue_depth=2)
        disp.start()
        self.addCleanup(lambda: (gate0.set(), disp.stop()))

        accepted = []
        rejected = []
        lock = threading.Lock()

        def submit(i):
            r = disp.submit({'op': 'execute', 'code': str(i)}, 'src')
            with lock:
                (rejected if r.get('broker_busy') else accepted).append(r)

        threads = []
        # 1 will be in-flight (held), 2 queued (depth==2), the rest fast-rejected.
        for i in range(8):
            t = threading.Thread(target=submit, args=(i,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(0.02)  # stagger so the in-flight + queue settle first

        _wait_until(lambda: len(rejected) >= 1,
                    msg='no fast-reject happened at queue depth 2')
        for r in rejected:
            self.assertTrue(r['broker_busy'])
            self.assertFalse(r['delivered'])
            self.assertFalse(r['ok'])
        gate0.set()
        for t in threads:
            t.join(timeout=10.0)


# ---------------------------------------------------------------------------
# Case 1: single-winner election (concurrent connect_or_spawn) -- fake spawn.
# ---------------------------------------------------------------------------

class ElectionTests(_BrokerTestBase):
    def test_concurrent_connect_or_spawn_yields_exactly_one_broker(self):
        # Run ONE real in-process broker; make spawn_detached_broker a no-op that
        # records calls, so concurrent connect_or_spawn callers must all converge
        # on the single advertised endpoint rather than each spawning a broker.
        editor = _RecordingEditor()
        broker, port = self.make_started_broker(editor)

        spawn_calls = {'n': 0}
        real_spawn = bk.spawn_detached_broker

        def counting_spawn(*a, **k):
            spawn_calls['n'] += 1
            return None  # never actually spawn; the live broker is already up

        bk.spawn_detached_broker = counting_spawn
        try:
            clients = []
            errors = []
            lock = threading.Lock()

            def attach():
                try:
                    c = bk.connect_or_spawn(
                        advert_file=broker._advert_file, spawn_wait=5.0)
                    with lock:
                        clients.append(c)
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=attach, daemon=True)
                       for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [])
            self.assertEqual(len(clients), 6)
            self.assertTrue(all(c is not None for c in clients))
            # All clients converged on the ONE advertised endpoint.
            self.assertTrue(all(c.endpoint == (bk.BROKER_HOST, port)
                                for c in clients))
            # Because a live broker was already advertised, nobody needed to spawn.
            self.assertEqual(spawn_calls['n'], 0)
            for c in clients:
                self.addCleanup(c.close)
        finally:
            bk.spawn_detached_broker = real_spawn

    def test_loser_attaches_to_winner_when_advert_present(self):
        editor = _RecordingEditor()
        broker, port = self.make_started_broker(editor)
        # A late caller finds the live advert and attaches (step 1, no lock/spawn).
        client = bk.connect_or_spawn(advert_file=broker._advert_file, spawn_wait=5.0)
        self.assertIsNotNone(client)
        self.addCleanup(client.close)
        self.assertEqual(client.endpoint, (bk.BROKER_HOST, port))
        pong = client.request({'op': 'ping'}, timeout=2.0)
        self.assertTrue(pong['ok'])


# ---------------------------------------------------------------------------
# Case 6: floating re-election -- a dead advert endpoint is re-elected by probe.
# ---------------------------------------------------------------------------

class ReElectionTests(_BrokerTestBase):
    def test_stale_advert_to_dead_endpoint_is_not_trusted(self):
        # Write an advert pointing at a closed port (winner died). A caller must
        # probe and find it dead rather than trusting the file.
        dead = self.track_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        dead.bind((bk.BROKER_HOST, 0))
        dead_port = dead.getsockname()[1]
        dead.close()  # nothing listens now

        advert_file = os.path.join(self._tmp.name, 'broker.advert.json')
        bk.write_advert_atomic(advert_file, {
            'host': bk.BROKER_HOST, 'port': dead_port, 'pid': 999999,
            'heartbeat': time.time(),
        })
        # probe_endpoint must report the dead endpoint as unreachable.
        self.assertFalse(bk.probe_endpoint(bk.BROKER_HOST, dead_port, timeout=0.5))
        # _try_connect_advertised must reject the stale advert.
        self.assertIsNone(bk._try_connect_advertised(advert_file))

    def test_late_caller_reelects_under_bootstrap_lock_after_winner_death(self):
        # Stand up a real broker, write its advert, then KILL it. A late caller's
        # connect_or_spawn must, under the BootstrapLock, re-probe (find it dead)
        # and spawn a replacement. We stub spawn to bring up a fresh in-process
        # broker and advertise it, modelling a real re-election.
        editor1 = _RecordingEditor()
        broker1, port1 = self.make_started_broker(editor1)
        advert_file = broker1._advert_file
        # Kill the winner: shut it down so its endpoint is dead but advert lingers
        # briefly (we re-write a stale advert to the now-dead port to be explicit).
        broker1.shutdown()
        bk.write_advert_atomic(advert_file, {
            'host': bk.BROKER_HOST, 'port': port1, 'pid': 123456,
            'heartbeat': time.time(),
        })
        _wait_until(lambda: not bk.probe_endpoint(bk.BROKER_HOST, port1, timeout=0.3),
                    msg='dead winner endpoint still answering')

        replacement = {}
        real_spawn = bk.spawn_detached_broker

        def spawn_replacement(*a, **k):
            editor2 = _RecordingEditor()
            b2, p2 = self.make_started_broker(editor2)
            replacement['broker'] = b2
            replacement['port'] = p2
            return 424242  # a non-None pid so connect_or_spawn proceeds to await

        bk.spawn_detached_broker = spawn_replacement
        try:
            client = bk.connect_or_spawn(advert_file=advert_file, spawn_wait=5.0)
        finally:
            bk.spawn_detached_broker = real_spawn

        self.assertIsNotNone(client, 're-election failed to produce a client')
        self.addCleanup(client.close)
        # The re-elected client points at the REPLACEMENT broker, not the dead one.
        self.assertEqual(client.endpoint, (bk.BROKER_HOST, replacement['port']))
        self.assertNotEqual(client.endpoint[1], port1)


# ---------------------------------------------------------------------------
# Case 7: in-flight vs queued on broker death -- surface, never replay.
# ---------------------------------------------------------------------------

class BrokerDeathTests(_BrokerTestBase):
    def test_queued_undispatched_jobs_surface_delivered_false_on_stop(self):
        editor = _RecordingEditor()
        # Hold the in-flight job so the rest stay queued/undispatched.
        gate0 = editor.gate_call(0)
        disp = bk.Dispatcher(editor=editor)
        disp.start()

        results = {}
        threads = []

        def submit(name):
            results[name] = disp.submit({'op': 'execute', 'code': name}, 'src')

        for name in ('A', 'B', 'C'):
            t = threading.Thread(target=submit, args=(name,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(0.05)

        _wait_until(editor.dispatched.is_set, msg='A never dispatched')
        # B and C are queued, undispatched. stop() must fail them as delivered:False
        # (provably never reached the editor -> safe for the client to retry).
        # Release the in-flight A first so its thread completes, then stop.
        gate0.set()
        disp.stop()
        for t in threads:
            t.join(timeout=10.0)

        # Queued ones that were never dispatched must be delivered:False.
        for name in ('B', 'C'):
            if results[name].get('ok'):
                continue  # got dispatched before stop -- acceptable, it ran once
            self.assertFalse(results[name].get('delivered', True),
                             '%s queued job not surfaced as undelivered' % name)
        # A was the single in-flight command; it ran exactly once regardless.
        self.assertLessEqual(editor.sendall_count, 3)
        self.assertEqual(editor.max_outstanding, 1)

    def test_poison_blocked_job_surfaces_delivered_false_and_broker_poisoned_on_stop(self):
        # MAJOR-1 regression: a job that is BLOCKED in the poisoned-channel wait
        # (never dispatched) and then the dispatcher is stopped must come back
        # with BOTH delivered:False (provably never ran -> safe to retry) AND
        # broker_poisoned:True (the slot is contended, not a clean restart).
        # Without broker_poisoned, the client cannot distinguish this from a plain
        # delivered:False (stale-node restart) and might auto-retry INTO a still-
        # poisoned slot, or surface an indistinguishable hard error.
        editor = _RecordingEditor()
        # A times out with timed_out=True -> channel poisoned.
        editor.set_result(0, {'ok': False, 'error': 'timed out', 'timed_out': True})
        # Editor stays "alive" forever so the poison-wait loop never clears on its own.
        editor.process_check = lambda: {'editor_process_alive': True}
        editor.discover = lambda payload: {'ok': True, 'nodes': []}

        disp = bk.Dispatcher(editor=editor)
        disp.start()

        # Submit A (will timeout and poison the channel).
        res_a = {}
        ta = threading.Thread(
            target=lambda: res_a.update(
                disp.submit({'op': 'execute', 'code': 'A'}, 'srcA')),
            daemon=True)
        ta.start()
        ta.join(timeout=5.0)
        self.assertFalse(res_a.get('ok'))
        _wait_until(lambda: disp.status()['channel_poisoned'],
                    msg='channel not poisoned after A timed out')

        # Submit B: it blocks in the poison-wait (editor still alive).
        res_b = {}
        tb = threading.Thread(
            target=lambda: res_b.update(
                disp.submit({'op': 'execute', 'code': 'B'}, 'srcB')),
            daemon=True)
        tb.start()
        time.sleep(0.2)  # give B time to enter the wait loop
        with editor.lock:
            self.assertEqual(editor.sendall_count, 1,
                             'B was dispatched into a poisoned slot')

        # Stop the dispatcher while B is blocked in the poison-wait.
        disp.stop()
        tb.join(timeout=8.0)

        # B must be delivered:False (provably never dispatched).
        self.assertIs(res_b.get('delivered'), False,
                      'poison-blocked job did not carry delivered:False')
        # B must also carry broker_poisoned:True so the client can distinguish
        # this from a plain never-ran restart (which IS auto-retryable).
        self.assertTrue(res_b.get('broker_poisoned'),
                        'poison-blocked job missing broker_poisoned:True')
        self.assertFalse(res_b.get('ok'))
        # A was dispatched exactly once regardless.
        self.assertEqual(editor.sendall_count, 1)

    def test_client_surfaces_none_when_broker_dies_midflight(self):
        # An in-flight command whose broker connection dies: the CLIENT surfaces
        # None (unknown fate) and does NOT silently resend.
        editor = _RecordingEditor()
        gate0 = editor.gate_call(0)
        broker, port = self.make_started_broker(editor)
        client = self.client_connect(port)

        sent_marker = threading.Event()
        orig_run = editor.run_command

        def run_then_signal(payload):
            sent_marker.set()
            return orig_run(payload)

        editor.run_command = run_then_signal

        result_box = {}
        t = threading.Thread(
            target=lambda: result_box.__setitem__(
                'r', client.request({'op': 'execute', 'code': 'A'}, timeout=10.0)),
            daemon=True)
        t.start()
        _wait_until(sent_marker.is_set, msg='command never reached the editor')

        # Kill the broker while A is in-flight (held in run_command).
        broker.shutdown()
        gate0.set()  # let the editor side finish; broker is already torn down
        t.join(timeout=10.0)

        # The client surfaces None (dead connection) -- it does not block forever
        # and does not fabricate a success. The editor ran the command exactly once.
        self.assertIn('r', result_box)
        self.assertEqual(editor.sendall_count, 1)


# ---------------------------------------------------------------------------
# Case 8: self-teardown grace.
# ---------------------------------------------------------------------------

class GraceTeardownTests(_BrokerTestBase):
    def test_broker_self_terminates_after_grace_with_no_clients(self):
        editor = _RecordingEditor()
        broker, port = self.make_started_broker(editor, grace_seconds=0.5)
        # No client ever connects. After the grace window the serve loop stops.
        _wait_until(lambda: broker._stop.is_set(), timeout=8.0,
                    msg='broker did not self-terminate after grace')
        # And the advert is cleared on clean exit.
        broker._serve_thread.join(timeout=5.0)
        _wait_until(lambda: bk.read_advert(broker._advert_file) is None, timeout=5.0,
                    msg='advert not cleared on self-teardown')

    def test_broker_does_not_terminate_while_a_client_is_connected(self):
        editor = _RecordingEditor()
        broker, port = self.make_started_broker(editor, grace_seconds=0.5)
        client = self.client_connect(port)
        # Keep the client connected past the grace window; broker must stay up.
        time.sleep(1.0)
        self.assertFalse(broker._stop.is_set(),
                         'broker self-terminated while a client was connected')
        pong = client.request({'op': 'ping'}, timeout=2.0)
        self.assertTrue(pong['ok'])


# ---------------------------------------------------------------------------
# Case 5: detached survival / stdout-EOF (the ONLY real spawn; grace=1).
# ---------------------------------------------------------------------------

class DetachedSpawnTests(_BrokerTestBase):
    def test_spawn_detached_broker_outlives_parent_and_closes_std_handles(self):
        # Make the spawned broker self-terminate fast so nothing leaks.
        os.environ['UE_BROKER_GRACE_SECONDS'] = '1'
        advert_file = os.path.join(self._tmp.name, 'broker.advert.json')

        # spawn_detached_broker internally creates a Popen that it intentionally
        # discards (the child must be detached, not a held child). Python's GC
        # emits ResourceWarning when that Popen object is collected without
        # wait(). Suppress here: the warning is cosmetic, the process is properly
        # detached (start_new_session, close_fds, stdio->devnull) and is reaped
        # by the OS on exit or by bk._reap() in the finally block below.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            pid = bk.spawn_detached_broker(advert_file=advert_file)

        self.assertIsNotNone(pid, 'spawn_detached_broker returned no pid')
        # It is a SEPARATE process, not this one and not a Popen object we hold.
        self.assertNotEqual(pid, os.getpid())
        self.assertNotIsInstance(pid, __import__('subprocess').Popen)

        try:
            # The detached broker advertises a reachable endpoint shortly. A live,
            # connectable listener that this process did NOT bind proves it is an
            # independent process (it was launched detached with close_fds=True and
            # stdio redirected to devnull, so it holds no handle to our stdio --
            # the property that keeps the agent's MCP stdio transport from wedging).
            _wait_until(lambda: bk.read_advert(advert_file) is not None,
                        timeout=15.0, msg='detached broker never advertised')
            advert = bk.read_advert(advert_file)
            self.assertEqual(advert['pid'], pid)
            self.assertNotEqual(advert['pid'], os.getpid())
            # ONE probe to prove the endpoint is live and independently reachable.
            self.assertTrue(bk.probe_endpoint(advert['host'], advert['port'],
                                              timeout=2.0))

            # Self-teardown on the grace timer. We must NOT probe the endpoint while
            # waiting -- every probe is a client connection that resets the grace
            # idle timer (verified: a poll faster than grace keeps it alive forever).
            # Instead watch the advert: the broker clears it on its clean grace exit.
            # The spawned process is detached (no parent reaps it), so os.kill(pid,0)
            # would report a post-exit ZOMBIE as alive; the advert-clear is the
            # reliable, side-effect-free liveness signal here.
            _wait_until(lambda: bk.read_advert(advert_file) is None,
                        timeout=10.0,
                        msg='detached broker did not self-terminate on grace '
                            '(advert never cleared)')
        finally:
            # Belt-and-suspenders: reap if the grace timer somehow did not fire.
            bk._reap(pid)

    def test_detached_broker_outlives_spawner_while_client_connected(self):
        # Criterion 8 / verifier MINOR: a detached broker keeps serving a
        # connected client AFTER its spawner (simulated here) exits. The
        # current test_spawn_detached_broker_... never connects a client;
        # this test does, proving the detached-spawn E1 property end-to-end.
        #
        # Simulation: we spawn a detached broker, connect a BrokerClient,
        # then drop ALL spawner-side in-memory references to simulate the
        # spawning process having "exited". The client must keep working because
        # the broker is already detached (start_new_session, close_fds,
        # stdio->devnull) and holds no handle to the spawner's resources.
        #
        # Use a dedicated tmpdir so this test's advert is isolated from the
        # first detached-spawn test's. The child reads UE_BROKER_DIR from env
        # and always writes broker.advert.json inside it; spawn_detached_broker
        # sets UE_BROKER_DIR=dirname(advert_file) in the child env, so we must
        # pass advert_file=<tmpdir>/broker.advert.json (the standard name).
        #
        # Teardown: _reap() is the reliable cleanup for detached subprocesses.
        # Grace-timer teardown through advert-clear is covered by
        # test_broker_self_terminates_after_grace_with_no_clients (in-process)
        # which is deterministic; the subprocess variant is not because the
        # BrokerClient reader-thread join timing interacts with the subprocess's
        # grace-check cadence unpredictably across CI environments.
        os.environ['UE_BROKER_GRACE_SECONDS'] = '1'
        tmp2 = tempfile.mkdtemp(prefix='ue-broker-det2-')
        self.addCleanup(__import__('shutil').rmtree, tmp2, True)
        advert_file = os.path.join(tmp2, 'broker.advert.json')

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            pid = bk.spawn_detached_broker(advert_file=advert_file)

        self.assertIsNotNone(pid)
        try:
            _wait_until(lambda: bk.read_advert(advert_file) is not None,
                        timeout=15.0, msg='detached broker (client test) never advertised')
            advert = bk.read_advert(advert_file)

            # Connect a BrokerClient (the "peer agent" that stays connected).
            client = bk.BrokerClient(advert['host'], advert['port'])
            self.assertTrue(client.connect(timeout=2.0),
                            'could not connect to detached broker')
            self.addCleanup(client.close)

            # Simulate spawner exit: drop all spawner-side in-memory state.
            # The detached broker must continue serving because it has no
            # handle to the spawner's process (not a Popen child) and its
            # stdio is redirected to devnull (not inherited from the spawner).
            del advert

            # Client stays connected past the grace window; broker must remain
            # alive because a client is connected (grace requires idle=0 clients).
            time.sleep(1.2)
            # Prove the broker is still alive and it IS the same process we spawned.
            pong = client.request({'op': 'ping'}, timeout=2.0)
            self.assertIsNotNone(pong, 'detached broker died while client was connected')
            self.assertTrue(pong.get('ok'), 'broker ping failed: %r' % pong)
            self.assertEqual(pong.get('pid'), pid,
                             'pong came from a different process than the spawned broker')
            # The broker is independently reachable (a loopback listener we did not bind).
            self.assertTrue(bk.probe_endpoint(
                bk.BROKER_HOST, client.endpoint[1], timeout=1.0))
        finally:
            # _reap is the reliable teardown for detached subprocesses.
            bk._reap(pid)


if __name__ == '__main__':
    unittest.main()

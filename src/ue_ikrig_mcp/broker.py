"""Auto-spawned, detached, result-frame-gated shared editor-command broker.

Why this module exists
----------------------
On native Windows each MCP agent spawns its own ``ue-ikrig-mcp`` stdio server
(unchanged launch UX). But the Unreal Editor exposes exactly ONE command slot
per node: the remote-execution protocol frame carries NO request id
(``make_message`` in ``ue_connection``), so a command's result frame is matched
**positionally** -- the next ``command_result`` frame on the channel. If two
agents open their own command channels concurrently, last-writer-wins eviction
(``listen(1)``) produces a 10053 steal-storm AND, worse, lets agent B's mutation
race agent A's in-flight command while A's late result frame is mis-attributed to
B. A bare per-process mutex cannot fix this: a mutex releases on holder *process
death*, freeing a peer to mutate while the editor is still executing the dead
holder's command.

This broker removes that contention by owning the single editor command channel
for the whole machine and serializing every agent through **result-frame-gated
dispatch**: it dispatches the next queued request ONLY after observing the prior
request's result frame, or after the editor is *proven* dead/restarted -- NEVER
on a bare timer and NEVER on a client disconnect. This is the one property that
closes the killed-mid-mutation overlapping window (see the plan
``.omc/plans/multi-process-command-slot-arbitration.md``, Phase E2).

Security / trust posture (v1, decided)
--------------------------------------
The broker is a persistent local listener through which ANY local process can run
arbitrary editor Python (``exec_mode``). v1 uses **loopback TCP** (127.0.0.1) and
therefore **explicitly accepts loopback-only / single-user trust with NO authz**
as a conscious decision. A named pipe with a current-user ACL is the hardening
follow-up. Do not bind this broker to a non-loopback address.

Portability
-----------
The broker TARGETS native Windows (the detached-spawn flags below are Windows
specific) but MUST import and run its dispatch + election logic on Linux/WSL so it
can be unit-tested with real loopback sockets and a fake editor. Every
Windows-only ``subprocess`` flag is read via ``getattr(..., 0)`` so importing this
module never requires pywin32 or Windows-only constants. The dispatcher takes its
editor-facing functions by injection (see ``EditorOps``) so tests drive a fake
editor with zero real UE.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Tunables (env-overridable; mirror ue_connection's _*_env helpers locally so
# this module has no import-time dependency on ue_connection).
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Loopback host for the broker listener and for client connects. Loopback only.
BROKER_HOST = '127.0.0.1'
# Max queued waiters (excluding the in-flight one) before fast-reject. Bounds the
# head-of-line pile-up that serializing the editor slot through one broker creates.
BROKER_MAX_QUEUE_DEPTH = max(1, _int_env('UE_BROKER_MAX_QUEUE_DEPTH', 64))
# Seconds with zero connected clients before the broker self-terminates.
BROKER_GRACE_SECONDS = max(1.0, _float_env('UE_BROKER_GRACE_SECONDS', 30.0))
# How long a spawner blocks waiting for a freshly spawned broker to advertise a
# reachable endpoint before it reaps the orphan it spawned.
BROKER_SPAWN_WAIT_SECONDS = max(1.0, _float_env('UE_BROKER_SPAWN_WAIT_SECONDS', 15.0))
# Cadence at which the broker rewrites its advert with a fresh `heartbeat`
# timestamp. NOTE: election/liveness does NOT decide staleness from the heartbeat
# age — it PROBES the endpoint directly (a real connect; see probe_endpoint /
# _try_connect_advertised). The heartbeat is purely an observability signal
# (surfaced in get_status's advert section) so an operator can see the broker is
# alive and refreshing; it is not load-bearing for correctness.
BROKER_HEARTBEAT_SECONDS = max(0.5, _float_env('UE_BROKER_HEARTBEAT_SECONDS', 5.0))
# Upper bound on a single blocking editor op the dispatch thread can be inside
# when stop() is requested. editor_process_check runs `tasklist` with a 10s
# timeout (ue_connection.editor_process_check), the longest such op; stop()'s
# join is sized to exceed it so a dispatch thread parked in _wait_for_editor_clear
# is still reliably joined rather than orphaned.
_EDITOR_OP_MAX_SECONDS = 12.0


# ---------------------------------------------------------------------------
# Wire framing (broker <-> client). Length-prefixed JSON: a 4-byte big-endian
# unsigned length followed by that many UTF-8 JSON bytes. This is binary-safe and
# self-framing (unlike the bridge's newline-delimited stdio framing, which the
# editor-protocol JSON could in principle break). The CLIENT correlation id
# carried in each request/response (``id``) is the broker<->client id and is
# DISTINCT from the editor command, which has no id and is matched positionally.
# ---------------------------------------------------------------------------

_LEN = struct.Struct('>I')
_MAX_FRAME = 64 * 1024 * 1024  # guard against a bogus length wedging a reader


class FrameError(OSError):
    """A frame could not be read/written (peer closed or protocol violation)."""


def send_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    """Send one length-prefixed JSON frame. Raises FrameError on a dead peer."""
    body = json.dumps(obj, ensure_ascii=True).encode('utf-8')
    try:
        sock.sendall(_LEN.pack(len(body)) + body)
    except OSError as exc:
        raise FrameError('frame send failed: %s' % (exc,))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        part = sock.recv(remaining)
        if not part:
            raise FrameError('peer closed mid-frame')
        chunks.append(part)
        remaining -= len(part)
    return b''.join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    """Receive one length-prefixed JSON frame. Raises FrameError on close."""
    header = _recv_exact(sock, _LEN.size)
    (length,) = _LEN.unpack(header)
    if length <= 0 or length > _MAX_FRAME:
        raise FrameError('bogus frame length %d' % (length,))
    body = _recv_exact(sock, length)
    try:
        return json.loads(body.decode('utf-8'))
    except ValueError as exc:
        raise FrameError('bad frame json: %s' % (exc,))


# ---------------------------------------------------------------------------
# Advert file: the broker self-writes (endpoint + pid + heartbeat) ATOMICALLY as
# its first post-bind action; the spawner blocks on its appearance and reaps the
# orphan on timeout; re-election PROBES the endpoint, not just this file.
# ---------------------------------------------------------------------------

def advert_dir() -> str:
    """Stable machine-shared directory for the advert + bootstrap lock.

    Native Windows: %ProgramData% (machine-wide, single namespace). Elsewhere
    (CI on Linux/WSL): the system temp dir. Always single-namespace per host.
    """
    override = os.environ.get('UE_BROKER_DIR')
    if override:
        return override
    program_data = os.environ.get('PROGRAMDATA')
    if program_data and os.path.isdir(program_data):
        base = os.path.join(program_data, 'ue-ikrig-mcp')
    else:
        import tempfile
        base = os.path.join(tempfile.gettempdir(), 'ue-ikrig-mcp')
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def advert_path() -> str:
    return os.path.join(advert_dir(), 'broker.advert.json')


def bootstrap_lock_path() -> str:
    return os.path.join(advert_dir(), 'broker.bootstrap.lock')


def write_advert_atomic(path: str, advert: dict[str, Any]) -> None:
    """Write the advert via temp-file + os.replace (Windows-safe atomic swap).

    os.rename over an existing file historically fails on Windows; os.replace is
    the atomic, replace-over-existing primitive. A reader therefore never sees a
    half-written advert.
    """
    tmp = '%s.%s.tmp' % (path, uuid.uuid4().hex)
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(advert, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_advert(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    path = path or advert_path()
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def clear_advert(path: Optional[str] = None) -> None:
    path = path or advert_path()
    try:
        os.unlink(path)
    except OSError:
        pass


def probe_endpoint(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True iff a TCP connect to (host, port) succeeds (broker alive).

    Re-election PROBES the endpoint -- a real connect attempt -- rather than
    trusting the advert file, so a running-but-unadvertised broker is found and
    not duplicated.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as probe:
            probe.settimeout(timeout)
            try:
                send_frame(probe, {'op': 'ping', 'id': uuid.uuid4().hex})
                recv_frame(probe)
            except (FrameError, OSError):
                # It accepted the TCP connection, so something is listening; even
                # if the ping races teardown, treat "connectable" as alive enough
                # to avoid a duplicate spawn. The dispatcher's own death detection
                # handles a truly-dead endpoint on the next op.
                return True
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Bootstrap lock: a single-namespace, machine-shared, exclusive file lock held
# ONLY during the spawn/re-probe critical section. Exactly one spawner wins.
# ---------------------------------------------------------------------------

class BootstrapLock:
    """Cross-process exclusive lock over a stable machine-shared path.

    Uses msvcrt on Windows and fcntl elsewhere; both are advisory-but-exclusive
    for our single-namespace use. Held only briefly around the spawn decision.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path or bootstrap_lock_path()
        self._handle = None

    def acquire(self, timeout: float = 10.0) -> bool:
        deadline = time.time() + max(0.0, timeout)
        handle = open(self._path, 'a+b')
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return True
            except OSError:
                if time.time() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                handle.close()
            except OSError:
                pass
            self._handle = None

    def __enter__(self) -> 'BootstrapLock':
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# EditorOps: the broker's editor-facing surface, injected so the dispatcher is
# testable against a fake editor with zero real UE. The real implementation
# delegates to the verbatim no-double-execute discipline in ue_connection.
#
# CONTRACT (the single-outstanding-command invariant lives here):
#   - run_command(payload) sends ONE command frame and blocks reading its result
#     frame to completion. It MUST raise on send failure / peer close / timeout /
#     off-type frame (it never silently advances). The dispatcher relies on this:
#     it holds at most one command outstanding and never dispatches the next until
#     run_command returns or raises.
#   - process_check() -> {'editor_process_alive': True|False|None}.
#   - reconnect_after_restart() -> bool (a fresh handshake proved a restart).
# ---------------------------------------------------------------------------

class EditorDeadError(OSError):
    """The editor channel is proven dead (peer close / connect refused)."""


class EditorTimeout(OSError):
    """The command exceeded its per-command timeout; the editor may still be
    running it. NEVER auto-resend (mirrors daemon_execute :449-453)."""


class EditorOps:
    """Default editor-facing ops backed by ue_connection's reused primitives.

    The editor-protocol functions (``daemon_execute`` with its verbatim
    no-double-execute discipline, ``discover``, ``editor_process_check``,
    ``close_all_channels``) and their shared ``CHANNELS``/``SOURCE_ID`` live
    INSIDE the embedded ``_EDITOR_PROTOCOL_SCRIPT`` string in ue_connection,
    NOT as module-level attributes of ue_connection. The broker runs IN-PROCESS,
    so it execs that same script body once into a private namespace and calls
    into it — reusing
    the exact verbatim logic (and the structured ``timed_out`` flag daemon_execute
    stamps on a may-still-be-running timeout) with zero duplication, while owning
    ONE CHANNELS/SOURCE_ID for the whole broker.

    Lazy + injected: the dispatcher only ever touches the editor through one of
    these calls, on its own thread, one command at a time. Tests inject a fake
    editor instead, so this default path is never exercised in CI.
    """

    def __init__(self) -> None:
        self._ns: Optional[dict[str, Any]] = None

    def _namespace(self) -> dict[str, Any]:
        if self._ns is None:
            from . import ue_connection  # lazy: keep broker import light
            ns: dict[str, Any] = {'__name__': 'ue_ikrig_mcp_broker_editor_ops'}
            # Exec the VERBATIM editor-protocol body once. This defines
            # daemon_execute / discover / editor_process_check / close_all_channels
            # and the single shared CHANNELS + SOURCE_ID this broker owns.
            exec(ue_connection._EDITOR_PROTOCOL_SCRIPT, ns)
            self._ns = ns
        return self._ns

    def run_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reuse daemon_execute VERBATIM: send-then-blocking-recv with the exact
        no-double-execute retry discipline. The result it returns is the
        positionally-matched result frame; it returns an error dict rather than
        ever silently resending after a send.

        Contract for the dispatcher's gate: daemon_execute itself stamps a
        STRUCTURED ``timed_out: True`` on the may-still-be-running per-command
        timeout. The dispatcher keys its poison decision on that flag at the
        SOURCE — no substring of the human-readable error is matched anywhere on
        the critical path. A send-failure timeout (which carries delivered=False
        instead) is deliberately NOT flagged: it provably never ran and must
        advance/retry, not poison.
        """
        # daemon_execute owns CHANNELS/SOURCE_ID and the retry discipline; the
        # dispatcher guarantees one-at-a-time entry, satisfying the
        # single-outstanding-command invariant for the positional result match.
        return self._namespace()['daemon_execute'](payload)

    def discover(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._namespace()['discover'](payload)

    def process_check(self) -> dict[str, Any]:
        return self._namespace()['editor_process_check']()

    def close_all(self) -> None:
        try:
            self._namespace()['close_all_channels']()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dispatcher: the correctness core (Phase E2). ONE thread owns the editor
# channel. At most one editor command is outstanding at any instant. The gate
# advances only on a result frame or PROVEN editor death -- never on a timer,
# never on a client disconnect.
# ---------------------------------------------------------------------------

class _Job:
    __slots__ = ('request', 'source_id', 'event', 'result', 'dispatched')

    def __init__(self, request: dict[str, Any], source_id: str):
        self.request = request
        self.source_id = source_id
        self.event = threading.Event()
        self.result: Optional[dict[str, Any]] = None
        # dispatched=True the instant the editor command is sent; a job that is
        # provably NOT dispatched may be safely retried by its client, a
        # dispatched one may NOT (no-double-execute).
        self.dispatched = False

    def complete(self, result: dict[str, Any]) -> None:
        self.result = result
        self.event.set()


class Dispatcher:
    """Single-editor-channel, result-frame-gated, multi-client command serializer.

    Threading model: any number of client-connection threads call ``submit()``,
    which enqueues a ``_Job`` and blocks on its event. One dedicated dispatch
    thread pulls jobs FIFO and runs them against the editor ONE AT A TIME via
    ``EditorOps.run_command``. The dispatch thread is the ONLY thread that touches
    the editor, which is what makes the positional result-frame match well-defined.
    """

    def __init__(
        self,
        editor: Optional[EditorOps] = None,
        max_queue_depth: int = BROKER_MAX_QUEUE_DEPTH,
    ):
        self._editor = editor or EditorOps()
        self._max_queue_depth = max(1, max_queue_depth)
        self._queue: list[_Job] = []
        self._cond = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        # Observability (Phase E4): surfaced through Broker.status().
        self._current_holder: Optional[str] = None
        self._last_result_frame_observed: bool = False
        self._channel_poisoned: bool = False
        self._poison_reason: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name='ue-broker-dispatch', daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            # Fail every queued (never-dispatched) job cleanly: they provably
            # never reached the editor, so the client may retry against a fresh
            # broker. The in-flight one (if any) is owned by the dispatch loop.
            pending = list(self._queue)
            self._queue.clear()
            self._cond.notify_all()
        for job in pending:
            job.complete({
                'ok': False,
                'error': 'broker shutting down',
                'delivered': False,  # provably never dispatched -> retryable
            })
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Sized to exceed the longest single blocking editor op so a dispatch
            # thread parked in _wait_for_editor_clear (which checks _stop between
            # every sub-step and wakes immediately on the notify above) is joined
            # rather than orphaned.
            thread.join(timeout=_EDITOR_OP_MAX_SECONDS + 1.0)
        self._editor.close_all()

    # -- client entry point -----------------------------------------------

    def submit(self, request: dict[str, Any], source_id: str) -> dict[str, Any]:
        """Enqueue one request and block until its result (or a fast-reject).

        Backpressure: beyond max_queue_depth queued waiters, fast-reject with a
        retryable "broker busy" error so waiters cannot pile up unbounded.
        """
        job = _Job(request, source_id)
        with self._cond:
            if self._stop:
                return {'ok': False, 'error': 'broker shutting down', 'delivered': False}
            if len(self._queue) >= self._max_queue_depth:
                return {
                    'ok': False,
                    'error': 'broker busy (queue depth %d reached), retry'
                             % (self._max_queue_depth,),
                    'delivered': False,  # never dispatched -> safe to retry
                    'broker_busy': True,
                }
            self._queue.append(job)
            self._cond.notify_all()
        job.event.wait()
        return job.result or {'ok': False, 'error': 'no result', 'delivered': False}

    # -- dispatch loop -----------------------------------------------------

    def _next_job(self) -> Optional[_Job]:
        with self._cond:
            while not self._queue and not self._stop:
                self._cond.wait(timeout=0.5)
            if self._stop:
                return None
            return self._queue.pop(0)

    def _run(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                return
            try:
                job.complete(self._dispatch_one(job))
            except Exception as exc:  # never let the dispatch thread die silently
                job.complete({
                    'ok': False,
                    'error': 'broker dispatch error: %s: %s' % (type(exc).__name__, exc),
                })

    def _dispatch_one(self, job: _Job) -> dict[str, Any]:
        """Run ONE job against the editor, honoring the result-frame gate.

        The gate is implicit in this being the only thread and in run_command
        blocking until the result frame (or a raise). We never return to
        ``_run`` (which dequeues the next job) until this job's editor command
        has either produced a result frame or been resolved as proven-dead.
        """
        op = job.request.get('op')

        # Non-editor-mutating ops do not consume the command slot positionally,
        # but we still run them on this one thread to keep all editor access
        # serialized (discover/process_check open their own short-lived sockets).
        if op == 'discover':
            return self._editor.discover(job.request)
        if op == 'process_check':
            return self._editor.process_check()

        if op != 'execute':
            return {'ok': False, 'error': 'unsupported broker op: %r' % (op,)}

        # If the channel was poisoned by a prior per-command timeout, BLOCK new
        # dispatch until the editor is PROVEN gone or a restart is proven. This
        # is the head-of-line consequence the plan makes explicit (Principle 2):
        # advancing here while the editor is still alive would race this mutation
        # against the prior in-flight command and positionally mis-read its late
        # frame. We never advance on a bare timer.
        if self._channel_poisoned:
            cleared = self._wait_for_editor_clear()
            if not cleared:
                return {
                    'ok': False,
                    'error': 'broker channel poisoned (prior command timed out on '
                             'a still-alive editor); blocked until the editor is '
                             'killed or restarts. Underlying: %s'
                             % (self._poison_reason or 'unknown'),
                    # This job was NEVER dispatched (we return before
                    # job.dispatched is set), so it provably never reached the
                    # editor: delivered=False, consistent with the no-double-
                    # execute axis (the ChannelNotDelivered/never-ran case). The
                    # SEPARATE broker_poisoned flag tells the client the slot is
                    # contended, so it can choose to surface rather than blindly
                    # resend; delivered=False alone does not force a resend.
                    'delivered': False,
                    'broker_poisoned': True,
                }

        with self._cond:
            self._current_holder = job.source_id
            self._last_result_frame_observed = False

        job.dispatched = True
        result = self._editor.run_command(job.request)

        # daemon_execute returns a dict; it never raises for the normal paths.
        # Classify the outcome to drive the gate (keyed on STRUCTURED flags, not
        # human-readable substrings):
        #   - ok==True              -> a result frame WAS observed; advance.
        #   - timed_out==True       -> per-command timeout while the editor may
        #                              still be running it: poison the channel and
        #                              block subsequent dispatch (do NOT advance on
        #                              the timer); surface no-resend to this client.
        #   - delivered==False      -> provably never reached the editor; advance
        #                              (retryable by the client; no positional
        #                              frame is outstanding).
        #   - any other error       -> post-send failure (peer close / off-type);
        #                              the channel is dropped by daemon_execute and
        #                              no frame is outstanding, so advance.
        with self._cond:
            self._last_result_frame_observed = bool(result.get('ok'))

        if not result.get('ok') and result.get('timed_out'):
            # Per-command timeout while the editor may still be executing the
            # command. Poison the channel: the prior command's late, id-less
            # result frame could be positionally mis-read by the NEXT dispatch,
            # so block all new dispatch until proven-gone/restart.
            self._poison_channel(result.get('error') or 'command timed out')

        return result

    # -- poison / proven-death gate ---------------------------------------

    def _poison_channel(self, reason: str) -> None:
        with self._cond:
            self._channel_poisoned = True
            self._poison_reason = str(reason)
            self._current_holder = None
        # Drop the editor channel so no stale positional frame survives.
        self._editor.close_all()

    def _wait_for_editor_clear(self) -> bool:
        """Block (the dispatch thread, so all dispatch is blocked) until the
        editor that held the timed-out command is PROVEN gone or a fresh
        handshake proves a restart. Returns True when the gate may advance,
        False when stop() was requested.

        This is the deliberate head-of-line block: a genuinely-hung-but-ALIVE
        editor blocks every agent until it is killed/restarts. That is the
        correct tradeoff under no-double-execute; the alternative (advancing on
        the timer) silently double-executes and corrupts results.

        stop() honoring: this loop checks _stop BEFORE every blocking sub-step
        (process_check / discover / inter-iteration wait) and bails immediately
        when it is set. A blocking editor op already in flight cannot be
        pre-empted, so the worst-case latency for stop() to take effect here is
        ONE such op; that is why stop()'s join timeout is sized to exceed the
        longest single editor op (see Dispatcher.stop / _EDITOR_OP_MAX_SECONDS).
        """
        while True:
            if self._stopping():
                return False
            check = self._editor.process_check()
            if self._stopping():
                return False
            alive = check.get('editor_process_alive')
            if alive is False:
                # Editor proven gone: the prior command can no longer produce a
                # frame on a live channel. Clear the poison and let dispatch
                # resume (daemon_execute will reopen a fresh channel on demand).
                self._clear_poison()
                return True
            # alive is True or None (check impossible). Try a fresh handshake to
            # prove a restart -- discover() reaches a *currently live* editor; if
            # it answers, the slot is usable again. We do NOT resend the prior
            # command here; only future jobs proceed.
            discovery = self._editor.discover({'op': 'discover', 'timeout': 1.0})
            if self._stopping():
                return False
            if discovery.get('ok') and discovery.get('nodes'):
                # A live editor answered discovery; treat the channel as usable.
                # (The truly-hung case stays poisoned because discover would not
                # answer while the game thread is wedged.) Clear and resume.
                self._clear_poison()
                return True
            # Still alive-but-silent (hung). Keep blocking; recheck shortly.
            # _cond.wait wakes immediately on stop()'s notify_all, so this
            # inter-iteration wait is fully interruptible.
            with self._cond:
                if self._stop:
                    return False
                self._cond.wait(timeout=0.5)

    def _stopping(self) -> bool:
        with self._cond:
            return self._stop

    def _clear_poison(self) -> None:
        with self._cond:
            self._channel_poisoned = False
            self._poison_reason = None

    # -- observability -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                'queue_depth': len(self._queue),
                'current_holder': self._current_holder,
                'result_frame_observed': self._last_result_frame_observed,
                'channel_poisoned': self._channel_poisoned,
                'poison_reason': self._poison_reason,
                'max_queue_depth': self._max_queue_depth,
            }


# ---------------------------------------------------------------------------
# Broker server: accepts loopback-TCP clients, tracks connected-client count for
# the grace-teardown, rewrites its advert heartbeat, and routes each request
# through the single Dispatcher.
# ---------------------------------------------------------------------------

class Broker:
    """The detached broker process's in-process server object.

    Responsibilities: bind a loopback listener; ATOMICALLY self-write the advert
    as the FIRST post-bind action (so an advert never exists without a live
    listener behind it); accept clients; serialize their requests through the
    Dispatcher; self-terminate after the last client disconnects + a grace
    window, clearing the advert on clean exit.
    """

    def __init__(
        self,
        host: str = BROKER_HOST,
        port: int = 0,
        editor: Optional[EditorOps] = None,
        grace_seconds: float = BROKER_GRACE_SECONDS,
        advert_file: Optional[str] = None,
        max_queue_depth: int = BROKER_MAX_QUEUE_DEPTH,
    ):
        self._host = host
        self._port = port
        self._advert_file = advert_file or advert_path()
        self._grace_seconds = max(0.0, grace_seconds)
        self._dispatcher = Dispatcher(editor=editor, max_queue_depth=max_queue_depth)
        self._listener: Optional[socket.socket] = None
        self._bound_port: Optional[int] = None
        self._client_count = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_client_at = time.time()
        self._client_threads: list[threading.Thread] = []
        self._started_at = time.time()
        self._heartbeat_thread: Optional[threading.Thread] = None

    @property
    def port(self) -> Optional[int]:
        return self._bound_port

    @property
    def endpoint(self) -> Optional[tuple[str, int]]:
        return (self._host, self._bound_port) if self._bound_port is not None else None

    # -- bind + advertise --------------------------------------------------

    def bind(self) -> tuple[str, int]:
        """Bind the loopback listener. Call before serve()."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._host, self._port))
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self._bound_port = int(listener.getsockname()[1])
        return (self._host, self._bound_port)

    def _advert(self) -> dict[str, Any]:
        return {
            'host': self._host,
            'port': self._bound_port,
            'pid': os.getpid(),
            'heartbeat': time.time(),
            'started_at': self._started_at,
        }

    def write_advert(self) -> None:
        """ATOMIC self-write of the advert -- the broker's FIRST post-bind action.

        Done by the BROKER (not the spawner) so an advert never exists without a
        live listener behind it; atomic via os.replace so readers never see a
        half-written advert. This closes the winner-death-mid-spawn TOCTOU.
        """
        write_advert_atomic(self._advert_file, self._advert())

    # -- serve loop --------------------------------------------------------

    def serve(self) -> None:
        if self._listener is None:
            self.bind()
        # FIRST post-bind action: advertise atomically.
        self.write_advert()
        self._dispatcher.start()
        heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread = heartbeat
        heartbeat.start()
        try:
            while not self._stop.is_set():
                try:
                    conn, _addr = self._listener.accept()
                except socket.timeout:
                    self._maybe_self_terminate()
                    continue
                except OSError:
                    break
                conn.setblocking(True)
                thread = threading.Thread(
                    target=self._serve_client, args=(conn,), daemon=True
                )
                with self._lock:
                    self._client_threads.append(thread)
                thread.start()
        finally:
            self.shutdown()

    def _heartbeat_loop(self) -> None:
        """Periodically rewrite the advert with a fresh `heartbeat` timestamp.

        Observability only: liveness/election probe the endpoint directly, so the
        heartbeat age is not consulted for correctness (see BROKER_HEARTBEAT_SECONDS).

        Stops on _stop and never writes after it: shutdown() clears the advert,
        and a heartbeat write racing past that clear would RESURRECT a dead
        broker's advert. shutdown() joins this thread BEFORE clearing, and this
        loop re-checks _stop right before each write, so the clear is final.
        """
        while not self._stop.is_set():
            if self._stop.is_set():
                return
            try:
                self.write_advert()
            except OSError:
                pass
            self._stop.wait(timeout=BROKER_HEARTBEAT_SECONDS)

    def _maybe_self_terminate(self) -> None:
        with self._lock:
            idle = self._client_count == 0
            since = time.time() - self._last_client_at
        if idle and since >= self._grace_seconds:
            self._stop.set()

    # -- per-client request loop ------------------------------------------

    def _serve_client(self, conn: socket.socket) -> None:
        with self._lock:
            self._client_count += 1
        source_id = uuid.uuid4().hex
        try:
            while not self._stop.is_set():
                try:
                    request = recv_frame(conn)
                except FrameError:
                    break  # client disconnected -- NEVER advances the dispatch gate
                request_id = request.get('id')
                op = request.get('op')
                if op == 'ping':
                    response = {
                        'ok': True, 'op': 'ping', 'pid': os.getpid(),
                        'broker': True,
                    }
                elif op == 'status':
                    response = {'ok': True, 'op': 'status', **self.status()}
                else:
                    # Route through the single dispatcher; the per-client source_id
                    # is the broker<->client correlation, NOT the editor command id.
                    response = self._dispatcher.submit(request, source_id)
                response['id'] = request_id
                try:
                    send_frame(conn, response)
                except FrameError:
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._client_count -= 1
                self._last_client_at = time.time()

    # -- teardown ----------------------------------------------------------

    def shutdown(self) -> None:
        self._stop.set()
        self._dispatcher.stop()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        # Join the heartbeat thread BEFORE clearing the advert: otherwise a
        # heartbeat write racing past the clear would resurrect this dead
        # broker's advert and the broker would appear alive forever. The
        # heartbeat is a daemon thread on a <=BROKER_HEARTBEAT_SECONDS cadence,
        # so this join is bounded.
        heartbeat = self._heartbeat_thread
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=BROKER_HEARTBEAT_SECONDS + 1.0)
            self._heartbeat_thread = None
        # Only clear OUR advert (don't stomp a successor's). A successor would
        # have rewritten the advert with its own pid; clear only if it's still us.
        current = read_advert(self._advert_file)
        if current is not None and current.get('pid') == os.getpid():
            clear_advert(self._advert_file)

    def status(self) -> dict[str, Any]:
        with self._lock:
            clients = self._client_count
        return {
            'pid': os.getpid(),
            'endpoint': list(self.endpoint) if self.endpoint else None,
            'connected_clients': clients,
            'grace_seconds': self._grace_seconds,
            **self._dispatcher.status(),
        }


# ---------------------------------------------------------------------------
# BrokerClient: the thin-client side used by UEConnection (Phase E3). Holds one
# persistent loopback connection to the broker, with an id-correlated reader
# thread (a _pending/_responses/reader/write-lock request-correlation scheme).
# ---------------------------------------------------------------------------

class BrokerClient:
    """A thin, persistent client to a running broker over loopback TCP."""

    def __init__(self, host: str, port: int):
        self._host = host
        self._port = int(port)
        self._sock: Optional[socket.socket] = None
        self._responses: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._eof = False
        self._reader: Optional[threading.Thread] = None

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

    def connect(self, timeout: float = 2.0) -> bool:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=timeout)
        except OSError:
            return False
        sock.settimeout(None)
        self._sock = sock
        self._eof = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def alive(self) -> bool:
        return self._sock is not None and not self._eof

    def _read_loop(self) -> None:
        sock = self._sock
        try:
            while True:
                frame = recv_frame(sock)
                request_id = frame.get('id')
                with self._cond:
                    if request_id is not None and str(request_id) in self._pending:
                        self._responses[str(request_id)] = frame
                    self._cond.notify_all()
        except (FrameError, OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def request(self, payload: dict[str, Any], timeout: float) -> Optional[dict[str, Any]]:
        """Send one request, block for its id-correlated response. None on death.

        The id here is the broker<->client correlation id, DISTINCT from the
        editor command (which has none).
        """
        sock = self._sock
        if sock is None or self._eof:
            return None
        request_id = uuid.uuid4().hex
        frame = {**payload, 'id': request_id}
        with self._cond:
            self._pending.add(request_id)
        with self._write_lock:
            try:
                send_frame(sock, frame)
            except (FrameError, OSError):
                with self._cond:
                    self._pending.discard(request_id)
                return None
        deadline = time.time() + max(0.1, float(timeout))
        with self._cond:
            try:
                while request_id not in self._responses:
                    if self._eof:
                        break
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=min(0.25, remaining))
                return self._responses.pop(request_id, None)
            finally:
                self._pending.discard(request_id)

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
            self._reader = None


# ---------------------------------------------------------------------------
# Spawn + connect-or-spawn election (Phase E1). This is the floating-role,
# TOCTOU-closed bootstrap that UEConnection calls to obtain a BrokerClient.
# ---------------------------------------------------------------------------

# Windows detached-spawn flags, guarded so import succeeds on Linux/WSL.
_DETACHED_PROCESS = getattr(__import__('subprocess'), 'DETACHED_PROCESS', 0)
_CREATE_NEW_PROCESS_GROUP = getattr(__import__('subprocess'), 'CREATE_NEW_PROCESS_GROUP', 0)
_CREATE_NO_WINDOW = getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0)


def _detached_creationflags() -> int:
    return _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW


def spawn_detached_broker(
    python: Optional[str] = None,
    advert_file: Optional[str] = None,
    reasons: Optional[list] = None,
) -> Optional[int]:
    """Spawn the broker as a DETACHED, independent process and return its pid.

    Returns None on failure; when ``reasons`` is given, a human-readable failure
    reason is appended to it so the caller can surface it (e.g. into
    UEConnection's broker-unavailable reason) instead of a silent None.

    HARD REQUIREMENTS (Phase E1, item 4):
      - DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW so the
        broker OUTLIVES its spawner (it is NOT a Popen child that dies with the
        parent).
      - close_fds=True and stdio redirected to NUL/log so it holds NO handle to
        the spawner's stdin/stdout/stderr; otherwise the agent's MCP stdio
        transport wedges (the MCP client never sees EOF).
    The spawner must afterward BLOCK on advert/endpoint appearance and reap this
    pid on timeout (see ``connect_or_spawn``).
    """
    import subprocess  # local: keep module import light and Windows-flag-safe
    python = python or sys.executable
    advert_file = advert_file or advert_path()
    env = dict(os.environ)
    if advert_file:
        env['UE_BROKER_DIR'] = os.path.dirname(advert_file)
    devnull = open(os.devnull, 'r+b')
    try:
        proc = subprocess.Popen(
            [python, '-m', 'ue_ikrig_mcp.broker', '--serve'],
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            close_fds=True,
            creationflags=_detached_creationflags(),
            # On POSIX (CI), start_new_session detaches from the spawner's
            # process group, the closest analogue to the Windows detach flags.
            start_new_session=(os.name != 'nt'),
            env=env,
        )
    except Exception as exc:
        # Broad on purpose: any spawn failure (OSError, ValueError, or a
        # platform-specific subprocess error) must be surfaced as the
        # broker-unavailable reason, never swallowed into a silent None.
        if reasons is not None:
            reasons.append('broker spawn failed: %s: %s' % (type(exc).__name__, exc))
        try:
            devnull.close()
        except OSError:
            pass
        return None
    finally:
        # The child has dup'd the fd; the parent must not retain it.
        try:
            devnull.close()
        except OSError:
            pass
    return proc.pid


def _reap(pid: Optional[int]) -> None:
    """Best-effort kill of an orphan broker we spawned that never advertised."""
    if not pid:
        return
    try:
        if os.name == 'nt':
            import subprocess
            # The broker may be spawned windowless (pythonw); suppress the
            # console window taskkill would otherwise flash.
            _si = subprocess.STARTUPINFO()
            _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            _si.wShowWindow = 0  # SW_HIDE
            subprocess.run(
                ['taskkill', '/F', '/PID', str(pid)],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                startupinfo=_si,
            )
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:
        # Best-effort reap: the orphan may already be gone, or taskkill/SIGKILL
        # may be unavailable. (OSError/ValueError are subclasses of Exception.)
        pass


def connect_or_spawn(
    advert_file: Optional[str] = None,
    spawn_wait: float = BROKER_SPAWN_WAIT_SECONDS,
    python: Optional[str] = None,
    reasons: Optional[list] = None,
) -> Optional[BrokerClient]:
    """Return a connected BrokerClient, spawning a detached broker if needed.

    Floating-election flow (Phase E1), with the TOCTOU closed:
      1. read advert -> CONNECT attempt to the endpoint; if reachable, use it.
      2. otherwise acquire the bootstrap lock (single namespace, exactly one
         winner), RE-PROBE the endpoint (a peer may have respawned), and only if
         still dead spawn a fresh DETACHED broker.
      3. block until the endpoint answers (else reap the orphan we spawned).
    Losers of a spawn race fall through to step 1's connect against the winner.
    Returns None only if no broker could be reached or spawned; when ``reasons``
    is given, the failure reason is appended for the caller to surface.
    """
    advert_file = advert_file or advert_path()

    # Step 1: try the advertised endpoint without taking the lock.
    client = _try_connect_advertised(advert_file)
    if client is not None:
        return client

    # Step 2: contend for the spawn under the bootstrap lock.
    lock = BootstrapLock()
    if not lock.acquire(timeout=spawn_wait):
        # Could not get the lock in time; a peer is likely spawning. Re-probe.
        client = _try_connect_advertised(advert_file)
        if client is None and reasons is not None:
            reasons.append('bootstrap lock contended; no reachable broker after re-probe')
        return client
    spawned_pid: Optional[int] = None
    try:
        # Re-probe under the lock: a peer may have spawned+advertised already.
        # Cheap ENDPOINT probe first (a real connect, not just the advert file),
        # so a running-but-stale-advert broker is found without a full handshake.
        advert = read_advert(advert_file)
        if advert and advert.get('port') and probe_endpoint(
            advert.get('host') or BROKER_HOST, advert['port'], timeout=1.0
        ):
            client = _try_connect_advertised(advert_file)
            if client is not None:
                return client
        # Still dead: spawn a fresh detached broker.
        spawned_pid = spawn_detached_broker(
            python=python, advert_file=advert_file, reasons=reasons
        )
        if spawned_pid is None:
            return None
        # Block on endpoint appearance; reap the orphan on timeout.
        client = _await_endpoint(advert_file, spawn_wait)
        if client is None:
            _reap(spawned_pid)
            if reasons is not None:
                reasons.append(
                    'spawned broker pid %s never advertised a reachable endpoint '
                    'within %.1fs; orphan reaped' % (spawned_pid, spawn_wait)
                )
            return None
        return client
    finally:
        lock.release()


def _try_connect_advertised(advert_file: str) -> Optional[BrokerClient]:
    """Read the advert and connect to its endpoint; None if unreachable."""
    advert = read_advert(advert_file)
    if not advert:
        return None
    host = advert.get('host') or BROKER_HOST
    port = advert.get('port')
    if not port:
        return None
    client = BrokerClient(host, int(port))
    if not client.connect(timeout=2.0):
        return None
    # Verify with a ping so a stale-but-accepting socket is rejected.
    pong = client.request({'op': 'ping'}, timeout=2.0)
    if pong is None or not pong.get('ok'):
        client.close()
        return None
    return client


def _await_endpoint(advert_file: str, timeout: float) -> Optional[BrokerClient]:
    """Block until the (freshly spawned) broker advertises a reachable endpoint."""
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        client = _try_connect_advertised(advert_file)
        if client is not None:
            return client
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------------------
# Entry point for the detached process: `python -m ue_ikrig_mcp.broker --serve`.
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--serve' not in argv:
        sys.stderr.write('usage: python -m ue_ikrig_mcp.broker --serve\n')
        return 2
    broker = Broker()
    broker.bind()
    try:
        broker.serve()
    except KeyboardInterrupt:
        broker.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

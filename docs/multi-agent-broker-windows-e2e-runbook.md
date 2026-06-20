# Multi-Agent Command-Slot Broker — Windows E2E Runbook

**Purpose.** The broker that serializes multiple MCP agents against one Unreal Editor
was implemented and unit-tested on WSL/Linux with faked transport. Three classes of
behavior **cannot** be validated off-Windows against a live editor and must be checked
manually here:

1. Real **result-frame-gated** serialization against the editor's single command slot.
2. Real **detached-process** spawn/survival (Windows `DETACHED_PROCESS` flags).
3. **Kill-mid-command** / editor-restart / re-election against a real game thread.

Run this on the **native Windows** host (not WSL), with the MCP launched the normal way
(per-agent stdio, unchanged `uvx` invocation). The broker auto-spawns on first use — there
is no separate server to start.

---

## 0. Reference: what to observe

| Thing | Where |
|---|---|
| Broker advert | `%ProgramData%\ue-ikrig-mcp\broker.advert.json` |
| Bootstrap lock | `%ProgramData%\ue-ikrig-mcp\broker.bootstrap.lock` |
| Broker process | a `python -m ue_ikrig_mcp.broker --serve` process (detached, no console window) |
| Transport | loopback TCP on `127.0.0.1:<port>` (port is in the advert) |
| Status | the `connection_status` MCP tool → `broker` section |
| Editor command slot | exactly **one** TCP connection from the broker to the editor's remote-exec command port |

**`connection_status` → `broker` fields** (this is your primary instrument):
`enabled`, `supported`, `connected`, `available`, `transport` (`loopback_tcp`),
`reason` (only when unavailable), `endpoint` `[host, port]`, `pid`, `queue_depth`,
`current_holder` (the per-client source id holding the slot), `result_frame_observed`,
`channel_poisoned`, `connected_clients`.

**Env knobs** (defaults in parens; override before launching the agents):
`UE_BROKER` (true) — master switch; `UE_BROKER_DIR` (ProgramData) — advert/lock dir;
`UE_BROKER_GRACE_SECONDS` (30) — idle self-teardown; `UE_BROKER_MAX_QUEUE_DEPTH` (64) —
backpressure; `UE_BROKER_SPAWN_WAIT_SECONDS` (15); `UE_BROKER_HEARTBEAT_SECONDS` (5).

> Tip: for faster iteration set `UE_BROKER_GRACE_SECONDS=5` so the broker tears down quickly
> between scenarios. Reset advert state between runs by deleting `%ProgramData%\ue-ikrig-mcp\`
> when **no** agent and **no** broker are running.

Quick PowerShell helpers:
```powershell
# Watch for the broker process
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*ue_ikrig_mcp.broker*' } | Format-Table Id, StartTime
# Read the advert
Get-Content "$env:ProgramData\ue-ikrig-mcp\broker.advert.json"
# Count broker->editor command connections (replace <EDITOR_CMD_PORT> with the editor's command port)
(Get-NetTCPConnection -RemotePort <EDITOR_CMD_PORT> -State Established -ErrorAction SilentlyContinue).Count
```

---

## Scenario 1 — Auto-spawn + single broker (smoke)

**Steps**
1. Ensure no broker is running and `%ProgramData%\ue-ikrig-mcp\` is empty.
2. Start **one** agent; `connect_to_editor`, run one trivial `execute` (e.g. print a number).
3. Call `connection_status`.

**Expect**
- `broker.enabled=true`, `supported=true`, `connected=true`, `available=true`, `transport=loopback_tcp`.
- `broker.advert.json` exists and contains the live `pid` + `endpoint`; that pid is a running detached `--serve` process with **no console window**.
- The command returns a correct result.

**Fail signs:** `connected=false` with a `reason` (record it); no advert; a visible console window (means not detached).

---

## Scenario 2 — The original repro: N agents, no steal-storm (CORE FIX)

This is the bug this whole change exists to kill (WinError 10053 last-writer-wins eviction).

**Steps**
1. Start **3+ agents** against the same editor (the exact setup that used to race).
2. Have every agent run a tight loop of `execute` calls concurrently for ~30s
   (e.g. each appends a line to a distinct file, or returns an incrementing counter).
3. While running, observe: broker process count, advert pid, and broker→editor TCP connections.

**Expect**
- **Zero `WinError 10053` / `WSAECONNABORTED`** in any agent. Every command from every agent succeeds.
- Exactly **one** broker process; exactly **one** advert pid (unchanged throughout).
- Exactly **one** established broker→editor command connection (the slot is never re-opened/evicted).
- `connection_status.broker.connected_clients` reflects the number of live agents; `queue_depth` rises under load and drains.

**Fail signs:** any 10053; more than one broker pid; the editor command connection count flapping > 1.

---

## Scenario 3 — Result-frame gating / single-outstanding (CORRECTNESS KEYSTONE)

Proves the broker dispatches **at most one** command at a time and advances only on the
editor's result frame — never interleaving, never mis-attributing a result.

**Steps**
1. From agent A, issue a **slow** command (e.g. a Python sleep of ~5s **on the game thread**, or any genuinely slow editor op) that returns a uniquely identifiable result `A`.
2. ~1s later, from agent B, issue a **fast** command returning a uniquely identifiable result `B`.
3. Record each agent's returned value and completion time.

**Expect**
- A returns value `A`; B returns value `B` — **no cross-attribution** (B never gets A's frame).
- B's command does **not** start until A's result frame is received: B completes *after* A, not concurrently. (If you can log editor-side, only one command body runs at a time.)
- During the window, `current_holder` shows A's client id, then B's.

**Fail signs:** B returns `A`'s payload (positional mis-read); both bodies run concurrently editor-side.

---

## Scenario 4 — No-double-execute on timeout (NON-IDEMPOTENT SAFETY)

The worst outcome is re-running a non-idempotent UE mutation. Verify a timeout **surfaces**,
never silently re-applies.

**Steps**
1. Pick a **non-idempotent, observable** mutation that runs *longer than the command timeout*
   (e.g. spawn-an-actor / append-to-an-asset that takes longer than the configured timeout, or
   temporarily lower the timeout). Use something whose application count you can inspect afterward.
2. Run it once from one agent. Let it time out.
3. Inspect the editor state (how many times the mutation actually applied) and `connection_status`.

**Expect**
- The agent sees a **surfaced failure** (timeout), not a success.
- The mutation applied **at most once** (the editor may finish the in-flight op, but it is **never re-sent**).
- `broker.channel_poisoned=true` immediately after the timeout while the editor is still alive; subsequent *mutating* dispatch is blocked (Scenario 5 covers recovery).

**Fail signs:** the mutation applied twice; the agent gets a success then the slot advances on a timer.

---

## Scenario 5 — Poison → recovery only on proven editor death/restart

Verifies the gate does **not** advance on a bare timer; it waits for proven editor death or restart.

**Steps**
1. Trigger a poison as in Scenario 4 (command times out while the editor stays alive).
2. From a second agent, attempt a command. Observe `channel_poisoned` and whether it dispatches.
3. Now **restart the editor** (or close it). Re-run a command from an agent.

**Expect**
- While the editor is alive-but-poisoned, the second mutating command is **blocked** (it does not run on a timer).
- After the editor is proven gone / restarted, `channel_poisoned` clears and dispatch resumes; reconnection happens without replaying the timed-out command.

**Fail signs:** the blocked command runs while the editor is still alive (timer advance); a replay of the poisoned command after restart.

---

## Scenario 6 — Detached survival: kill the spawning agent (DETACHMENT)

The broker is spawned **detached**, so it must outlive the agent that spawned it.

**Steps**
1. Start agent #1 (it spawns the broker). Note the broker pid from the advert.
2. Start agent #2 and run a command (confirm it works via the same broker pid).
3. **Hard-kill agent #1** (close its process / End Task).
4. From agent #2, keep running commands.

**Expect**
- The broker process (same pid) **keeps running** after agent #1 dies.
- Agent #2's commands continue to succeed uninterrupted.
- `connected_clients` drops by one but the broker stays up (still has agent #2).

**Fail signs:** broker dies when agent #1 dies (means it was a bound child, not detached); agent #2 errors.

---

## Scenario 7 — Floating re-election: kill the broker (RESILIENCE)

Any agent can re-elect a dead broker; exactly one new broker comes up.

**Steps**
1. With ≥2 agents connected, **hard-kill the broker process** (by its advert pid).
2. From each agent, issue a new command (roughly concurrently).

**Expect**
- Exactly **one** new broker is elected (advert pid changes to a single new value — not several).
- Commands resume on all agents.
- Any command that was **in flight** at kill time is **surfaced as failed**, never silently replayed; queued-but-undispatched commands are safe to retry.

**Fail signs:** multiple new brokers; a replayed in-flight mutation; agents stuck unable to re-elect.

---

## Scenario 8 — Kill an agent mid-command (SLOT RELEASE)

**Steps**
1. From agent A, start a slow command.
2. While it is in flight, **Ctrl-C / kill agent A**.
3. From agent B, issue a command.

**Expect**
- A's in-flight command is **not replayed** anywhere.
- The slot is released after the editor's result frame (or proven death); B proceeds.
- No 10053 on B.

---

## Scenario 9 — Grace self-teardown

**Steps**
1. With `UE_BROKER_GRACE_SECONDS` at a small value (e.g. 5), disconnect/close **all** agents.
2. Wait > grace seconds.

**Expect**
- The broker process exits on its own.
- `broker.advert.json` is removed by the broker (it only clears its own-pid advert).

**Fail signs:** broker lingers well past grace with no clients; stale advert pointing at a dead pid is left behind (a *next* agent should still re-elect cleanly per Scenario 7, but the self-clear should normally happen).

---

## Scenario 10 — Backpressure (optional)

**Steps**
1. Set `UE_BROKER_MAX_QUEUE_DEPTH=2`.
2. Fire many concurrent commands from several agents faster than the editor drains them.

**Expect**
- Excess requests get a fast **`broker_busy`** rejection (`delivered:false`) rather than unbounded queueing — surfaced to the agent as a retryable "busy", not a hang.

---

## Scenario 11 — WSL is unsupported for the broker (only if you also run under WSL)

**Steps**
1. Run the MCP under WSL.
2. Call `connection_status`.

**Expect**
- `broker.supported=false`, `reason=wsl_unsupported`. The broker is native-Windows-only by design. The WSL→Windows subprocess bridge has been removed, so under WSL the server cannot discover an editor on the Windows host; launch the server via Windows Python (`pythonw.exe -m ue_ikrig_mcp`) instead.

---

## Pass/fail summary to report back

Record, for each scenario: PASS / FAIL + the relevant `connection_status.broker` snapshot.
The **must-pass** gates before declaring the multi-agent race fixed in production:
- **Scenario 2** (no 10053 under N concurrent agents, single broker, single editor slot),
- **Scenario 3** (no result mis-attribution, serialized),
- **Scenario 4** (no double-execute on timeout),
- **Scenario 6 & 7** (detached survival + clean single re-election).

If any must-pass fails, capture the agent error text + the `broker` status block + the advert
contents and hand back for a targeted fix.

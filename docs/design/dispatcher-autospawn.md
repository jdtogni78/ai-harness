# Design: supervisor auto-spawn of the local-dispatcher session

**Status:** implemented; amended after the Stage-3 runaway fix (see
"Amendment: inject into `<host>-dev`, never spawn a one-off" below).
**Owner:** this thread.
**Companion:** Stage 1 changes (active-dirs cleanup + `~/dev/CLAUDE.md`).

> **Amendment (runaway fix).** The original "Dispatch action" below routed
> through `new-session`'s DEFAULT behaviour, which spawns a fresh `oneoff-*`
> server (capacity 1). The dispatcher cse_ attached to THAT new server — never
> to the running `<host>-dev` server — so `<host>-dev` Capacity stayed 0/32 and
> the supervisor re-dispatched **every tick** (a per-tick one-shot storm,
> observed live: 6 cycles before manual revert). `claude remote-control` has no
> "attach to an already-running named server" primitive, so the fix is:
>
> 1. **`new-session --inject-into <SERVER>`** — a mode that does NOT spawn a
>    server. It reads `<SERVER>.log` (the supervisor's logdir), harvests the
>    cse_ of the session that server has **already pre-created**, sets its
>    `[dispatcher]` title, and submits the first turn into it — raising the
>    EXISTING server's Capacity 0→1. (`remote_control/new_session.py
>    :inject_into_server`.)
> 2. **The `<host>-dev` server is spawned with `--create-session-in-dir`** (only
>    dev, only when `DISPATCHER_AUTOSPAWN` is on) so it comes up WITH a
>    pre-created session for the inject to harvest. Every other supervised
>    server keeps `--no-create-session-in-dir` (comes up empty, idle-recyclable).
>    (`remote_control/procutil.py :spawn_argv`.)
> 3. **Defense-in-depth:** the supervisor records the `<host>-dev` server **pid**
>    it last dispatched into (`Supervisor._dispatched_dev_pid`) and refuses to
>    re-dispatch while that same process is alive — so a single bad Capacity read
>    can't restart the storm. The pid resets when the dev server is recycled
>    (new pid → its session is gone → a fresh inject is legitimate).
>
> Net effect: dispatching now occupies the slot the supervisor gates on
> (`should_dispatch_dispatcher`'s `cap==0`), so the second tick is correctly a
> no-op. This matches the "only dev is allowlisted" design intent. The
> "Dispatch action" and "Idempotency" sections below describe the original
> (buggy) flow and are kept for history; the amendment supersedes them.

## Problem

In the manager-only model, each host should always have one live
`cse_*` session anchored at `~/dev` acting as the local dispatcher
(see `~/dev/CLAUDE.md`). Today the supervisor only spawns *servers*,
not sessions — so `<host>-dev` is always up, but whether a `cse_*` is
attached to it depends on whether a human (or another agent) opened
one. Goal: guarantee a live dispatcher cse_ on each host, recreated if
it ends, without spinning up extras when one already exists.

## Constraints

1. **Idempotent.** Multiple ticks must not produce multiple dispatcher
   sessions. The supervisor ticks every ~30s; cold-start session
   registration takes several seconds; we can't race ourselves.
2. **Respect human sessions.** If the user opens their own session on
   `<host>-dev` (Capacity ≥ 1), do not also spawn a dispatcher — they
   are using the slot. We re-spawn after their session ends.
3. **Don't fight `idle_recycle_secs`.** The supervisor recycles a
   server idle for ≥ `IDLE_RECYCLE_SECS`. If we spawn a dispatcher
   that never gets used, the supervisor would TERM the server, killing
   the dispatcher we just made. Either the dispatcher's mere existence
   keeps Capacity ≥ 1 (it should — a live cse_ holds a slot), or we
   exempt `<host>-dev` from idle-recycle.
4. **Survive restarts.** After supervisor kickstart, the dispatcher
   should reappear without operator intervention.
5. **Fail-soft.** If `new-session` dispatch fails (cloud down, auth
   expired, etc.) the supervisor must keep running its primary job.
   Log + retry on next tick; no crash, no exponential mess.

## Design

### Where the logic goes

A new helper in `remote_control/supervisor.py`, called from `tick()`
after the main spawn/recycle pass. Keep it a single concern: ensure a
live dispatcher cse_ exists on `<host>-dev`.

```python
def _ensure_dispatcher(self, now: float) -> None:
    """If <host>-dev is healthy and has no live cse_, dispatch one."""
```

Called once per tick, only for the `dev` entry. No-op if `dev` isn't
allowlisted on this host (so disabling auto-spawn = just remove
`dev@<host>` — same model as everything else).

### "Is a live cse_ already attached?" — detection

Two signals, AND'd:

1. **Capacity from log** (already in supervisor): `read_capacity(<host>-dev.log)`.
   Capacity ≥ 1 → at least one session attached. **Don't dispatch.**
2. **Cloud session list** (already wired in `_rehydrate_on_startup`
   via `usage_limit.monitor.list_sessions`): filter for sessions whose
   server name is `<host>-dev`. If any exist and are live, **don't
   dispatch.**

Signal 1 alone is enough for the common case. Signal 2 is the
belt-and-suspenders for "cloud says we have a session, log hasn't
updated Capacity yet" (cold-start race). On signal 2 failure (no
token, network blip) fall back to signal 1 only.

### Dispatch action

```python
# Equivalent to: python3 -m remote_control new-session \
#   --dir ~/dev --no-reply-to \
#   --prompt-file <dispatcher-brief> --subname dispatcher
from . import new_session  # the CLI module
new_session.run(...)        # or subprocess to keep import surface small
```

Use the existing `new-session` primitive — it already handles spawn +
wait-for-registration + first-turn submission as one call. `--no-reply-to`
because the dispatcher has no caller. `--subname dispatcher` so the
picker row reads `[NICK.host][dispatcher] auto-spawned`.

The prompt file: `<repo>/remote_control/prompts/dispatcher.md`,
shipped with the repo. Short: "you are the local dispatcher, see
`~/dev/CLAUDE.md`, await routing requests via send-to-session."

### Idle-recycle interaction

A live dispatcher cse_ keeps `<host>-dev` at Capacity 1/32, so
`is_busy(1) == True`, so `last_busy` keeps refreshing, so
`should_recycle` stays false. The existing logic already does the
right thing — no special case needed.

If the dispatcher cse_ dies (cloud-side eviction, user-archived,
crash), Capacity drops to 0 and the existing `idle_recycle_secs`
clock starts. Within `IDLE_RECYCLE_SECS` (default ~hours), recycle
TERMs and respawns the server, and *the next tick after that* our
`_ensure_dispatcher` re-dispatches. No special handling needed; the
gap is bounded by `tick_secs + idle_recycle_secs` worst case.

For tighter recovery, optionally short-circuit: if `Capacity == 0`
*and* there's no live cse_ in the cloud list *and* the server has
been at 0 for ≥ `dispatcher_min_idle_secs` (e.g. 60s), re-dispatch
without recycling. Out of scope for v1.

### Idempotency between ticks

Dispatch is async — `new-session --prompt-file` blocks ~30s for the
cse_ to register, then returns. During that window the next tick
must not dispatch *again*. Two guards:

1. **Per-name in-flight flag** on the Supervisor instance:
   `self._dispatcher_inflight: bool`. Set before dispatch, cleared
   after, with a timeout fallback (≤ `wait_timeout + slop` seconds).
2. The `new-session --wait` call is **synchronous** — by the time
   `tick()` returns, the cse_ is registered and Capacity is 1. So
   the next tick sees Capacity ≥ 1 and is a no-op.

(1) alone is sufficient; (2) is why this works in practice. We could
just rely on (2), but (1) is cheap defense against a hung registration.

### Tick-time cost

`new-session --wait --prompt-file` blocks the supervisor's tick loop
for up to 30s. That delays the next tick by that much. Options:

- **Accept it.** Dispatching is rare (only when the dispatcher is
  missing) — usually once per supervisor lifetime per host. A 30s
  tick delay during startup is acceptable.
- **Background it.** Spawn `new-session` as a child process and let
  it run async; the supervisor returns to its tick. Next tick observes
  the new cse_ via signal (1) or (2). Cleaner but adds plumbing.

Pick **accept it** for v1; revisit if it bites.

## Config

New `SupervisorConfig` field (env-driven, like the rest):

| env | default | meaning |
|---|---|---|
| `DISPATCHER_AUTOSPAWN` | `on` | `off` disables the whole feature |
| `DISPATCHER_PROMPT_FILE` | `<repo>/remote_control/prompts/dispatcher.md` | brief used for the first-turn |
| `DISPATCHER_WAIT_TIMEOUT_SECS` | `45` | pass-through to `new-session --wait-timeout` |

## Testing

Pure-decision helpers stay testable in `tests/test_supervisor_decisions.py`:

```python
def should_dispatch_dispatcher(
    capacity: int,
    live_cse_count_for_dev: int,
    inflight: bool,
    autospawn_enabled: bool,
) -> bool: ...
```

Integration: the existing supervisor test harness already injects a
fake `proc`. Add a `fake_new_session` callable on the Supervisor
constructor (default = real `new-session` invocation) so tests can
assert "dispatched with these args" without spawning real servers.

## Rollout

1. Land behind `DISPATCHER_AUTOSPAWN=off` default — feature off in
   production until manually flipped on.
2. Enable on m5 first. Confirm one dispatcher cse_ appears, survives
   tick cycles, isn't double-dispatched.
3. Enable on mini.
4. Flip default to `on` after a few days clean.

## Open questions

- **Subname**: is `[dispatcher]` the right tag, or do you want
  something shorter (`[disp]`)? Affects picker readability.
- **Prompt content**: bare-minimum vs. full role briefing in the
  first turn (since `~/dev/CLAUDE.md` already covers the role)?
  Bare-minimum recommended.
- **Recycle exemption**: should `<host>-dev` skip idle-recycle
  entirely (since the dispatcher is meant to be permanent)? Probably
  yes — cleaner than the "respawn → re-dispatch within
  `idle_recycle_secs`" gap. Trade-off: no scheduled hygiene restart
  for the dispatcher server itself.
- **Cross-host bootstrap**: how does a dispatcher on host A discover
  the dispatcher's cse_id on host B? Today: human looks it up via
  [[list-sessions]]. Future: dispatcher writes its id to a known
  cloud-side key (KV / session-tag) on first turn; peer dispatcher
  reads it. Out of scope for v1.

## Out of scope (explicitly)

- Killing existing non-dispatcher sessions on `<host>-dev` to make
  room. If the user is on the slot, leave them alone.
- Multi-dispatcher (sharded by tenant / role). One per host.
- Cross-host failover (dispatcher on m5 routing for mini if mini is
  down). Each host owns its own dispatcher.

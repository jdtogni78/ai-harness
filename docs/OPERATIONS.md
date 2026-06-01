# Operations & CLI reference

The runbook for the services and the `python3 -m remote_control …` command-line
surface. For the *why* and the design, see [ARCHITECTURE.md](ARCHITECTURE.md);
for the bird's-eye view, the [top-level README](../README.md).

All commands run from the repo dir (so `python3 -m` finds the local package; the
installed LaunchAgents set `PYTHONPATH` for launchd). Everything is **stdlib-only
Python on the system `/usr/bin/python3`** — no venv, no pip.

```
python3 -m remote_control <supervisor|usage-monitor|manager|manager-ui|perm-gate|install|codex-import|titles|sessions|work|fork>
```

## Layout of runtime files

| Path | Purpose |
|---|---|
| `~/.ai-harness/active-dirs.txt` | Allowlist of dir basenames the supervisor will spawn servers for (per-user, per-host, chmod 600 — names private app dirs, not in the repo). Re-read every tick. The installer seeds an empty template if missing. Override the path via `REMOTE_CONTROL_ACTIVE_FILE`. See [Activation list](#activation-list). |
| `com.*.claude-remote-control.plist` | LaunchAgent for the claude supervisor → `python3 -m remote_control supervisor` (runs at login, `KeepAlive`). |
| `com.*.claude-usage-limit-monitor.plist` | LaunchAgent for the usage-limit monitor → `python3 -m remote_control usage-monitor` (runs at login, `KeepAlive`). |
| `com.*.claude-titles-monitor.plist` | LaunchAgent for the title-prefix watcher → `python3 -m remote_control titles watch` (runs at login, `KeepAlive`). Split out from the usage-limit monitor so the two services have independent cadences and lifecycles. |
| `logs/` | Runtime logs (gitignored). `manager.log` = claude supervisor; `<host>-*.log` = per-claude-server output (one per allowlisted dir, prefixed with the host nickname); `usage-limit-monitor.log` + `paused-sessions.json` = monitor activity + state. |

## Configure your apps

Most of the harness (the supervisor, monitor, manager, perm-gate, work / titles
/ sessions CLIs) is project-agnostic — it acts on **dirs under your dev root**
and **cloud sessions**, with no knowledge of what those apps are. The only
subsystem you have to teach about your apps is the **environment pool** that
backs the `lease-env` / `test-env` skills.

The pool is driven by a single YAML config. The public repo ships only the
template — there are **no per-user pool configs in this repo**.

- **Template:** [`scripts/pool/config.example.yaml`](../scripts/pool/config.example.yaml)
  — copy and edit. Describes the expected shape (adapter, DB prefix, state
  dir, container names, compose services, slot/offset arrays, baseline backup
  path, etc.).
- **Where the live config is loaded from**, in order
  ([`scripts/pool/load-config.sh`](../scripts/pool/load-config.sh)):
  1. `$AI_HARNESS_POOL_CONFIG` (explicit env override — typical when the
     config lives outside this repo, e.g. in a private personal-config repo
     under `~/.ai-harness/pools/<name>/config.yaml`).
  2. `scripts/pool/config.local.yaml` (gitignored; keep it local or
     SOPS-encrypt if it must travel).
- **Adapter selection.** `adapter: <name>` in the config picks
  `scripts/pool/adapters/<name>-pool.sh` + `<name>-testpool.sh`. The only
  shipped adapter today is `laravel-docker` (a Laravel + Docker Compose dev
  loop with a per-slot MariaDB). Supporting a different stack means dropping a
  new adapter pair next to it and pointing `adapter:` at it.
- **What the config controls.** Every per-app value is in the YAML:
  `db.prefix` (so each slot N gets a `<prefix>_testN` DB), `state_dir` (where
  lease tables / locks / the pool-owned datadir / the warm cache live —
  outside the repo, survives worktree churn), container names
  (`app.container_prefix`, `db.dev_container`, `db.test_container`), compose
  services to bring up per slot, baseline backup subpath, slot / port-offset
  layout for both pools, etc.
- **Tradeoff to know.** The pool assumes a **Docker-Compose-style local dev
  loop** with a snapshot-restorable database. It is not a general-purpose CI
  system — it optimizes for "claim a slot, run my branch, hand it back" on the
  same workstation, not for cluster scheduling or remote execution.

After editing the config, the next `lease-env` / `test-env` invocation picks
up the change — there is no daemon to restart. The supervisor and the other
services don't read the pool config at all.

## Services

### Remote-control supervisor

- The LaunchAgent starts the supervisor (`python3 -m remote_control supervisor`)
  at login (`RunAtLoad`) and `KeepAlive`s it — if the supervisor itself dies,
  launchd relaunches it (throttled 10s) and it re-adopts the running servers.
- The supervisor loops every `TICK_SECS` (default 30s): discovers dirs, and for
  each ensures a server is running, respawning within ~one tick if not.
- **Naming / spawn mode:** `<host>-<dir>` (original casing; `<host>` is the
  supervisor's host nickname). `~/dev` root →
  `--spawn same-dir`; git-repo subdirs → `--spawn worktree`; non-git →
  `same-dir`. All run `--permission-mode bypassPermissions
  --no-create-session-in-dir`, so spawned servers come up empty — sessions are
  created on demand from the app, not pre-allocated at start. The
  `bypassPermissions` mode skips in-Claude permission prompts so unattended
  inner sessions don't hang on ask-list ops; the global PreToolUse perm-gate
  hook (#23) still vets every tool call.
- **Adoption:** servers already running (incl. ones from an earlier
  fire-and-forget model) are adopted by PID — no disruptive kill at cutover.
- **Idle-recycle:** a server idle (`Capacity == 0` in its log — no on-demand
  sessions; there is no pre-created session) continuously for
  `IDLE_RECYCLE_SECS` (default 12h) is restarted. Recycling an idle server
  never kills in-flight work; it bounds how long an *idle* hung server lasts.
- **Clean shutdown:** because the plist does **not** set
  `AbandonProcessGroup`, launchd SIGTERMs the supervisor at logout/reboot; its
  SIGTERM handler forwards SIGTERM to every server so they can deregister from
  the cloud, then exits.

Tunables (env, overridable in the plist if needed): `TICK_SECS`,
`IDLE_RECYCLE_SECS`, `GRACE_SECS`, `REMOTE_CONTROL_DEV`,
`REMOTE_CONTROL_CLAUDE_BIN`, `REMOTE_CONTROL_LOGDIR`,
`REMOTE_CONTROL_ACTIVE_FILE`.

#### Activation list

The supervisor only spawns servers for dirs listed in
`~/.ai-harness/active-dirs.txt` (per-user, per-host, chmod 600; one basename
per line; `#` comments and blank lines ignored). `dev` (the basename of
`$REMOTE_CONTROL_DEV`) is the special entry for the root itself. Override the
path via `REMOTE_CONTROL_ACTIVE_FILE`. The installer creates the file with an
empty template if it doesn't exist, so a fresh checkout's first supervisor
boot finds a valid (empty) allowlist.

- **Not in the repo.** The file names private app dirs, so it lives under
  `~/.ai-harness/`, not the public repo. Each host maintains its own.
- **Reloads in <1 tick.** The file is re-read every `TICK_SECS` (~30s). Adds
  spawn within one tick; removes get SIGTERM'd within one tick. No
  `launchctl kickstart` needed.
- **Host scoping.** The `@<nick>` suffix still parses, useful if you sync
  this file via dotfiles across machines: `your-app@host-a` spawns only on
  the host whose nickname is `host-a`; `name@nick1,nick2` allows several; a
  bare `name` (no `@`) spawns on every host that has the dir. Each host's
  nickname is `REMOTE_CONTROL_HOST` (set in its plist's
  `EnvironmentVariables`) or, if unset, derived from the hostname via
  `NICKNAME_RULES`, falling back to the first hostname label lowercased.
  Matching is case-insensitive; the active nickname is logged at supervisor
  startup.
- **Fail-closed.** If the file is missing or unreadable, the supervisor logs
  it and treats nothing as allowed — running servers are deactivated. A
  typo'd path will never silently mean "spawn everything".
- **Deactivation is destructive to in-flight work.** Removing a name from the
  allowlist sends SIGTERM (then KILL after `GRACE_SECS`) regardless of
  `Capacity` — the design assumes you want the server gone. Edit-then-wait
  is the way to wind something down deliberately.
- **Manage via the `/remote-control-dirs` skill** (list / enable / disable /
  status), or just edit `~/.ai-harness/active-dirs.txt` directly.

#### Known limitations (read this)

- **"Hung but alive" with an active session is NOT auto-recovered.** A server
  with `Capacity >= 1` is treated as busy and left alone so in-flight work is
  never killed on a guess. There is no health endpoint on `claude
  remote-control` and the per-server log is a repainting TUI (it reprints
  "Connected" regardless of cloud-link health), so unresponsiveness can't be
  reliably detected. Only *idle* hangs are bounded (via idle-recycle).
- **Clean-deregister-on-SIGTERM is a strong assumption, not verified.** The
  architecture (no `AbandonProcessGroup` + SIGTERM forwarding) gives servers a
  chance to deregister at reboot, which *should* prevent ghost sessions piling
  up in the app. This has not been confirmed by a controlled reboot test.
- **Ghost sessions in the app can't be cleared locally.** The session list in
  the app is cloud-side state. No local command deletes a stale session entry;
  the cloud evicts them on heartbeat timeout. Clean shutdown reduces how many
  appear; it can't delete ones already shown.
- **The server has no resume.** `claude remote-control` always spawns *fresh*
  sessions — there is no flag to bring a prior conversation back as a live,
  controllable session in the app. Transcripts are never lost (on disk under
  `~/.claude/projects/<encoded-dir>/*.jsonl`); see recovery below.

#### Resuming a previous conversation

The supervisor cannot reattach old chats (the CLI doesn't support it). To
continue prior work:

- **From a terminal:** `cd <project>` then `claude --resume` (picker) or
  `claude -c` (most recent).
- **From the phone / desktop app:** connect to the (now-running) server →
  new session → run the `/resume-work` skill. It lists the repo's recent /
  interrupted sessions, reconstructs the goal + remaining work from the
  on-disk transcript, and continues. This is the only supported way to pick a
  prior chat back up from the app.

### Usage-limit auto-resume monitor

A second LaunchAgent (alongside the supervisor) that auto-resumes sessions
paused by the cloud usage/session limit. It talks to the **code-sessions API** —
*not* local JSONL transcripts. See
[`usage-limit-monitor-v2.md`](usage-limit-monitor-v2.md) for the full design and
why the earlier JSONL approach was abandoned.

> **Why not JSONL?** The local transcript session id (a uuid) is a *different
> conversation* from the cloud/bridge session (`cse_…`) the app shows, so
> `claude --resume <uuid>` never touches the session the user sees. Live
> bridged pauses also often aren't written to the local JSONL at all. Only the
> API sees and resumes the real sessions (both `bridge` and `anthropic_cloud`).

- **Auth:** the OAuth token is read from the macOS keychain each cycle
  (`security find-generic-password -s "Claude Code-credentials" -w` →
  `.claudeAiOauth.accessToken`, never logged); the always-running `claude`
  servers keep it refreshed. Calls send `anthropic-version: 2023-06-01` +
  `anthropic-beta: oauth-2025-04-20`.
- **Detect (every `USAGE_LIMIT_DETECT_SECS`, default 60s):**
  `GET /v1/code/sessions`; a session is paused when `status == "active"`,
  `worker_status == "idle"`, and
  `external_metadata.post_turn_summary.status_category == "failed"` with a
  `status_detail` matching *usage limit* or *session limit* (other `failed`
  details — e.g. path errors — are ignored). A tracked session is dropped as
  soon as the API stops reporting it paused (it recovered).
- **Resume (every `USAGE_LIMIT_RESUME_SECS`, default 300s):** picks the oldest
  due session and `POST`s a user turn to `/v1/code/sessions/{id}/events`.
  Success is confirmed by re-`GET`ting the session (no longer `failed`).
  **Session (5h) limits** carry a reset time in `status_detail` ("resets 7:50pm
  UTC") and the next attempt is scheduled just after it; **monthly** limits back
  off **5 / 15 / 30 min** until they clear. One resume per tick.
- **DRY_RUN (default ON):** detects and logs would-be resumes but fires no
  `POST`. Set `USAGE_LIMIT_DRY_RUN=0` to actually resume.
- **State file:** `logs/paused-sessions.json` —
  `{cse_id, env_kind, title, status_detail, first_seen, attempts, last_attempt_at, next_attempt_at}`,
  keyed by `cse_` id; entries older than 7 days are GC'd. Survives daemon
  restarts so attempts/backoff aren't reset on a launchd respawn.
- **Tunables (env):** `USAGE_LIMIT_DETECT_SECS`, `USAGE_LIMIT_RESUME_SECS`,
  `USAGE_LIMIT_DRY_RUN`, `USAGE_LIMIT_RESUME_MESSAGE` (default `continue`),
  `USAGE_LIMIT_MAX_ATTEMPTS` (0 = unlimited), `USAGE_LIMIT_HTTP_TIMEOUT_SECS`,
  `USAGE_LIMIT_SKIP_SIDS`, `REMOTE_CONTROL_LOGDIR`,
  `REMOTE_CONTROL_KEYCHAIN_SERVICE`.
- **Single-instance:** lockfile at `logs/usage-limit-monitor.lock`.
- **Clean shutdown:** SIGTERM handler leaves the state file consistent and
  removes the lockfile.
- **Caveat:** resuming injects a `continue` user turn into the conversation;
  whether the app's "Try again" instead re-runs the failed turn without adding a
  message is an open question (see the design doc). Detection/backoff/selection
  logic is covered by `tests/test_usage_detect.py` + `tests/test_usage_backoff.py`.

### Title-prefix watcher

A third LaunchAgent (`com.<user>.claude-titles-monitor`) that re-applies the
`[NICK.host]` title prefix to every active session on a fixed cadence, to
counterweight the platform's auto-titler that strips our prefix mid-session.
Split out from the usage-limit monitor so the two services have independent
cadences, failure modes, and restart triggers.

- **Loop:** every `SESSION_TITLE_APPLY_SECS` (default `600`s) calls
  `apply_prefixes` -- the same code path as a `titles apply` one-shot. Token
  is re-read from the keychain each tick (so launchd respawns / token
  rotations propagate without restarting the daemon). An exception in
  `apply_prefixes` is logged and swallowed -- the daemon never breaks the
  loop on a transient API hiccup.
- **What it preserves:** `[NICK.host]` prefixes (e.g. `[AH.<host>]`) on
  sessions that the platform's auto-titler would otherwise rewrite. Pairs
  with the `plan_renames` self-claim guard that keeps a matching host token
  from being stripped even when the on-disk indexer can't see the session
  (see `tests/test_session_titles.py::test_own_host_claim_preserved_when_not_in_worktree_index`).
- **Single-instance:** lockfile at `logs/titles-monitor.lock` (distinct from
  the usage-limit lockfile -- the two services run side by side).
- **Logs:** `logs/titles-monitor.log` (UTC timestamps). Each non-empty pass
  logs `titles: re-applied N prefix(es), M failed`.
- **Tunables (env):** `SESSION_TITLE_APPLY_SECS`, `SESSION_TITLE_NICKNAMES`
  (extends the repo->nickname map), `SESSION_TITLE_FORMAT` (the prefix
  template; default `{nick}.{host}`), `REMOTE_CONTROL_LOGDIR`,
  `REMOTE_CONTROL_HOST` (host nickname for the `.host` segment).
- **One-shot equivalent:** `python3 -m remote_control titles watch
  --interval 0` (foreground, runs once and exits)  -- though `titles apply`
  is the actual one-shot CLI; `watch` is daemon-shaped.

### Autonomous session manager + dashboard

`manager` reads every session's state and classifies each into one actionable
case (waiting on a question / idle-maybe-done / broken / limit-paused /
running), then plans the matching action. `manager-ui` is a local web dashboard
to review those analyses and tune the guidelines that drive them. Both are
**dry-run / review-only by default**; the full design, the case catalog, and the
open submission-shape question live in
[`session-manager-cases.md`](session-manager-cases.md).

```sh
python3 -m remote_control manager              # one classify/plan pass (dry-run)
python3 -m remote_control manager --loop       # keep classifying on an interval
python3 -m remote_control manager --replay scenarios/<x>.json --repo R  # offline investigator replay
python3 -m remote_control manager-ui           # open the review dashboard (binds 127.0.0.1)
```

### AI permission gate (`PreToolUse` hook)

`perm-gate` is a Claude Code `PreToolUse` hook that decides **allow / deny / ask**
per tool call (static rules + an AI tier for the ambiguous middle), against the
same stakes policy in [`session-manager-cases.md`](session-manager-cases.md). It
reads the hook event on stdin and prints the decision on stdout. It is
**shadow-first** (`PERM_GATE_ENFORCE=0` — logs to `logs/perm-gate-decisions.jsonl`
and changes nothing) and **fail-safe to `ask`** on any error. Register it in
`~/.claude/settings.json`; full setup, the two-tier model, and all `PERM_GATE_*`
tunables are in [`perm-gate.md`](perm-gate.md).

## Cross-engine work orchestration

`work` is a single, cross-engine "what's running where" view — Claude Code
sessions (local bridge + Anthropic cloud, from the code-sessions API) **and**
Codex rollout threads on this machine — merged into one table keyed by engine,
repo, host, and a recency-based `active`/`idle` activity (the same 1h TTL the
multi-agent claim convention uses). By default it shows only **in-flight** work
(active within the TTL); `--all` widens to idle + archived.

```sh
python3 -m remote_control work                       # in-flight work, both engines
python3 -m remote_control work --all                 # include idle + archived
python3 -m remote_control work --engine codex        # one engine only
python3 -m remote_control work --repo your-app       # one repo (case-insensitive)
python3 -m remote_control work --ttl 86400           # treat <24h as still active
python3 -m remote_control work --json                # machine-readable rows
```

`work stale` is the second view: idle work that looks *blocked or waiting* (so it
won't progress without a nudge), each with the reason —

```sh
python3 -m remote_control work stale                 # what's stuck, and why
python3 -m remote_control work stale --engine codex  # …one engine; --repo/--json too
```

Reasons are the unambiguous ones only: **usage-limit paused** (reusing the
usage-limit detectors), **disconnected** (the bridge server is gone), and
**awaiting response** (Claude `requires_action`, or a Codex rollout whose last
turn is the user's — the agent never replied). A merely-quiet `running` worker is
*not* flagged: from the API a hung worker and a long tool call look identical.

`work move` is the third view: move work between engines. The two directions are
asymmetric because the platforms are —

```sh
python3 -m remote_control work move --from codex --match security --limit 2   # dry-run
python3 -m remote_control work move --from codex --id 019e4d6e --write        # convert
python3 -m remote_control work move --from claude --list                      # what's in Codex
python3 -m remote_control work move --from claude --id <session-id>           # import status
```

- **Codex → Claude** is automatable: it delegates to the `codex-import` converter
  (dry-run by default; `--write` to create the resumable Claude session).
- **Claude → Codex** is an *app-only* feature ("import external agent session" in
  the Codex app), so this direction can't perform the move — it resolves the
  Claude session file and reports whether Codex has **already** imported it.

`work start` is the fourth view: trigger fresh agent work from a prompt. It is
**dry-run by default** (triggering work is outward-facing and hard to undo) —
it prints the exact command + cwd and spawns nothing; pass `--go` to launch a
detached run.

```sh
python3 -m remote_control work start --engine codex  --repo your-app "fix the failing tests"
python3 -m remote_control work start --engine claude --dir "$PWD" "summarize the README" --go
```

- **Codex** → `codex exec "<prompt>"`.
- **Claude** → headless `claude -p --permission-mode bypassPermissions "<prompt>"`
  (the same permission posture the supervisor uses; there is no API to create a
  *cloud* session from a prompt, so this is a local run — the global perm-gate
  hook still vets every tool call).

Together these are the four pieces of the cross-engine work-orchestration goal
(**trigger / inventory / stale-detect / migrate**).

## Importing Codex sessions into Claude Code

`codex-import` converts Codex threads (rollout JSONL under `~/.codex/sessions`)
into resumable Claude Code sessions (under `~/.claude/projects/<encoded-cwd>/`),
so a chat you started in the Codex app shows up in `claude --resume` for that
repo. It keeps the clean user/agent conversation and drops Codex's reasoning +
raw tool-call/output records — replaying those is what breaks Claude's resume
validation. Dry-run by default; pass `--write` to create files.

```sh
python3 -m remote_control codex-import --list                       # browse all Codex threads
python3 -m remote_control codex-import --list --limit 10            # ...10 most recent
python3 -m remote_control codex-import --cwd /path/to/Repo --write  # all of one repo's threads
python3 -m remote_control codex-import --match 'security|pen test'  # dry-run a title filter
python3 -m remote_control codex-import --id 019e4d6e --id 019e4d74 --write  # specific threads
python3 -m remote_control codex-import --include-archived --limit 5 # include archived sessions
```

Each converted session lands as a fresh `<uuid>.jsonl`; re-running `--write`
makes a new copy rather than updating, so delete the old file first if
re-importing.

## Session organization

```sh
python3 -m remote_control sessions                 # live sessions, annotated repo + host
python3 -m remote_control titles list              # preview the nickname-prefix retitling
python3 -m remote_control titles apply --only ai-harness   # apply (idempotent)
python3 -m remote_control titles set --id <sid> "…"        # one session
python3 -m remote_control fork --id <sid>          # clone a Claude session into a local sibling
```

## Install / manage the services

```sh
python3 -m remote_control install                  # install or reload all services
python3 -m remote_control install <plist-label>    # just one service
launchctl kickstart -k gui/$(id -u)/<plist-label>  # restart a service now
tail -f logs/manager.log                           # watch the claude supervisor
tail -f logs/usage-limit-monitor.log               # watch the usage-limit monitor
launchctl list | grep claude                       # status
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/<plist>.plist  # disable
```

Prerequisite: each dir must have had `claude` run once interactively to accept
the workspace-trust dialog, or remote sessions there stall on the trust prompt.

### Adding a new host

Plists are per-user (`com.<user>.<service>.plist`) and `install` (no args) only
picks the current user's plists, so a new machine needs both files **and** an
`AGENTS` entry — otherwise `install` silently skips them.

1. Add `com.<user>.claude-remote-control.plist`,
   `com.<user>.claude-usage-limit-monitor.plist`, and
   `com.<user>.claude-titles-monitor.plist` at the repo root (copy from an
   existing triple and rewrite the user-specific paths in `PYTHONPATH`,
   `REMOTE_CONTROL_*`, `StandardOutPath`, `StandardErrorPath`).
2. Append all three filenames to `AGENTS` in `remote_control/installer.py` —
   `plan_install` iterates that list and ignores anything not in it, even when
   passed explicitly.
3. `python3 -m remote_control install` on that host.

All three services are required:
- the **supervisor** runs the per-dir `claude remote-control` servers;
- the **usage-limit-monitor** detects + auto-resumes paused sessions;
- the **titles-monitor** re-applies the `[NICK.host]` session-title prefix
  every `SESSION_TITLE_APPLY_SECS` (default 600s) — without it, the app's
  auto-titler strips the prefix and sessions stop grouping by repo. Confirm
  with `grep "titles: re-applied" logs/titles-monitor.log`.

### Rollback (titles-monitor misbehavior)

Bootout both monitors, then `git revert bca75fb 0483ddb` to restore the
in-monitor title-pass — the order restores `titles_interval` on
`UsageLimitConfig` first, then re-introduces the loop branch that reads
it. Reinstall and bootstrap the usage-limit-monitor. The titles-monitor
plist can stay installed but unbootstrapped (or remove it from `AGENTS`
in `installer.py` if you want `install` to skip it on the next run).

## Tests

Stdlib `unittest`, no third-party deps, runs on `/usr/bin/python3`:

```sh
python3 -m unittest discover -s tests -t .
```

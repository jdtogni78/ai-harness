# Slot-pool scripts

A pair of shared environment **pools** — one for dev/preview, one for tests —
that let concurrent agent sessions claim isolated slots of one app's stack
without spinning up a heavy per-worktree stack each time.

The implementation is split into a project-agnostic core, a stack-specific
adapter, and a YAML config that names the app:

```
scripts/pool/
├── pool.sh                 dispatcher; reads `adapter:` from config and execs ↓
├── testpool.sh             dispatcher; same, for the testpool side
├── adapters/
│   ├── laravel-docker-pool.sh        Laravel + Docker dev/preview pool
│   └── laravel-docker-testpool.sh    Laravel + Docker test pool (per-slot DB)
├── pool-core.sh            sourced library: lease table + lock + slot math
├── load-config.sh          sourced helper: YAML config → shell vars
├── _yaml_to_env.py         internal: emits shell-evalable lines from a YAML
├── config.example.yaml     template — copy and edit
├── config.local.yaml       gitignored; either a real file or a symlink
└── link.sh                 wire `~/.<state-dir>/` symlinks back to this repo
```

## Pools

| Dispatcher | Pool | Default slots | Skill |
|---|---|---|---|
| `pool.sh`     | dev / preview (shares the dev DB) | `pool0..pool5` (ports 3020–3025) | `lease-env` |
| `testpool.sh` | test (isolated per-slot DBs)      | `test0..test9` (ports 3010–3019) | `test-env`  |

Slot counts, offsets, and the prefix come from the config file.

## Config

`pool.sh` and `testpool.sh` resolve a single config file in this order:

1. `$AI_HARNESS_POOL_CONFIG`
2. `scripts/pool/config.local.yaml` (gitignored)

The config picks the adapter and supplies every value that was previously
hardcoded — DB prefix, state dir, main checkout path, container prefix,
compose service names, baseline backup subpath, slot/offset arrays, etc. See
[`config.example.yaml`](config.example.yaml).

The expected shape today is a Laravel+Docker app; the only shipped adapter is
`laravel-docker`. A different stack would add `adapters/<name>-pool.sh` +
`adapters/<name>-testpool.sh` and select it via `adapter: <name>` in the
config.

Secrets in the config (`db.password`) should NOT live in a shared repo
without encryption — keep the file local or SOPS-encrypt it.

## State

Code is vendored here; runtime state stays in `$STATE_DIR` (per the config —
e.g. `~/.app-one-pool/`), survives worktree churn, and is never committed:

- `pool.tsv`, `testpool.tsv` — lease tables
- `pool.lock/`, `testpool.lock/` — `mkdir` mutexes
- `testdb/` — pool-owned MariaDB datadir
- `warm/` — branch-agnostic vendor/build cache

`link.sh` symlinks the dispatchers from `$STATE_DIR/pool.sh` and
`$STATE_DIR/testpool.sh` back to this repo so every caller (skills, agents)
finds the same code.

## Activate / update

```bash
scripts/pool/link.sh   # run from a STABLE checkout, not a .claude worktree
```

Idempotent. The first run backs up any existing real file to
`$STATE_DIR/<name>.bak.<ts>` before replacing it with a symlink. After that,
edit the scripts **here** and the change is live for both pools on this host.

> **Heads-up (works both ways):** once linked, an agent that edits the *live*
> path `$STATE_DIR/<name>.sh` is editing **this repo's working tree** (it's a
> symlink). Such edits land as uncommitted changes on whatever branch this
> checkout has out (usually `main`) — they are NOT lost, but they won't
> persist or reach other hosts until committed here. If you find an unexpected
> dirty `scripts/pool/*.sh`, it's another agent's live edit — commit it (with
> attribution) rather than reverting.

## Common commands

```bash
pool.sh     list | claim [label] | release [slot] | warm | cool | gc
testpool.sh list | claim [label] | snapshot [dev|<slot>] | reseed [slot] \
            | run [slot] [-- args] | tour [slot] [-- args] \
            | release [slot] | dbup | dbdown | warm | cool | gc
```

`gc` reclaims a slot only when its worktree dir is gone **or** its container
is absent — it does **not** detect a still-`leased` slot whose owning agent
has disconnected (a running-but-idle container). Agent-liveness reclamation
is handled by the `resume-work` skill's zombie-lease sweep.

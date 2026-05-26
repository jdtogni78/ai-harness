---
name: test-env
description: >-
  Lease one of N isolated-DB test slots (count + names come from your pool
  config) via `$STATE_DIR/testpool.sh` to run the CURRENT worktree's test suite
  (incl. RefreshDatabase, migrations, coverage, browser tours) against a
  slot-private `<app>_testN` DB freshly restored from a committed baseline. Use
  when the user wants to "run tests on a test env", "run coverage", "run the
  suite in isolation", "claim/release a test slot", "run browser tests against
  a leased env", "reseed my test DB", or asks which test slots are free. ALWAYS
  prefer this over [[lease-env]] for ANY test run: the preview pool shares the
  dev DB and RefreshDatabase would wipe it. Concurrent Claude sessions share
  the slots via a lease table, so always go through this skill.
---

# Lease a test env

A fixed pool of test slots with isolated DBs (configured in your pool config —
e.g. `test0..test9` with ports 3010..3019). Every slot has its own
`<app>_testN` schema (the `db.prefix` from the pool config) on a pool-owned
test MariaDB container; each `claim` restores the slot DB from a committed
baseline so runs are fully isolated from each other and from the shared
`<app>_dev`.

Slot count, port offsets, DB prefix, state dir, baseline path, and container
names all come from the pool config — see `scripts/pool/README.md` and
`scripts/pool/config.example.yaml` for the source of truth.

Helper: **`$STATE_DIR/testpool.sh`** — `list | claim [label] | release
[slot] | run [slot] -- <args> | tour [slot] -- <args> | reseed <slot> |
snapshot [dev|<slot>] | gc`.

## When to use this vs. lease-env

| You want to … | Use |
| --- | --- |
| Click through the UI / show a stakeholder | `lease-env` (preview pool, shared dev DB) |
| Run `php artisan test` / `phpunit` / coverage | **this skill** (isolated DB) |
| Run browser tests (Dusk / Selenium) | **this skill** — `testpool.sh tour` wires the Selenium sidecar |
| Run a migration to test it before merging | **this skill** (do NOT migrate against shared dev) |

If you're tempted to claim a preview-pool slot for a test run, stop — the
preview pool hardcodes the shared `<app>_dev` DB for every slot, so
RefreshDatabase will wipe shared dev data and break every other session.

## To lease a test env

1. **Check the pool first:** `$STATE_DIR/testpool.sh list`. If a row
   shows this worktree already `leased`, reuse it — don't re-claim.
2. **Claim from this worktree's app subdir** (the `app.subdir` from the pool
   config):
   ```bash
   cd <this-worktree>/<app-subdir> && $STATE_DIR/testpool.sh claim "<short label>"
   ```
   This leases the first free slot, restores its DB from the committed
   baseline, brings up the per-slot app container plus its configured sidecars
   (`compose_services` / `sidecar_container_prefixes` in the pool config), and
   reports the app URL. First claim on a fresh worktree builds the image
   (minutes); subsequent claims are seconds.
3. **The shared pool-owned test MariaDB auto-boots** if needed — no separate prep.

## Run the suite

`testpool.sh run` wraps `php artisan test` inside the slot container, with
pass-through args after `--`:

```bash
# default args: --exclude-group=incomplete,needs-data-refactor
$STATE_DIR/testpool.sh run                                # this worktree's slot
$STATE_DIR/testpool.sh run test2 -- --filter=FooTest      # single test
$STATE_DIR/testpool.sh run test2 -- --coverage            # Laravel coverage table
```

For a CLAUDE.md-style coverage summary, call phpunit directly inside the
slot's app container (container name is `<container_prefix>-<slot>` from the
pool config):

```bash
docker exec <container_prefix>-<slot> sh -c "cd /app && \
  ./vendor/bin/phpunit --coverage-text --exclude-group=incomplete,needs-data-refactor" \
  2>&1 | grep -E '^  (Lines|Methods|Classes):'
```

The image has `pcov` baked in (`Dockerfile`), so coverage works without extra
setup. A full-suite coverage run takes many minutes — kick it off in the
background.

## Run browser tours

`testpool.sh tour` stands up a Selenium+Chromium sidecar on the slot's
network and runs `php artisan dusk`. This resolves the
[[project_dusk_pool_infra_gap]] limitation for the test pool. Default
args run the **whole `tests/Browser/` tree**; pass `-- <args>` to
override (e.g. a single test class or group). Screenshots land in the
worktree's `tests/Browser/screenshots/`.

```bash
$STATE_DIR/testpool.sh tour                                      # full suite, auto-claims slot
$STATE_DIR/testpool.sh tour test2 -- --filter=SomeUITest         # one class
$STATE_DIR/testpool.sh tour test2 -- --group=tour                # by phpunit @group
```

## Re-seed a slot mid-lease

If a test run leaves the slot DB in a state you want to throw away without
releasing + re-claiming:

```bash
$STATE_DIR/testpool.sh reseed test2     # slot DB <- committed baseline
```

If the committed baseline itself is stale, rebuild it from dev or a curated
slot state and commit the result (baseline path is `app.test_baseline_subpath`
from the pool config):

```bash
$STATE_DIR/testpool.sh snapshot dev     # then git add <test_baseline_subpath>
```

## Release

`$STATE_DIR/testpool.sh release` from the owning worktree's app subdir
(or `release <slot>` from anywhere). Tears the stack down, marks the slot
free. **Always release when the run is done** — the slot count is a hard cap.

## When all slots are taken

Run `$STATE_DIR/testpool.sh gc` to reclaim leases whose worktree was
deleted or whose container is gone (common — leased slots whose containers
were stopped via `docker stop` look leased in the table). If still full,
`list` shows owners; ask the user which to reclaim.

## Rules

- **Never run tests against `pool.sh` preview slots** — they share the dev DB.
- The test-pool DB container is separate from the dev DB container (both names
  configured in the pool config).
- The slot stack is not a long-lived preview — release after the run.

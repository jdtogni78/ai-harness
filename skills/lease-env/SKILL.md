---
name: lease-env
description: >-
  Lease one of N shared preview environments (slot count + names come from your
  pool config) to run the CURRENT worktree's code against the shared dev DB,
  instead of spinning up a heavy ad-hoc per-worktree stack. Use when the user
  wants to "preview this branch", "see my changes in a browser", "spin up an
  env", "lease/claim/grab a preview env", "release my env", or asks which envs
  are free / who holds what. Concurrent Claude sessions share the pool via a
  lease table, so always go through this skill (never invent a new nickname).
---

# Lease a preview env

A fixed pool of slots (configured in your pool config — e.g. `pool0..pool5`
with ports 3020..3025) replaces ad-hoc per-worktree stacks. All slots share
one dev DB (the `<app>_dev` schema named by the pool config) via
`host.docker.internal:<dev-port>` (no per-slot restore). A lease table at
`$STATE_DIR/pool.tsv` (e.g. `~/.<your-app>-pool/pool.tsv`) records which
worktree owns which slot so parallel sessions don't collide.

Slot count, port offsets, DB names, state dir, and container names all come
from the pool config — see `scripts/pool/README.md` and
`scripts/pool/config.example.yaml` for the source of truth.

Helper: **`$STATE_DIR/pool.sh`** — `list | claim [label] | release [slot] | warm | cool | gc`.

**Branch-agnostic warm cache:** `pool.sh warm` builds the app image once and
seeds `$STATE_DIR/warm/` (vendor + public/build) from the main checkout, then
starts all slots. Every slot's compose override bind-mounts that cache over
`/app/vendor` + `/app/public/build`, so a `claim` from a fresh worktree needs
**no `composer install`** — it's just a container rebind (seconds). This holds
as long as the worktree's `composer.lock` sha matches
`$STATE_DIR/warm/composer.lock.sha` (all current branches do); on a mismatch
`claim` auto-falls back to a per-worktree install and warns. `pool.sh cool`
tears down all free slots (leased ones untouched) but keeps the cache.

## To lease an env (the common ask)

1. **Check the pool first:** `$STATE_DIR/pool.sh list`. If a row shows
   this worktree already `leased`, reuse that URL — don't re-claim. If the warm
   cache is absent (no `$STATE_DIR/warm/vendor`), run `pool.sh warm`
   once first so claims stay fast.
2. **Precondition:** the shared DB must be up — something must publish the
   configured dev host port (normally the `dev` stack's db). If `claim` warns
   it's missing, tell the user to start the `dev` stack; the slot app will 500
   on DB calls otherwise.
3. **Claim from this worktree's app subdir** (the `app.subdir` from the pool
   config):
   ```bash
   cd <this-worktree>/<app-subdir> && $STATE_DIR/pool.sh claim "<short label>"
   ```
   This leases the first free slot, reserves its offset in this worktree's
   `.dc-ports`, writes a shared-DB compose override, bootstraps `vendor/` /
   `public/build` / `.env` if missing (first claim in a fresh worktree runs
   `composer install` — minutes), then `launch_docker.sh <slot> up -d`.
4. **Verify & hand off the URL.** Reachability test (dev-login needs a cookie
   jar; URL-encode the `/` after `dev-login`):
   ```bash
   curl -s -c /tmp/cj -b /tmp/cj -L "http://localhost:<port>/dev-login/<path>"
   ```
   Give the user the plain `http://localhost:<port>` URL.

## Release

`$STATE_DIR/pool.sh release` from the owning worktree's app subdir (or
`release <slot>` from anywhere). Tears the stack down, deletes the override +
per-slot `.env`/`.dc-ports` entry, marks the slot free. **Always release when
the preview is no longer needed** — the slot count is a hard cap.

## When all slots are taken

Run `$STATE_DIR/pool.sh gc` first — it reclaims leases whose worktree
was deleted or whose container is gone. If still full, `list` shows owners
(worktree + branch + claimed_at); ask the user which to reclaim rather than
forcing one.

## Rules

- **Never** hand-roll a nickname / raw `launch_docker.sh` / compose for preview —
  it reintroduces the offset & DB collisions this pool exists to prevent.
- The shared DB is read/write and visible to every slot **and** the `dev`
  stack. Do not run destructive tests or migrations against a leased slot.
- For a pure single-file template tweak, copying the file into the main
  checkout (revert with `git checkout`) is still lighter than leasing — see the
  `worktree-preview-stack` memory.

Background and gotchas live in the `env-pool` and `worktree-preview-stack`
auto-memories. Slot/port/DB/container specifics come from your pool config:
`scripts/pool/config.example.yaml`.

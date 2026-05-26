# Rename: claude-remote-control → ai-harness

**Goal:** rename this repo/dir `ai-harness` → `ai-harness`. The
Claude-specific name is misleading: it already runs both Claude and Codex
infra, and it's growing into a general, all-projects **harness for AI coding
work** — skills, session restartability/usage-limit resume, the per-dir
servers, session renaming, and workflow-management skills — across both Claude
Code and Codex. Decided as issue **#4** (original target `remote-agent-infra`
was superseded by `ai-harness` on 2026-05-23, to match the broader scope).

> **Do NOT rename** the `claude remote-control` **CLI subcommand** (Anthropic's,
> note the space), nor the `remote-control-dirs` skill (it manages that CLI
> feature, not this repo).

## How to run it

Everything is automated by **`scripts/migrate_rename.sh`** — it does the whole
sequence as one unit, idempotently, dry-run first. Run it **from a shell
*outside* the repo** (it `cd`s to `$HOME` and self-copies to `/tmp` so the `mv`
can't pull the script out from under itself), ideally during a **quiet window**
(few/no active Claude sessions in this repo — see Risks).

```bash
# 1. preview (default is dry-run; prints every step, mutates nothing)
bash ~/dev/ai-harness/scripts/migrate_rename.sh

# 2. do it
bash ~/dev/ai-harness/scripts/migrate_rename.sh --apply
```

Run it **on each host** that has a checkout. The **first** host edits + commits
+ pushes the shared repo files and renames the GitHub repo; the **second** host
detects that (`config.py` already points at `ai-harness`), just pulls, moves its
own dir, repairs worktrees, and reinstalls. Safe to re-run — every step checks
whether it's already done.

### launchd labels: kept by default

By default the script **keeps** the launchd labels
(`com.user.claude-remote-control`, `com.user2.claude-remote-control`) and
only rewrites the *paths* inside the plists. Rationale: labels are invisible
internal IDs; renaming them adds churn (plist-file renames, `installer.py`
`AGENTS` + `test_installer.py` edits, and a bootout-old/bootstrap-new dance) for
no functional gain. Pass `--rename-labels` to rename them too:

```bash
bash ~/dev/.../migrate_rename.sh --apply --rename-labels
```

(The `com.<user>.claude-usage-limit-monitor` label is always kept — its
`claude-` refers to the product, not this repo.)

## Blast radius (re-mapped 2026-05-23)

This is a **two-host** setup sharing one repo via git:
- **mini** (the Mac Mini) = user `user`, dir `/Users/user/dev/ai-harness`,
  agents `com.user.claude-remote-control` + `com.user.claude-usage-limit-monitor`.
- **note** (the MacBook) = user `user2`, dir `/Users/user2/dev/ai-harness`,
  agent `com.user2.claude-remote-control`.

What the script rewrites (verified: after the run the only `ai-harness`
left in-tree are the launchd labels/filenames, unless `--rename-labels`):
- **Absolute paths** `…/dev/ai-harness` → both plists, `config.py`
  `REPO`, the `remote-control-dirs` skill.
- **GitHub repo** + local `origin` URL (`youruser/ai-harness`).
- **`active-dirs.txt`** basename entry `claude-remote-control@…` (fail-closed —
  see Risks).
- **Prose**: README H1, package docstring, the skill board-maps that name the
  repo in backticks (`start/resume/close-work`, `list-tickets`, …).
- **The dir move itself** + `git worktree repair` for **all worktrees**
  (~22 `.claude/worktrees/bridge-cse_*` as of 2026-05-23, each with an absolute
  gitdir pointer into the old path).

## What the script does (sequence)

1. Stop this host's launchd daemons (`launchctl bootout`).
2. Rename the GitHub repo (first host only; idempotent via `gh repo view`) and
   `git remote set-url origin`.
3. Second host only: `git pull --ff-only` the already-migrated content.
4. `mv ~/dev/ai-harness ~/dev/ai-harness`.
5. `git -C …/ai-harness worktree repair` (fixes every worktree pointer).
6. First host only: rewrite in-repo path/name refs (precise, collision-safe
   subs), print a leftover audit, then commit + push.
7. `python3 -m remote_control install` from the new path (re-copies this host's
   plist, re-bootstraps).
8. Verify: worktrees rooted under the new path, the agent is `state = running`,
   tail `logs/launchd.out.log`, run `python3 -m unittest discover -s tests -t .`.

## Risks

- **Renaming the dir while a session runs inside `.claude/worktrees/*`** breaks
  that session's git linkage mid-flight (`worktree repair` fixes it afterward,
  but the live session is disrupted). Do it in a quiet window.
- **`active-dirs.txt` is fail-closed**: between the file edit and the `mv` the
  basename points at a not-yet-existing dir; the script does the edit and the
  move together with daemons stopped, so the supervisor never reads the
  inconsistent state. Don't merge the rewrite commit to `main` *before* the
  `mv` for the same reason.
- **GitHub repo rename** redirects old URLs, so a stale `origin` keeps working,
  but the script updates it anyway.

## Note: the Python package name stays `remote_control`

This rename only changes the **repo/dir/GitHub** name. The Python package is
still imported and run as `remote_control` (`python3 -m remote_control …`,
`PYTHONPATH=…/ai-harness`). Renaming the package is a separate, larger change
(module imports, plist `ProgramArguments`, tests) and isn't needed for #4.

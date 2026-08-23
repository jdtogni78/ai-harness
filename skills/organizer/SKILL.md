---
name: organizer
description: >-
  Loss-safe repo/worktree organization auditor (alias `dream`). Scans a repo
  and its worktrees read-only, RATES every folder A-F on a six-axis rubric
  (duplication · naming · objective signal · loss risk · searchability ·
  reduction), and proposes REVERSIBLE reorg (dedup, rename-for-search,
  tag+index, merge, delete-junk, commit-or-stash orphaned work). Read-only
  through scan → rate → plan → discuss; a backup-before-change GATE blocks any
  apply until the affected paths' backup is confirmed; apply is boss-gated and
  reversible; a final report dovetails with the `report` skill. Use when the
  user says "organize this repo", "audit my worktrees", "rate my folders",
  "clean up / declutter without losing anything", "/organizer", "/dream",
  "find duplicate/orphaned files", or "propose a safe reorg".
---

# /organizer (alias /dream) — loss-safe repo organization audit

You audit how a repo (and its worktrees) is organized, **rate** each folder,
and propose a **reversible** reorganization — without ever losing work. The
skill is deliberately **read-only until an explicit, boss-gated apply phase**,
and even then every change is backed up first and carries a revert recipe.

The tools are **deterministic — no AI, no network beyond `git`** — mirroring the
`session-inventory.py` pattern. AI (you) does the judgement: reading the ratings,
shaping proposals, routing them to domain managers, and narrating the report.

## Phase model (loss-safe by construction)

```
scan → rate → plan → discuss → backup → apply → report
(1 RO)  (2 RO)  (3 RO)  (4 RO)   (5 GATE) (6 boss) (7)
```

Phases 1-4 **mutate nothing**. Phase 5 writes only to a backup dir. Phase 6
(apply) refuses to run until phase 5's backup is **confirmed** and only touches
paths the boss approved. Never skip ahead — you cannot apply before you've
scanned, rated, planned, discussed, and backed up.

## Tools

All live in `scripts/` next to this file. Run with `python3` / `bash`.

| Tool | Phase | Reads | Writes | Purpose |
|------|-------|-------|--------|---------|
| `fs-inventory.py` | 1 | filesystem + `git` (RO) | JSON (stdout/`-o`) | subfolder tree, file size/mtime/class/sha256, git loss-risk signals |
| `dup-detect.py` | 1 | inventory JSON | JSON | exact + near-duplicate groups, per-folder duplicated bytes |
| `folder-rater.py` | 2 | inventory (+dup) JSON | grade JSON / rated report | six-axis rubric → per-folder A-F |
| `safe-backup.sh` | 5/6 | `git` (RO) | backup dir | the reasonable-backup helper **and the apply GATE** |

### 1. Scan (read-only)

Default scope = **ai-harness + its worktrees** (auto-expanded from
`git worktree list`). Broader roots are opt-in per run.

```bash
cd skills/organizer/scripts
# default: ~/dev/ai-harness + worktrees, content-hashed
python3 fs-inventory.py -o /tmp/org-inv.json
# explicit / broader (opt-in): add roots, or scan just one worktree
python3 fs-inventory.py --root ~/dev/ai-harness-172 --no-worktrees -o /tmp/org-inv.json
python3 dup-detect.py -i /tmp/org-inv.json -o /tmp/org-dups.json
```

`fs-inventory.py` records the loss-risk signals verbatim: per-root `uncommitted`,
`untracked`, `stashes`, and the full `worktree list`. `--no-hash` / `--max-hash-mb`
tune the hashing cost; hashing is what makes exact dedup reliable.

**Cross-worktree correction (important).** When the scope spans a repo *and* its
git worktrees, the same file exists at the same relpath in every worktree —
that's how worktrees check out, not waste. `dup-detect.py` classifies every
group as **within-root** (2+ copies inside one root → actionable) or
**cross-root** (spread across worktrees → expected) and headlines the
within-root bytes; pure cross-root groups are dropped by default when >1 root
is scanned (`--all-groups` keeps them). Files are classed doc/script/data/
artifact/**log**/junk — logs are their *own* class (real runtime output, never
auto-deleted), not junk.

### 2. Rate

```bash
python3 folder-rater.py -i /tmp/org-inv.json -d /tmp/org-dups.json          # rated report
python3 folder-rater.py -i /tmp/org-inv.json -d /tmp/org-dups.json --json   # grade JSON
# collapse the 3x-redundant per-worktree rows into one logical row per relpath,
# flagging any folder whose grade actually diverges between worktrees:
python3 folder-rater.py -i /tmp/org-inv.json -d /tmp/org-dups.json --collapse-worktrees
```

The `duplication` axis counts **within-root** duplicated bytes only, so a folder
is never penalized for existing in a sibling worktree. `--collapse-worktrees`
gives the "one logical repo" view: identical folders across worktrees fold into
a single row, and genuine divergence (e.g. one worktree carrying uncommitted
work) is flagged `*DIVERGENT*`.

**Rubric — 0-5 each, composite → A-F** (worst-first in the report):

- **duplication** — fewer duplicated bytes in the folder → higher.
- **naming consistency** — one dominant basename style (snake/kebab/camel/…).
- **objective signal** — a README/index/manifest that explains the folder.
- **loss risk** — inverse of uncommitted/untracked/stash exposure touching it.
- **searchability** — descriptive, non-cryptic basenames (not `tmp`, `foo`, `a`).
- **reduction** — low junk+artifact ratio (not a dumping ground).

Composite ≥4.5 A · ≥3.7 B · ≥2.9 C · ≥2.1 D · ≥1.3 E · else F. Grades are
**signals to reason from**, not verdicts — read the weakest two axes the report
prints per low-grade folder and let that shape the plan.

### 3. Plan (read-only)

From the ratings + dup groups + git signals, draft a **proposal list**. Each
proposal carries a **risk level** and an **owning domain**. Proposal types:

- **dedup** — collapse an exact-duplicate group to one canonical copy.
- **rename-for-search** — give cryptic files descriptive, consistent names.
- **tag+index** — add a README/index so a grab-bag folder gains objective signal.
- **merge** — fold a near-empty or overlapping folder into its sibling.
- **delete-junk** — remove `.DS_Store`, `*.bak`, stray logs (never source).
- **commit-or-stash orphaned work** — the highest-value, highest-risk class:
  uncommitted deltas, untracked new files, orphan worktrees, dangling stashes.
  **Never** discard these — propose commit/stash/PR, never delete.

Present the plan as a skimmable artifact (grade table + proposal list), not a
wall of questions. Mutate nothing.

### 4. Discuss with domain managers (read-only)

Any proposal that touches a domain routes to that domain's owning manager
before it reaches the boss:

```bash
~/.claude/skills/manage/scripts/registry.sh lookup --responsibility <domain>
# → owning active manager's cse_id (exit 4 if none) → message via send-to-session
```

Collect objections, fold them into the plan. If no manager owns the domain,
note it and carry the proposal to the boss unrouted.

### 5. Backup — the GATE, not a suggestion

Before **any** apply, back up the affected root. This is enforced, not advised.

```bash
BKP=$(./safe-backup.sh backup --root ~/dev/ai-harness --label reorg | tail -1)
```

`backup` is itself non-destructive: it captures tracked deltas as a `git stash
create` object (working tree untouched) under `refs/organizer/backup-<stamp>`,
tars untracked/new files (the ones a move/delete could actually lose), and
records `git worktree list` / `git stash list` / `status` / HEAD. It writes a
`manifest.json` + checksum — the pair `verify` trusts.

### 6. Apply — boss-gated + backup-gated + reversible

Apply **only** boss-approved proposals, and **only** through the gate. The gate
runs the apply command **iff** the backup verifies:

```bash
./safe-backup.sh gate --backup-dir "$BKP" -- git mv old_name.py new_name.py
```

If the backup can't be confirmed (missing dir/manifest, checksum mismatch), the
gate prints `GATE-BLOCKED`, exits non-zero, and the apply command **never runs**.
Every applied change has a revert recipe:

```bash
./safe-backup.sh revert --backup-dir "$BKP"          # print the recipe
./safe-backup.sh revert --backup-dir "$BKP" --run    # restore untracked files
```

Rules: source files move via `git mv` (history-preserving); deletes are
restricted to junk classes; orphaned work is committed/stashed, **never**
discarded. One proposal at a time, re-verifying the gate before each.

### 7. Report

Emit **guidances + ratings + applied-change log**. Keep the shape feed-able to
the `report` skill (do not modify `report` here): per-folder grades, the
proposal list with decisions (approved/deferred/rejected), and — for each
applied change — the backup dir + revert recipe. State plainly what was applied
vs. proposed-only, and surface any blocked-by-gate attempts.

## Hard constraints (recap)

- **Read-only through phase 4.** Scan/rate/plan/discuss mutate nothing.
- **Backup is a gate.** No apply runs until its backup verifies (`safe-backup.sh
  gate`). Demonstrably blocks when a backup can't be confirmed.
- **Everything reversible.** `git mv`, backups, revert recipes; orphaned work is
  committed/stashed, never deleted.
- **Default scope = ai-harness + worktrees.** Broader roots are opt-in per run.
- **Route domain-touching proposals** through `registry.sh lookup` before the boss.

## Quick dry run (mutates nothing)

```bash
cd skills/organizer/scripts
python3 fs-inventory.py --root ~/dev/ai-harness-172 --no-worktrees -o /tmp/inv.json
python3 dup-detect.py   -i /tmp/inv.json -o /tmp/dups.json
python3 folder-rater.py -i /tmp/inv.json -d /tmp/dups.json    # → folder grades + call-outs
```

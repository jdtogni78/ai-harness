# Session naming / titles model

Reference for how Claude Code session titles are named, re-applied, and kept
from drifting/doubling. Written after the MGR-12 titles-system fix (2026-07-20).

The API has no folders and `tags` is read-only, so the writable `title` is the
only repo-grouping handle. We render `[NICK.host][SUB...] <desc>` and defend it
against the platform's auto-titler. Code: `remote_control/session_titles.py`.

## The three actors (and where they conflict)

1. **Self-title** — a session sets its own title:
   `python3 -m remote_control titles set --id <cse> --nick <NICK.host> --sub <S> "<desc>"`
   (or `--raw '<verbatim>'`, #111). Managers do this on their first turn.
2. **The app auto-titler** — the Claude Code platform periodically rewrites a
   session's title from its content, **stripping our `[NICK]` prefix** mid-session.
3. **The `titles watch` daemon** — re-applies the prefix every
   `SESSION_TITLE_APPLY_SECS` (default 600s) so the auto-titler can't win.
   Launched by launchd: `~/Library/LaunchAgents/com.dtogni.claude-titles-monitor.plist`
   (label `com.dtogni.claude-titles-monitor`, KeepAlive). Logs to
   `logs/titles-monitor.log`; single-instance lockfile `logs/titles-monitor.lock`.
   `PYTHONPATH` in the plist points at this repo — **the daemon runs whatever
   code is on disk at that path when the process started**.

## Composition (how the prefix is preserved across passes)

`plan_renames` → `extract_sub_tokens(old, nick=token, nicks=nmap.values())` →
`apply_prefix`. Each pass strips our old prefix, decides which leading brackets
are stale NICK occurrences (drop) vs real subs (keep), and re-renders.

- `nick_base("DEV.m5") == "DEV"` — a leading bracket is matched as a NICK
  occurrence by its **base**, not exact string, so a host-suffix flap
  (`DEV` one pass, `DEV.m5` the next) collapses into one slot instead of
  stacking. (#110, commit `3f99b58`.)
- The drop set = the **current nick** + every **configured** nick
  (`nmap.values()`). Real subs (`MGR-3`, `MGR7-W15`, `#66`) never collide
  with a repo nick, so they survive.

### Unconfigured auto-derived stale nick (fixed after #110)

The base/configured-nick collapse alone misses a stale segment whose base is in
neither the current nick nor `nmap`. The live case: one session resolved to
**two different repo names** depending on the derivation source — its git source
URL said `divorce-prep` (auto-derives `DP`) while its local dir is `divorcio`
(auto-derives `DIV`, since single-word repos take 3 letters and hyphenated ones
take initials). Neither `DP` nor `DIV` was configured, so `known` contained
neither, the different-nick collapse couldn't fire, and `[DP.m5][DIV.m5]…`
survived as a bogus sub the daemon couldn't self-heal (only a manual
`titles set` could).

**Two independent fixes, both wanted.** The code fix below makes *any* such
double self-heal. The config fix kills this one at the source: map **every
alias of a repo to the same nick** in `session-nicknames.txt`, so both
derivation sources render the same token and there is no flap to collapse. Do
that whenever a repo's dir name and its git-URL name differ.

Use a **glob key** rather than one line per name:

```
divorcio*=DP
divorce-prep*=DP
```

A feature worktree's dir name becomes its own repo name
(`divorcio-73-familyfund-pipeline` → auto-derives `D7FP`, flapping against
`DP`), and the transcript dir **outlives** the worktree, so the stale name keeps
resolving after the worktree is reaped. Without globs every
`<repo>-<ticket>-<slug>` needs its own hand-added line forever. Precedence is
most-specific-first — an exact key beats a glob, and among globs the longest
pattern wins — so a broad glob can't shadow a deliberate entry. Keys without a
glob character keep pure exact-match semantics.

The widening (`extract_sub_tokens(..., host=<monitor host>)`, threaded from
`plan_renames`): `apply_prefix` emits subs **bare** and only ever suffixes the
NICK bracket with `{host}`, so **any leading bracket rendered as `<base>.<host>`
is provably a stacked prior-pass nick**, whatever its base — drop it. A
`<base>.<other-host>` bracket is left intact (a cross-host claim
`existing_prefix_host` owns; eating it would restart the suffix ping-pong).
Result: a fresh stacked unconfigured-nick title self-heals on the next
**daemon** cycle. Regression tests: `PrefixTest.test_*legacy*` /
`test_daemon_cycle_self_heals_legacy_double_end_to_end`.

## Operating the watcher

```bash
# Is it running / which PID?
launchctl list | grep com.dtogni.claude-titles-monitor
ps -o pid,lstart,command= -p <PID>            # start time = which code it loaded

# Restart it (SIGTERM + relaunch on current on-disk code; refreshes auth token):
launchctl kickstart -k gui/$(id -u)/com.dtogni.claude-titles-monitor

# Dry-run the rename plan / apply one pass by hand (what each cycle does):
python3 -m remote_control titles list          # '~' rows = will-rename
python3 -m remote_control titles apply
```

**Config hot-reloads; code does not.** `apply_prefixes` re-reads
`session-nicknames.txt` (and rebuilds the nickname map) on *every* call, and the
daemon calls it each cycle — so nickname/format edits take effect on the next
cycle with **no restart**. Python code, by contrast, is loaded once at process
start, so a `session_titles.py` change needs a restart.

> **Landing a config change that DEPENDS on a code change? Restart the daemon
> immediately — the asymmetry actively breaks things, it doesn't just fail to
> fix them.** Real example: #126 added glob keys and switched the config to
> `divorcio*=DP`. The config hot-reloaded into a daemon still running
> exact-match code, which couldn't match the `*` key, fell through to the
> auto-derive, and would have retitled every divorce session to `[DIV.m5]` —
> re-creating the exact flap #119 removed. Sequence such changes as
> **merge → `kickstart -k` → verify `titles list` shows `0 to rename`**, inside
> one cycle (600s).

**A stale daemon is the #1 failure mode.** The process loads code at start; a
merge to `session_titles.py` does **nothing** until the daemon is restarted. In
July 2026 the live daemon had been up since Jul 9 and so ran pre-#110 code that
stacked a bracket per pass; it also went `401 (token stale?)` and skipped
cycles for hours. `kickstart -k` fixed both. Check the process start time
against the fix commit date whenever titles misbehave.

## Naming gotchas

- **`--self` works anywhere** (since the env-JWT fallback). It resolves this
  session's own id from the bridge-worktree cwd, and failing that from the
  `CLAUDE_CODE_SESSION_ACCESS_TOKEN` JWT the app injects — so a manager or
  worker anchored in a plain checkout can self-title too. It only errors when
  *both* miss (no bridge cwd and no usable token), and then you pass
  `--id <cse_id>`. Use `--id` for titling **another** session; `--self` for
  your own. (Historical gotcha: before this fallback, `--self` errored with
  "not inside a bridge worktree" from any non-bridge session.)
- **`--nick` takes the rendered host-suffixed form** you want in the bracket,
  e.g. `--nick AH.m5` → `[AH.m5]`. `--sub` may repeat for a chain
  (`--sub MGR7-W15 --sub '#66'` → `[...][MGR7-W15][#66]`).
- **`--raw '<title>'`** pins a verbatim title (#111). Note: a `titles apply`
  pass can still re-render a `--raw`-set title if it computes a plan for it —
  `--raw` is a self-title convenience, not a hard lock against the watcher.

## Manager / MGR-N naming

- Managers carry a `[MGR-N]` bracket. `N` is a stable per-host ordinal
  allocated on the first `mgr-id`/`retitle` call, persisted in
  `~/.ai-harness/manager/ordinals.jsonl`.
- Self-title as the **first turn** via the manage helper:
  `~/.claude/skills/manage/scripts/workers.sh retitle "<task>"` (auto-allocates
  the ordinal). Outside a bridge worktree, fall back to
  `titles set --id <own-cse> --nick <NICK.host> --sub MGR-<n> "<task>"`.
- **Onboarding requirement:** a spawned manager must self-title on its first
  turn — otherwise it lingers as `[NICK.host][<name>] auto-spawned` until a
  human retitles it. Manager-spawn briefs (meta-manage §5) MUST include this
  directive; see `skills/meta-manage/SKILL.md`.

## Spawned workers: don't leave the placeholder

`new-session` titles a spawned session `[NICK.host][SUB] auto-spawned`, where
`SUB` defaults to the server-derived subname. Left alone that row is an orphan:
it carries no manager linkage and its body describes nothing.

**Pass `--task`** whenever you know what you're dispatching — you wrote the
brief, so you do:

```bash
python3 -m remote_control new-session --dir ~/dev/<repo> \
  --prompt-file <brief> --reply-to <self_cse_id> \
  --subname tf-loan-40 --task "#40 TF loan export"
# -> [FE.m5][tf-loan-40] #40 TF loan export
```

`--task` implies `--wait` (the title is PUT against the registered session, so
it needs the cse_id). Omitted/blank falls back to `auto-spawned`.

## Manager ordinals (fixed in #129)

An ordinal is **allocated**, never asserted. The only sanctioned path:

```bash
~/.claude/skills/manage/scripts/workers.sh retitle "<task>"   # calls mgr-id
```

`mgr-id` allocates under a lock and records the claim in
`~/.ai-harness/manager/ordinals.jsonl`. **Never write an ordinal into a spawn
brief** and never run `titles set --sub MGR-<n>` by hand — `titles set` now
warns when it sees a bare `MGR-<n>` the allocator didn't issue (`workers.sh`
sets `MANAGER_ORDINAL_ALLOCATED=1` to mark the sanctioned call).

### What was wrong before

> Every `[MGR-N]` in the system was a human/agent **assertion**. No code path
> called the allocator; ordinals were hand-written into briefs and the session
> took the number.

All observed, and all now fixed:

- **Vestigial ledger** — it stopped at ord 8 (2026-07-18) while MGR-9…MGR-12 ran
  titled with zero records, so "not in the ledger" proved nothing.
- **Two competing sources of truth** — brief text vs the ledger, with the brief
  winning. Worse, the two drifted apart entirely: one session held ord 3 in the
  ledger while titling itself `[MGR-1]`, another held ord 5 while titling
  `[MGR-2]`. Ledger ordinals and title ordinals were disjoint namespaces, each
  with duplicates.
- **TOCTOU allocator** — `prior=max(.ord); ord=prior+1; append`, no lock. In a
  12-way concurrent test the old code produced **2 distinct ordinals out of 12**.

### The fix

- **Atomic allocation.** `mkdir`-based lock (macOS ships no `flock(1)`), plus
  double-checked locking so concurrent calls for the *same* manager yield one
  record. Same 12-way test now yields 12 distinct ordinals.
- **Reconciled ledger.** Titles are what `[MGR` filters read, so the ledger was
  reconciled *to the live titles*. Where a live session's claim collided with a
  disconnected holder, the **live claimant keeps the number** and the legacy
  record is marked `retired_at`/`superseded_by` — kept, not deleted, so `max()`
  never recycles an ordinal. `_ordinal_record` skips retired records, so a
  superseded session that reconnects gets a *fresh* ordinal instead of
  colliding.

With allocation atomic and single-source, #128's `[MGRn-Wm]` linkage half is
unblocked.

### Ordinals are per-host; titles are global

The ledger lives in `$HOME/.ai-harness/manager` — **per machine**. Titles live
in the cloud and are visible from every host. So each host allocates from its
own `max(.ord)+1`, and **two hosts can independently mint the same `[MGR-N]`**;
worker tags inherit it, so `MGR13-W1` could exist on both. New records carry a
`host` field so a cross-host duplicate is at least auditable after the fact.

What saves you in practice is that the full title carries the host in the nick
bracket — `[DEV.m5][MGR-13]` vs `[DEV.mini][MGR-13]` are distinguishable. But
the **linkage token alone is not**, so any consumer grouping by ordinal
(`sessions --json` filtering on `[MGR`) must key on **host + ordinal**, never the
ordinal alone.

Corollary: **allocate an ordinal on the host that owns the session.** Running
`mgr-id` here for a session on another host writes the claim into the wrong
ledger *and* reads the wrong `max()` — you cannot even see the other host's
ledger from here. Ask that host's dispatcher to allocate its own.

### Linkage must be in a bracket

A title can carry a correct `[NICK.host]` prefix — so `titles list` reports
`0 to rename` — while its linkage sits loose in the description
(`[DP.m5] MGR7-W20 — …`). `extract_sub_tokens` returns `[]` and any `[MGR`
filter misses it: the orphan failure #128 prevents, hiding inside a title the
checker calls clean. `titles list` now reports these separately as
**UNBRACKETED linkage** (a rename pass does *not* fix them; retitle with
`--sub`). A title whose own linkage is correctly bracketed may still mention
another manager in prose — that's not flagged.

**Title PUTs work on disconnected sessions.** A title write is metadata; it does
not require the session to be connected. A `disconnected` row can still be
retitled — only *submitting turns* into it fails.
</content>
</invoke>

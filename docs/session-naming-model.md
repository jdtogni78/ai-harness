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
</content>
</invoke>

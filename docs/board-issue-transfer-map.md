# Board-issue transfer map (archive → ai-harness)

The original Remote Control board (user-level project #2) contained issues
from both `jdtogni78/ai-harness-archive` (the renamed pre-public-fork
private repo) and `jdtogni78/ai-harness` (the current public portfolio
repo). To consolidate, the 25 **open** archive issues were **recreated** on
`jdtogni78/ai-harness` — comment chains, commenter identities, and the
old archive URLs do **not** redirect. This file is the only forward link
from a legacy `archive#N` reference to its new home.

The 13 **closed** archive issues were intentionally not migrated; reach
them at `https://github.com/jdtogni78/ai-harness-archive/issues/<N>`
(private, owner-only).

9 of the 25 new bodies were lightly edited under "R1" for cross-reference
integrity: each one carried inline `#X` references that, after migration,
silently re-resolved against the new repo (ai-harness) instead of the
archive. Those references were rewritten to either the new ai-harness `#M`
or, for refs to closed-and-not-migrated archive items, the explicit
`jdtogni78/ai-harness-archive#X` cross-repo form. Each rewritten issue
carries a one-line audit footer listing the substitutions applied. The 9:
#47, #49, #53, #55, #56, #57, #58, #67, #68.

| archive#N | title | new ai-harness#M |
| --- | --- | --- |
| 5 | Resume mechanism: decide 'true retry' vs injected continue; confirm anthropic_cloud parity | [#44](https://github.com/jdtogni78/ai-harness/issues/44) |
| 6 | Codex approval rules: make force-push prompt again (normal push stays allowed) | [#45](https://github.com/jdtogni78/ai-harness/issues/45) |
| 7 | Optional: report Claude.ai not GC-ing dead env_* entries from Remote Control dropdown | [#46](https://github.com/jdtogni78/ai-harness/issues/46) |
| 12 | [AH] Verify Codex auto-resume end-to-end against a real usage-limit pause (item 3 of #3) | [#47](https://github.com/jdtogni78/ai-harness/issues/47) |
| 14 | Unify secret management across harness repos — SOPS + age (cross-host: 2 Macs + Linux) | [#48](https://github.com/jdtogni78/ai-harness/issues/48) |
| 15 | [ai-harness] Provision macmini as a SOPS age recipient (cross-host rollout) | [#49](https://github.com/jdtogni78/ai-harness/issues/49) |
| 16 | [AH] Agent-liveness slot reclamation: `gc --agents` for dev + test pools | [#50](https://github.com/jdtogni78/ai-harness/issues/50) |
| 17 | [AH] Cross-host portability: parameterize hardcoded paths in pool scripts | [#51](https://github.com/jdtogni78/ai-harness/issues/51) |
| 19 | monitor: usage-limit monitor 401s on stale OAuth token (+ socket timeouts) — refresh handling | [#52](https://github.com/jdtogni78/ai-harness/issues/52) |
| 21 | [AH] Repo-prefix label-less issue titles ([AH]/[DSA]) for cross-repo board scanning | [#53](https://github.com/jdtogni78/ai-harness/issues/53) |
| 22 | [AH] Manager session-shepherd: finish live answering + guidelines + REVIEW/RESCUE/service | [#54](https://github.com/jdtogni78/ai-harness/issues/54) |
| 24 | [AH] Permission-review UI: triage the perm-gate log, classify request types, manage allow/deny/ask | [#55](https://github.com/jdtogni78/ai-harness/issues/55) |
| 25 | [AH] perm-gate enforce hardening: tune static lists, AI-tier enforce, manager-ui surface | [#56](https://github.com/jdtogni78/ai-harness/issues/56) |
| 26 | [AH] Manager executor blocks the monitor tick (synchronous claude -p, up to 20m) | [#57](https://github.com/jdtogni78/ai-harness/issues/57) |
| 27 | [AH] Give the manager executor a reliable way to archive a session (via claude -p) | [#58](https://github.com/jdtogni78/ai-harness/issues/58) |
| 28 | config: claude_bin default still hardcoded to /Users/claudio1/.local/bin/claude | [#59](https://github.com/jdtogni78/ai-harness/issues/59) |
| 29 | [AH] manager-ui: one-click 'Re-analyze stale' action on stuck-session detail | [#60](https://github.com/jdtogni78/ai-harness/issues/60) |
| 30 | [AH] supervisor: clean up MERGED+clean bridge worktrees by archiving their source sessions | [#61](https://github.com/jdtogni78/ai-harness/issues/61) |
| 31 | [AH] triage UNMERGED bridge-cse_01GKTKEfm7DPDj1BVvoSRPLB: 2-col layout worktree with unique content | [#62](https://github.com/jdtogni78/ai-harness/issues/62) |
| 33 | Per-host server-name prefix (mm- on mini, nb- on note) | [#63](https://github.com/jdtogni78/ai-harness/issues/63) |
| 34 | usage-limit-monitor: refresh OAuth token on 401 instead of looping forever | [#64](https://github.com/jdtogni78/ai-harness/issues/64) |
| 36 | [AH] Cross-host session trigger skill (mini→note) — design paused on API gap | [#65](https://github.com/jdtogni78/ai-harness/issues/65) |
| 37 | [AH] manager: guideline eval harness (plan) | [#66](https://github.com/jdtogni78/ai-harness/issues/66) |
| 40 | [AH] manager-eval phase 3: runner with LLM judge (eval run subcommand) | [#67](https://github.com/jdtogni78/ai-harness/issues/67) |
| 41 | [AH] manager-eval phase 4: eval compare A/B command + report | [#68](https://github.com/jdtogni78/ai-harness/issues/68) |

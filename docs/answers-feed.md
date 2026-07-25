# The boss's durable answers feed

When the boss asks a question in a fast-moving Claude Code chat, the manager's
answer scrolls away and can't be found later. The **answers feed** gives every
answer a durable, single, low-noise home so he can always return to it.

Built for issue #140 (worker under MGR-11). Source of truth is an iCloud file;
a pinned inbox session mirrors it for in-app reading; a notification fires when
a new answer lands.

## Three surfaces, one call

`skills/manage/scripts/answers.sh post` does all three atomically so a caller
can never half-post:

1. **iCloud file (source of truth)** —
   `~/Library/Mobile Documents/com~apple~CloudDocs/claude-answers/ANSWERS.md`.
   One file; a `##` section per manager/subject; newest entry first within each
   section; newest-active section on top. Written under a `mkdir`-based lock
   (macOS has no `flock(1)`, mirroring the ordinals-ledger pattern in
   `workers.sh`). Existing entries are never rewritten — only prepended — and a
   re-post of the same Q&A is an idempotent no-op.

2. **Inbox session (in-app mirror)** — a durable, pinned
   `[NICK.host][INBOX] Boss answers feed` session whose *sole* job is to render
   each posted answer as a clean block and nothing else. Its cse_id is stored in
   `~/.ai-harness/answers/inbox.cse`; if it is missing or archived when a post
   runs, the helper recreates it (the iCloud file is the durable backup, so a
   lost session is recoverable, not catastrophic).

3. **Notification** — the helper fires an unconditional macOS desktop
   notification (`osascript`), and the inbox session's standing brief fires one
   `PushNotification` per answer. `PushNotification` reaches the boss's **phone**
   when he is away from all active terminals and correctly self-suppresses (as
   redundant) when a terminal is actively watching. The iCloud file and the
   in-app inbox mirror are both phone-reachable regardless.

## Interface

```bash
answers.sh post --mgr MGR-<ord> --subject "<title>" [--ticket N] \
                --q "<question>" --a "<answer>" [--no-inbox] [--no-notify]
answers.sh file            # print the iCloud file path
answers.sh inbox-id        # print / create the inbox session cse_*
answers.sh inbox-status    # is the recorded inbox session live?
```

`--no-inbox` / `--no-notify` isolate the file logic for tests (see
`tests/test_answers_sh.py`, which runs the real script against a scratch
`ANSWERS_FILE` with no network). Env overrides: `ANSWERS_FILE`,
`ANSWERS_STATE_DIR`, `AI_HARNESS_DIR`, `ANSWERS_NOW`.

## When to post

**Strict scope:** only answers to the boss's **explicit questions** — never
decisions, "done" milestones, or worker status, which would make the feed noisy
and defeat its purpose. Both the [[manage]] and [[meta-manage]] skills require a
post at the moment the answer is surfaced in chat (see each skill's *Posting
answers to the boss's durable feed* section).

## Entry format

```
## MGR-13 — Monarch history-match

### 2026-07-24 14:03 · [MGR-13 / #44]
**Q:** How far back should the Monarch history match go?
**A:** Back to Jan 2019 — …
```

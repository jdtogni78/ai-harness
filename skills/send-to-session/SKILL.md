---
name: send-to-session
description: >-
  Submit a user-turn message into a live Claude Code `cse_*` session via the
  `/v1/code/sessions/{id}/events` API — the same plumbing the usage-limit
  monitor uses to nudge a paused session, exposed for ad-hoc use. Use when the
  user wants to "send a message to session X", "deliver a prompt to a running
  thread", "kick a session", "tell session X to do Y", or asks for a programmatic
  way to inject a turn into another session (their own elsewhere, or another
  agent's). Always check the target session is idle first — submitting into a
  busy worker interleaves with whatever it is currently doing.
---

# send-to-session (submit a user turn to a live `cse_*` session)

The Claude Code code-sessions API accepts wrapped user-turn events at
`POST /v1/code/sessions/{id}/events`. This is the same path the usage-limit
monitor uses to deliver its `resume_message` to a paused session
(`remote_control/usage_limit/monitor.py` → `attempt_resume` →
`submit_user_message`).

This skill exposes that primitive as a stand-alone CLI so a turn can be
delivered into any live session.

## Tool

```
python3 -m remote_control sessions submit CSE_ID (--message TEXT | --stdin) [--dry-run]
```

- `--message TEXT` — single-arg message (good for short prompts).
- `--stdin` — read the message from stdin (use for multi-line).
- `--dry-run` — print the wrapped-event JSON body that *would* be POSTed and
  exit 0. No network call. Always do this first on a new session id.

Auth reuses the usage-limit monitor's keychain OAuth token; no extra setup.

## Interactive workflow (default)

Unless the user already named a target id and a message verbatim, drive this
end-to-end as a guided flow — the agent should NOT silently guess the target.

1. **List candidate sessions.** Run
   ```
   python3 -m remote_control sessions list
   ```
   (add `--repo <name>` if the user named one, or `--location this-host` /
   `--location cloud` to narrow). Show the user the resulting table — title,
   `cse_*` id, repo, host, and the `state : worker=...` line.

2. **Ask the user to pick a target.** Use `AskUserQuestion` with one row per
   plausible candidate (label = `[NICK] <title>` and `worker=<status>`,
   description = the `cse_*` id). Include an explicit "I'll paste a `cse_*` id"
   escape hatch so the user can pick something not in the list.

3. **Ask for the message.** Either:
   - Take the message the user already typed in the conversation, OR
   - Use `AskUserQuestion` for a short message (with sensible preset options
     like `continue`, `please summarize where you are`, `Other` for free-form).
   Multi-line messages → write them to a temp file or pipe via `--stdin`.

4. **Warn if the target is not idle.** Re-check `worker_status` from step 1.
   If it is `running` or `requires_action`, surface this and confirm again —
   the turn will land while the agent is mid-work and may derail it.

5. **Dry-run.**
   ```
   python3 -m remote_control sessions submit <CSE_ID> --message "..." --dry-run
   ```
   Show the user the printed body + target URL.

6. **Confirm and send.** Drop `--dry-run`. On success the CLI prints
   `submitted <CSE_ID> (N chars)`.

## Confirm before sending

Submitting a turn is a side-effecting action on a live thread someone else may
be using. Always confirm the target id and the message text with the user
before running without `--dry-run`. This is *not* like editing a local file —
the turn is delivered to a running agent that will act on it.

## Examples

Send a short nudge to a specific session:

```
python3 -m remote_control sessions submit cse_01ABC --message "continue"
```

Send a multi-line instruction from stdin (here-doc):

```
python3 -m remote_control sessions submit cse_01ABC --stdin <<'EOF'
Update the README to mention the new --stdin flag, then run the unit tests.
EOF
```

## When NOT to use this skill

- **A paused-on-usage-limit session.** The usage-limit monitor already detects
  and auto-resumes these; manually nudging steps on its backoff.
- **A session this agent is currently inside.** Use the normal reply path.
- **An archived session.** It won't accept events.

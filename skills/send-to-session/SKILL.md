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
python3 -m remote_control sessions submit CSE_ID (--message TEXT | --stdin) \
    [--reply-to CSE_ID | --no-reply-to] [--dry-run]
```

- `--message TEXT` — single-arg message (good for short prompts).
- `--stdin` — read the message from stdin (use for multi-line).
- `--dry-run` — print the wrapped-event JSON body that *would* be POSTed and
  exit 0. No network call. Always do this first on a new session id.
- `--reply-to CSE_ID` — explicit sender id. Overrides the env-var auto-detect.
- `--no-reply-to` — skip the sender-id header entirely (e.g. when nudging
  your own paused session, where no reply is expected).

**Sender-id header (default on).** The CLI prepends a single line —
`[from <sender-cse_id> — reply via send-to-session]` — to the outgoing message
so the receiving agent knows which `cse_*` to reply back into. The sender id
is read from the `CLAUDE_CODE_SESSION_ACCESS_TOKEN` JWT the desktop app
injects into every spawned process. If that env var is missing, the CLI logs a
warning and submits without the header — pass `--reply-to` to be explicit.

Auth reuses the usage-limit monitor's keychain OAuth token; no extra setup.

## Programmatic manager dispatch (skip the interactive flow)

When this agent is acting as a **manager** that already knows exactly which
session it is addressing — typically because the agent itself just spawned
the target via [[new-session]] and has its `cse_*` id in hand — skip the
interactive list / pick / confirm flow below. The dispatch is unambiguous,
and the friction of a 6-step guided send doesn't add safety to a call the
manager has already decided to make.

The whole spawn-and-brief sequence can be done in **one** CLI call via
`new-session --prompt`/`--prompt-file`, which auto-detects the manager's
own `cse_*` from its `CLAUDE_CODE_SESSION_ACCESS_TOKEN` and submits the
first turn after the worker registers. See [[new-session]]'s "Manager /
worker pattern" section. Prefer that over `new-session` + `send-to-session`
as two calls; the composed flow eliminates the registration race and the
manual id-scrape that broke earlier dispatches.

A follow-up message to an already-spawned worker (i.e. after the first
turn has landed) is still a `sessions submit` call, but skip the
interactive picker — go straight to `--dry-run` then `--message`/`--stdin`
on the known id. The "confirm before sending" rule still applies if the
prompt could derail in-progress work; for a worker idle and waiting on you
specifically, brevity is the right call.

## Interactive workflow (default for human-driven sends)

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

## Replying to a coordination message (REQUIRED)

If the user turn you just received is itself a coordination message from
another agent — i.e. it says things like *"from the &lt;nick&gt; session"*,
*"please reply here when done"*, *"ack receipt"*, or otherwise asks you to
report a status back to a specific `cse_*` id — **you MUST reply using this
skill, not by just ending your turn**.

The reason: the natural model reply lands only in *your* session's
transcript. The sending agent is in a different session and is **not**
subscribed to yours; from their side, your natural reply is invisible. The
only way they see a response is if you turn around and submit a user-turn
event into their session via this skill.

Procedure for the receiving agent:

1. **Find the sender's `cse_*` id.** Three sources, in order of reliability:
   - **`REMOTE_CONTROL_REPLY_TO` env var.** When the worker was spawned via
     [[new-session]] (with `--reply-to` or the auto-detect default), the
     manager's id is injected into the worker process's environment and
     survives any user-side edit of the prompt body. Check this first —
     it's the most reliable source.
   - **`[from cse_XXX — reply via send-to-session]` header.** When the
     send-to-session CLI was used on the sending side, the body opens with
     this line. Read the id from there.
   - **Prose in the message.** The sender may have quoted the id inline, or
     you may have to ask the user to confirm/paste it. Do not guess across
     multiple candidates.
2. **Wait for your in-flight work to settle** before replying, so the reply
   reflects a final state, not an intermediate one.
3. **Compose the reply as a status report** — what you did, what verified,
   any blockers. Be concrete: file paths, pid changes, log excerpts.
4. **Confirm with the user** (per the "Confirm before sending" section
   above) — even when replying to another agent, this is still a
   side-effecting send into a live thread.
5. **Dry-run, then submit.** Same `--dry-run` then `--stdin` flow as a
   first-time send. The submit CLI will auto-embed your own session id as
   the new `[from ...]` header so the round-trip is symmetric.

A coordination thread that uses this skill **on both sides** behaves like a
real conversation between sessions. One that uses it on only one side
silently strands the initiating agent waiting for a reply they will never
see.

## When NOT to use this skill

- **A paused-on-usage-limit session.** The usage-limit monitor already detects
  and auto-resumes these; manually nudging steps on its backoff.
- **A session this agent is currently inside.** Use the normal reply path.
- **An archived session.** It won't accept events.

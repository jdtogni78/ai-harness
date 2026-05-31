# Inter-session communication

How one ai-harness component talks to a live session somewhere else — the
endpoint, the auth, the wrapped body, and the user-facing commands.

## One endpoint

```
POST https://api.anthropic.com/v1/code/sessions/<cse_id>/events
```

Every "deliver a turn into another session" path goes through this:

- the usage-limit auto-resume monitor,
- the session manager's answer submission,
- the `send-to-session` skill / `sessions submit` CLI.

No socket, no MCP server, no broker. One HTTPS endpoint.

## Library

`urllib.request` from the Python stdlib. No `anthropic` SDK, no `requests` —
the package is stdlib-only so it boots from launchd's bare environment with no
venv.

## Headers

```python
{
  "Authorization": f"Bearer {token}",
  "anthropic-version": "2023-06-01",
  "anthropic-beta": "oauth-2025-04-20",
  "content-type": "application/json",
}
```

## Auth

OAuth bearer read fresh each cycle from the macOS Keychain:

```bash
security find-generic-password -s 'Claude Code-credentials' -w
# then JSON-parse: ["claudeAiOauth"]["accessToken"]
```

Never logged. The always-on `claude` servers keep it refreshed. No env var or
`.env` file — the desktop app already wrote it to Keychain.

## Wrapped event body

`remote_control/usage_limit/detect.py` → `resume_event_body`:

```json
{
  "events": [{
    "event_type": "user",
    "source": "client",
    "payload": {
      "type": "user",
      "message": {
        "role": "user",
        "content": [{"type": "text", "text": "<your message>"}]
      }
    }
  }]
}
```

## Endpoints used by the client

All under `https://api.anthropic.com/v1/code`:

| Verb | Path | Used for |
|---|---|---|
| `GET` | `/sessions?limit=100` | list — manager classify, monitor scan |
| `GET` | `/sessions/<cse_id>` | re-fetch one session (verify-after-resume) |
| `POST` | `/sessions/<cse_id>/events` | **inter-session user turn** |
| `POST` | `/sessions/<cse_id>/archive` | archive after fork-and-replace |

## Commands

```bash
# dry-run: print the wrapped JSON that would be POSTed
python3 -m remote_control sessions submit cse_01ABC --message "continue" --dry-run

# live: drop a turn into another session
python3 -m remote_control sessions submit cse_01ABC --message "continue"

# multi-line via stdin
python3 -m remote_control sessions submit cse_01ABC --stdin <<'EOF'
Update the README to mention the new flag, then run the unit tests.
EOF

# explicit sender (overrides auto-detect)
python3 -m remote_control sessions submit cse_01ABC --message "ack" --reply-to cse_01XYZ

# the usage-limit monitor uses the same path internally
python3 -m remote_control usage-monitor --once
```

## The `[from cse_…]` reply convention

The submit CLI prepends one line to the outgoing message:

```
[from cse_01ABC — reply via send-to-session]
```

The receiving agent reads its own sender id from the
`CLAUDE_CODE_SESSION_ACCESS_TOKEN` JWT the desktop app injects into every
spawned process, and replies *back* via the same `sessions submit` into the
sender's `cse_id`.

Without this round-trip, the natural model reply only lands in the receiver's
transcript — each session is its own subscription, so the originator never
sees it.

## Idle-check before sending

Submitting into a `running` or `requires_action` worker interleaves with
whatever it's doing. List first, check `worker_status` on the target row, then
submit:

```bash
python3 -m remote_control sessions list --repo ai-harness
# look for `state : worker=idle` on the target, then submit
```

## Open blocker

How to submit a *structured choice* to an `AskUserQuestion` is unresolved: two
body shapes return `200` but don't actually resolve the option. The session
manager runs its investigator and posts a regular user-turn event with the
chosen text instead, gated behind dry-run while the right shape gets nailed
down. See [session-manager-cases.md](session-manager-cases.md).

---
name: rename-sessions
description: >-
  Rename Claude Code session titles in bulk with a per-repo nickname prefix
  (e.g. `[MYAPP] ...`, `[AH] ...`) so chats from the same repo cluster together
  in the app's session list. The code-sessions API has no groups/folders and
  `tags` is read-only, so the writable `title` is the only repo-grouping handle.
  Use when the user wants to "group/organize chats by repo", "prefix session
  titles", "rename my sessions", "shorten the project names in the session
  list", "use nicknames for projects", or wants a single session re-titled as
  its work shifts.
---

# Rename sessions (repo-nickname title prefixes)

The Claude Code code-sessions API (`/v1/code/sessions`) has **no groups or
folders**, and the session `tags` field is **read-only** (a `PUT` with `tags`
returns 200 but never changes it). The session **`title` is writable**
(`PUT /v1/code/sessions/{id}` with `{"title": "..."}`), so a `[NICK] ` title
prefix is the only way to make same-repo chats cluster in the app list.

## Tool

`python3 -m remote_control titles` (from the repo root):

- `titles list` (default) — dry run: print every session, its derived repo, and
  the proposed `[NICK] ` title. **No writes.**
- `titles apply` — `PUT` the changed titles.
- `titles set [--self|--id CSE_ID] "<description>"` — set ONE session's title to
  `[NICK] <description>` (the `[NICK] ` prefix is applied for you). `--self`
  derives the id + repo from the current bridge worktree; this is what
  `start-work` / `close-work` call to track the current ticket.
- `--only <repo>` — limit to one repo (basename, case-insensitive).
- `--map "my-app=MYAPP,foo=BAR"` / `SESSION_TITLE_NICKNAMES` env — extend the map.
- `--dev DIR` — dev root for bridge-worktree repo lookup (default `~/dev`).

Auth reuses the usage-limit monitor's keychain OAuth token; no extra setup.

### How a session's repo is derived (first hit wins)
1. `config.sources[].url` → git repo basename (cloud / CLI-launched sessions).
2. a local bridge worktree `~/dev/<repo>/.claude/worktrees/bridge-<id>` → `<repo>`
   (bridge / app-launched sessions have an empty `config.sources`).

A session with neither is printed as `<unknown repo>` and left untouched.

### Nicknames
`session-nicknames.txt` (repo root) maps `repo=NICK`, one per line. It's the
editable source; built-in defaults cover the active repos, and unmapped repos get
an auto-derived acronym (multi-word → initials `claude-remote-control`→`CRC`;
single-word → first 3 letters `my-cool-app`→`MYC`). To shorten a project, add/edit a
line (e.g. `my-application=MYAPP`) and re-run.

### Prefix template
The bracketed prefix is rendered from a **format template** — the reserved
`format=` line in `session-nicknames.txt`, overridable by the
`SESSION_TITLE_FORMAT` env var. Default `{nick}.{host}` → `[MYAPP.mini]` for a
session on one host, `[MYAPP.note]` on another, `[MYAPP]` for a cloud session.
`{token}` placeholders each resolve from a different source:

| token       | value                                  | when filled            |
|-------------|----------------------------------------|------------------------|
| `{nick}`    | repo nickname (`MYAPP`)                | always                 |
| `{repo}`    | full repo basename (`my-application`)  | always                 |
| `{host}`    | host nickname                          | local bridge sessions  |
| `{branch}`  | the worktree's git branch              | local bridge sessions¹ |
| `{id}`      | full session id (`cse_01ABC…`)         | always                 |
| `{shortid}` | compact id (`cse_` stripped, 8 chars)  | always                 |
| `{engine}`  | agent engine (`claude`)                | always                 |

¹ `{branch}` costs one `git` call per local session (only when the template
actually uses it). Cloud / CLI sessions live on no host, so `{host}`/`{branch}`
are empty there — and an **empty token collapses with one adjacent separator**,
so `{nick}.{host}` is `MYAPP.mini` locally and just `MYAPP` (no dangling dot) in
the cloud. This also stops the two hosts' self-heal passes from fighting over a
shared cloud session (both render the same host-less prefix).

Prefixing is **idempotent**: re-running replaces an existing `[...] ` prefix
rather than stacking, so it's safe to run repeatedly and after the map/template
changes. (The strip window is 64 chars, wide enough for `{id}`/`{branch}`
tokens; combining `{id}` with a very long branch can exceed it, so prefer
`{shortid}` for compact titles.)

## Workflow

1. Run `titles list` and review the proposed renames with the user.
2. Adjust `session-nicknames.txt` (or `--map`) until the nicknames look right.
3. Run `titles apply`. (Bulk write to the user's session list — confirm first.)

## Keeping a title current as work changes

A title is `[NICK] <description>`. The tool only owns the `[NICK] ` slot; the
description is the human part. When a single session's focus shifts, re-title
just that one so the list stays scannable — from inside that session's worktree:

```
python3 -m remote_control titles set --self "<new description>"
```

(or `--id CSE_ID` to retitle another session). The `[NICK] ` prefix is applied
idempotently, so re-running is safe. `start-work` calls this when it claims a
ticket, to keep a session's title matched to the ticket it's on.

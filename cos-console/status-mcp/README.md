# status-mcp — cos-console data plane (W0)

A local, **read-only** MCP server that assembles a real `StatusReport` for a
project from signals this host already has: GitHub Projects boards, Maven test
artifacts, deploy logs, narrate-demo / visual-review artifacts, and decisions
mined from git merges. This is the shared backend every voice POC depends on.

> **Read-only w.r.t. every real system.** It NEVER deploys, migrates, or
> mutates anything. It only reads logs, artifacts, and the GitHub API.

- **Contract:** [`SCHEMA.md`](./SCHEMA.md) + [`status_report.schema.json`](./status_report.schema.json) (frozen v1.0)
- **What's real vs stubbed:** [`FINDINGS.md`](./FINDINGS.md)

## Quick start (CLI probe — no MCP client, no secrets)

The probe is pure stdlib, so it runs out of the box:

```bash
cd ~/dev/cos-console/status-mcp
python3 -m status_mcp.probe --list                 # known projects
python3 -m status_mcp.probe dstrader --pretty      # full StatusReport JSON
python3 -m status_mcp.probe dstrader --validate     # + JSON Schema check (needs jsonschema)
```

`--project dstrader` also works (alias for the positional arg). Any project in
the registry works; `dstrader` is the first real target, `familyfund` is wired
as a second to prove `--project` generalizes.

## Running the MCP server

```bash
python3 -m pip install mcp            # the standard MCP Python SDK (server only)
python3 -m status_mcp.server          # stdio transport
```

Exposes two tools:
- `get_project_status(project) -> StatusReport` — the contract.
- `list_known_projects() -> [str]`

### Wire into an MCP client (e.g. Claude Desktop)
```json
{
  "mcpServers": {
    "cos-status": {
      "command": "python3",
      "args": ["-m", "status_mcp.server"],
      "cwd": "/Users/<you>/dev/cos-console/status-mcp"
    }
  }
}
```

## What it reads (per project, from `status_mcp/config.py`)

| Signal | Source | dstrader |
|---|---|---|
| **tickets** | `gh project item-list <N>`, filtered to the repo | board 1 "Trading & Fund", repo `jdtogni78/dstrader` |
| **tests** | Maven surefire XML + JaCoCo csv/xml under `target/` | `~/dev/dstrader/target/...` (nulls if not run) |
| **deploy** | deploy-log `INDEX.md`, newest row for the target | `~/dev/dstrader-docker/local/deploy_logs/INDEX.md`, target `dstrader-docker` |
| **visual_review** | demos dir: `.mp4` / `.demo.yaml` / `*explainer*.html` | `~/dev/dstrader/demos` |
| **decisions** | `git log --merges` (PR titles) | `~/dev/dstrader` |

## Requirements / auth

- **Python 3.10+** (uses `X | Y` typing). Tested on 3.13.
- **`gh` CLI**, authenticated with the **`project`** + `repo` scopes:
  ```bash
  gh auth status          # confirm login
  gh auth refresh -s project,read:org   # if the project scope is missing
  ```
  Without it, `tickets` degrades to `availability: unavailable` + a warning —
  the rest of the report still works.
- **`git`** on PATH (for `decisions`).
- **`mcp`** (pip) only for the server; **`jsonschema`** (pip) only for `--validate`.

Paths are overridable via env so this runs on another host without code edits —
see [`.env.example`](./.env.example).

## Dev

```bash
python3 -m status_mcp.probe dstrader --validate   # schema-conformance smoke test
```
Individual signals degrade gracefully: a missing/failed collector sets that
section's `availability` to `unavailable` and adds a `warnings[]` entry rather
than failing the whole report.

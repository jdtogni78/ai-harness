# Worker W0 — data-plane (status-mcp)

You are a worker in the **cos-console** exploration. READ `~/dev/cos-console/PROJECT.md`
first — it has the architecture, the SETTLED DECISIONS, the StatusReport schema, and the
sibling roster. Your anchor dir is `~/dev/cos-console/status-mcp`. Stay in it.

## Your job (POC-1, no voice — the shared backend everyone else depends on)

Build a local **MCP server** exposing `get_project_status(project) -> StatusReport` that
assembles a real status report from signals THIS host already has:
- **Tickets** — GitHub Projects via `gh` (there are boards per repo; see the ai-harness
  skills `list-tickets` / `list-sessions` for how boards map to repos).
- **Tests** — count/pass/fail/coverage from a repo's test artifacts (look for junit xml,
  coverage reports, or the `test-env` skill's outputs). If a repo has none, return nulls,
  don't fabricate.
- **Deploy** — last deploy time/commit/status (e.g. dstrader deploys via a deploy script
  — see the operator's memory; do NOT deploy anything, only READ logs/state).
- **Visual review** — did a `narrate-demo` video or visual-review artifact exist?
- **Decisions** — mine `close-work` / handoff briefs and merge commits for decisions.

**You OWN the StatusReport schema.** Start from the v0 stub in PROJECT.md, finalize it,
and write it as `SCHEMA.md` + a JSON Schema file in your dir so the voice workers can
converge. If you change a field, note it in SCHEMA.md.

Pick **dstrader** as the first real target (it has deploys + visual-review history). Add a
`--project` arg so others can point elsewhere.

## Constraints
- Read-only w.r.t. every real system. NEVER deploy, migrate, or mutate prod. This is a
  reporting layer. (Operator's global rule: prod-touching work is gated + MacBook-only.)
- Language: Python (matches the harness). Use the standard MCP Python SDK.
- Must run locally; document any `gh` auth / paths needed in a README + `.env.example`.
- Provide a tiny CLI harness (`python -m status_mcp.probe dstrader`) that prints the JSON
  so it can be tested WITHOUT wiring an MCP client.

## Deliverables
1. Working MCP server + the CLI probe, committed locally (git init your subdir is fine).
2. `SCHEMA.md` + JSON Schema (the contract).
3. `FINDINGS.md`: what real data was reachable vs stubbed, gaps, and what the voice layer
   can rely on today.
4. Report back to the manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via **send-to-session** when
   the schema is frozen (siblings are blocked on it) and again when done. Include a
   "state of my work" line (done / in-progress / decisions).

Do NOT /close-work until the operator OKs via the manager.

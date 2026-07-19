# Worker W4 — surface (Wave 2: the slide / visualization surface)

You are a worker in the **cos-console** exploration. READ `~/dev/cos-console/PROJECT.md`
first (architecture, SETTLED DECISIONS, sibling roster). Anchor dir:
`~/dev/cos-console/surface`. Stay in it.

## Context: the data plane is REAL and frozen

Sibling **w0-data-plane** shipped a working StatusReport source (schema v1.0, FROZEN):
- Contract: `~/dev/cos-console/status-mcp/SCHEMA.md` + `status_report.schema.json`.
- Get real data with NO secrets, pure stdlib:
  `cd ~/dev/cos-console/status-mcp && python3 -m status_mcp.probe dstrader --pretty`
  (`--list` for projects; `familyfund` also works). Use dstrader as your primary demo.

## Your job (the "watch slides while you talk" surface)

Build a local **server-driven React PWA** that renders a StatusReport as a walkable deck
and works on BOTH a desktop big-screen and a phone (responsive/PWA). This is the visual
half of the chief-of-staff: the agent will later push "show slide N / show the coverage
chart" over a websocket; for THIS POC, model that server-driven protocol and drive it
from a small local Node/Python server that reads the probe output.

Slides to generate from a StatusReport:
- Title / project overview (ticket burn: todo/in_progress/done/blocked).
- Tests (count/pass/fail/coverage) — Recharts.
- Deploy status + last deploy.
- Visual-review artifacts list.
- Decisions timeline.
- Open questions.
- Optional: a Mermaid architecture/flow diagram slide.

## CRITICAL — honor the anti-fabrication contract
Read the top-level `availability` map before rendering ANY number. When a signal is
`unavailable` (numeric fields null), render an explicit **"no data" / "not run" state** —
NEVER a zero-value chart that reads as "0 tests" when it means "unknown". This visual
honesty is a core validating-manager requirement, same rule the voice workers follow.

## Constraints
- Local, macOS. `npm`/Vite React is fine. Server-driven state over a websocket (agent →
  UI intents like `{type:"goto", slide:3}` / `{type:"show", widget:"tests"}`).
- Must run locally with `npm run dev` (or a documented script) against real probe JSON —
  no external services, no secrets.
- Follow the operator's `dataviz` skill for chart color/'layout choices.
- Responsive: verify it renders on a phone-width viewport (headless Chromium is fine per
  operator's global rule — headless unless asked).

## Deliverables
1. Runnable PWA + local driver server + README (exact `npm`/run steps).
2. A short `FINDINGS.md`: how the server-driven protocol worked, desktop vs phone notes,
   what the eventual voice-brain needs to emit to drive it.
3. Report to manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via **send-to-session** when done,
   with a "state of my work" line.

## Sibling roster (all Wave 1 workers, for context — don't recompute their scope)
- w0-data-plane  cse_01Ne2x7XcUVVjhgRNqn6GEcM  (StatusReport MCP + probe — your data source)
- w1-voice-pipecat  cse_011S2562UPGrYFAxU62teH2o  (Claude-brain voice loop)
- w2-voice-realtime cse_01F3vpFmr5id7EsAo8xUAp4e  (speech-to-speech voice loop)
- w3-voice-local    cse_01Ws6MKShE21utgeQjbXGDF8  (local voice loop)

If your scope overlaps a sibling, STOP and report to the manager. Do NOT /close-work until
the operator OKs.

# cos-console — Chief-of-Staff Console (exploration)

A **voice-first "chief of staff"** that sits on top of ai-harness. You talk to
it; it talks back, and while it talks it drives a live slide/visualization
surface. Its job is to **report and validate** (tickets, tests, deploys, visual
reviews, decisions) — not to write code. See [`PROJECT.md`](PROJECT.md) for the
full architecture and settled decisions.

Tracked under epic **[#99](https://github.com/jdtogni78/ai-harness/issues/99)**.
Relocated here (from a set of standalone git repos) per **#107** so the whole
exploration has a single version-controlled home. Voice stack is **not yet
picked** — Wave 3 is held; this subtree is a relocation, not new build.

## Component map

All components are siblings under `cos-console/` (relative paths like
`../status-mcp` are load-bearing — keep the layout).

| Dir | What it is |
|---|---|
| `status-mcp/` | **Data plane (W0).** StatusReport MCP over the harness (gh Projects, sessions, git, deploys). Owns the `StatusReport` schema. No voice. |
| `voice-pipecat/` | Voice approach A: Claude-as-brain pipeline (Pipecat STT→Claude→TTS). |
| `voice-realtime/` | Voice approach B: speech-to-speech (OpenAI Realtime), Claude as a tool. |
| `voice-local/` | Voice approach C: fully local/private (Whisper.cpp + Piper + Claude). |
| `surface/` | React PWA surface (desktop + phone), server-driven deck. |
| `presentation-poc/` | Server-driven slide deck generator (reads `../status-mcp`). |
| `briefs/` | Per-wave worker briefs (w0–w4). |
| `docs/` | Verification artifacts (e.g. the deck screenshot below). |

Each component keeps its own `README.md` and `FINDINGS.md`.

## Quick verify

```bash
# Data plane: live status probe
cd status-mcp && python3 -m status_mcp.probe dstrader --pretty

# Deck: generate a self-contained status deck (reads ../status-mcp)
cd presentation-poc && python3 -m deck.generate dstrader
# -> out/dstrader-status.html
```

A rendered deck screenshot from the relocated tree lives at
[`docs/verify-deck-dstrader.png`](docs/verify-deck-dstrader.png).

## What is NOT committed (regenerable / secret)

Governed by `cos-console/.gitignore` (single file for the whole subtree):

- **Secrets** — no `.env`, only `.env.example` per component.
- **Voice models** (`models/*.bin`, `models/*.onnx`) — Whisper/Piper blobs
  (~200MB). Fetch via each voice component's README/setup.
- **Deps / build** — `node_modules/`, `.venv/`, `dist/`, `__pycache__/`.
- **narrate render scratch** — `demos/out/.narrate/`.
- **Generated deck output** (`presentation-poc/demos/out/`, `presentation-poc/out/`)
  — including the two rendered demo MP4s (~1.8MB each). These are ignored by the
  repo-root `.gitignore` `out/` rule (regenerable), so they are **not** committed.

To regenerate a deck: `cd presentation-poc && python3 -m deck.generate <project>`
(writes `out/<project>-status.html`). To regenerate a demo video, run the
narrate-demo tool on the emitted `demos/<name>.demo.yaml`.

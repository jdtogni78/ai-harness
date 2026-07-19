# Worker W3 — voice-local (Approach C: fully local / private)

You are a worker in the **cos-console** exploration. READ `~/dev/cos-console/PROJECT.md`
first (architecture, SETTLED DECISIONS, StatusReport schema, sibling roster). Anchor dir:
`~/dev/cos-console/voice-local`. Stay in it.

## Your job (voice loop, everything local except the brain)

Build a local voice agent where the **audio never leaves the machine**: mic →
**Whisper.cpp** (STT) → Claude (brain, the one network hop) → **Piper** or Kokoro (TTS) →
speaker. This is the privacy-first counterpoint: measure how close local STT/TTS gets to
the cloud approaches on latency and naturalness, and whether it's usable day-to-day.

Claude is the brain and calls ONE tool: `get_project_status(project)`. Until sibling
**w0-data-plane** publishes the real MCP + schema, STUB it against the v0 StatusReport in
PROJECT.md. Do not invent a divergent shape.

## Recommended components
- STT: whisper.cpp (or faster-whisper) — pick a model size that runs real-time on Apple
  Silicon and note which.
- Brain: Claude via Anthropic API.
- TTS: Piper (fast, local) or Kokoro; document voices.

## Constraints
- Local, macOS Apple Silicon. Only Claude needs network. Document brew/pip + model
  downloads. Anthropic key in `.env.example`, never committed.
- Must run end-to-end offline except the Claude call.

## Deliverables
1. Runnable POC + README with exact setup + model choices.
2. `FINDINGS.md`: setup effort, rough latency (STT + TTS local cost on this Mac),
   naturalness, privacy win, and a one-line verdict vs the cloud approaches.
3. Report to manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via **send-to-session** when done,
   with a "state of my work" line.

If your scope overlaps a sibling, STOP and report to the manager. Do NOT /close-work until
the operator OKs.

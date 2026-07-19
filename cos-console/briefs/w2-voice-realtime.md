# Worker W2 — voice-realtime (Approach B: speech-to-speech)

You are a worker in the **cos-console** exploration. READ `~/dev/cos-console/PROJECT.md`
first (architecture, SETTLED DECISIONS, StatusReport schema, sibling roster). Anchor dir:
`~/dev/cos-console/voice-realtime`. Stay in it.

## Your job (voice loop, speech-to-speech, Claude as tool)

Build a local voice agent on a **speech-to-speech Realtime API** (OpenAI Realtime, or
Gemini Live as fallback). Here the Realtime model is the fast conversational I/O; **Claude
is bolted on as a reasoning TOOL** the Realtime model calls for anything substantive — in
particular `get_project_status(project)` (route to Claude for summary/reasoning). This is
the DELIBERATE counterpoint to the Claude-as-brain approach (per SETTLED DECISION #2) so we
can measure the tradeoff: lower latency / better barge-in vs losing Claude as the primary
reasoner.

Until sibling **w0-data-plane** publishes the real MCP + schema, STUB the status tool
against the v0 StatusReport in PROJECT.md. Do not invent a divergent shape.

Target demo: operator says "status on dstrader" → snappy spoken reply; measure how it feels
vs the Pipecat/local approaches. Note explicitly where the GPT-brain limits reasoning
quality and how well the Claude-as-tool handoff works.

## Constraints
- Local, macOS. Needs a Realtime API key — document it in `.env.example`; provide a
  transcript-only / mocked dry-run path so the repo is inspectable without a key.
- Never commit real keys. Document setup.

## Deliverables
1. Runnable POC + README with exact setup.
2. `FINDINGS.md`: setup effort, latency, naturalness/barge-in, cost, the reasoning-quality
   tradeoff of GPT-brain + Claude-as-tool, and a one-line verdict.
3. Report to manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via **send-to-session** when done,
   with a "state of my work" line.

If your scope overlaps a sibling, STOP and report to the manager. Do NOT /close-work until
the operator OKs.

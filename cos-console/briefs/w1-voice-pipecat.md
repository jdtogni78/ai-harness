# Worker W1 — voice-pipecat (Approach A: Claude as brain)

You are a worker in the **cos-console** exploration. READ `~/dev/cos-console/PROJECT.md`
first (architecture, SETTLED DECISIONS, StatusReport schema, sibling roster). Anchor dir:
`~/dev/cos-console/voice-pipecat`. Stay in it.

## Your job (voice loop, Claude-as-brain)

Build a local voice agent using **Pipecat**: mic → STT → **Claude** → TTS → speaker, with
barge-in. Claude is the reasoning brain and does tool-use; it can call ONE tool this POC:
`get_project_status(project)`. Until sibling **w0-data-plane** publishes the real MCP +
schema, STUB the tool against the v0 StatusReport in PROJECT.md (hard-code a sample
dstrader report). Do not invent a divergent shape.

Target flow to demo: operator says "give me a status on dstrader" → agent SPEAKS a concise
spoken summary sourced from the (stub) StatusReport, and can answer one follow-up
("how many tests?").

## Recommended components (swap freely, document what you chose)
- STT: Deepgram streaming (fallback: local Whisper if no key).
- Brain: Claude via Anthropic API / Agent SDK.
- TTS: Cartesia or ElevenLabs (fallback: macOS `say`, already used in narrate-demo).

## Constraints
- Local, macOS. Must at least dry-run WITHOUT paid keys (fall back to Whisper + `say`).
- Keys in `.env.example`, never commit real keys. Document pip/brew + portaudio setup.

## Deliverables
1. Runnable POC (`python -m voice_pipecat` or a run script) + README with exact setup.
2. `FINDINGS.md`: setup effort, rough end-to-end latency, naturalness, barge-in quality,
   cost, and a one-line verdict vs the other two voice approaches.
3. Report to manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via **send-to-session** when done,
   with a "state of my work" line.

If your scope overlaps a sibling (e.g. you find yourself defining the status schema),
STOP and report to the manager. Do NOT /close-work until the operator OKs.

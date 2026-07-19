# cos-console — Chief-of-Staff Console (exploration)

A **voice-first "chief of staff"** that sits on top of the existing ai-harness.
You talk to it (ChatGPT-voice style); it talks back; while it talks it drives a
live slide/visualization surface (desktop big-screen or phone). Its job is to
**report and validate**, not to write code: "Have we tested this? How many tests?
Did it run in prod? Was there a visual review? What decisions were made?" — and to
**capture the operator's notes** back into tickets/memory.

## Architecture (four planes)

- **Surfaces** — React PWA (desktop + phone), server-driven deck (agent pushes
  "show slide N"), charts via Recharts/D3, diagrams via Mermaid.
- **Voice** — mic → STT → brain → TTS → speaker, barge-in capable.
- **Brain** — Claude (Agent SDK / Anthropic API), tool-use decides what to say
  AND what to show.
- **Data** — a "harness-report" MCP wrapping gh Projects, sessions API, git log,
  test/coverage artifacts, deploy logs, close-work briefs, narrate-demo videos.

## This exploration = Wave 1: compare voice approaches head-to-head

Four parallel local POCs, one per subdir. Wave 2 (slide surface PWA) comes after
the data plane lands.

## SETTLED DECISIONS (do not recompute differently)

1. **Project home** is `~/dev/cos-console`, one subdir per POC. Stay in your subdir.
2. **Claude is the brain** wherever reasoning/tool-use is needed (via Anthropic
   API / Claude Agent SDK) — EXCEPT the voice-realtime POC, which deliberately
   tests a speech-to-speech (GPT) brain with Claude as a reasoning *tool*, so we
   can measure the tradeoff.
3. **Shared status contract**: every voice POC calls ONE tool,
   `get_project_status(project) -> StatusReport` (JSON below). The **data-plane
   worker (W0) OWNS the StatusReport schema.** Voice workers STUB it against the
   schema below until W0 publishes the real one — do not invent a divergent shape.
4. **Local-first**: build so it at least dry-runs WITHOUT secrets. Put required
   keys in `.env.example`, never commit real keys. Document brew/pip installs.
5. **Each worker writes `FINDINGS.md`** in its subdir: setup effort, latency
   (rough), naturalness/barge-in, cost, and a one-line recommendation.

## StatusReport schema (v0 stub — W0 finalizes)

```json
{
  "project": "dstrader",
  "generated_at": "ISO8601",
  "tickets": {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0,
              "items": [{"id": "", "title": "", "state": "", "url": ""}]},
  "tests":   {"count": 0, "passing": 0, "failing": 0, "coverage_pct": null,
              "last_run": "ISO8601|null"},
  "deploy":  {"last_deployed_at": "ISO8601|null", "env": "", "commit": "",
              "status": "ok|failed|unknown"},
  "visual_review": {"done": false, "artifacts": []},
  "decisions": [{"when": "ISO8601", "summary": "", "source": ""}],
  "open_questions": []
}
```

## Sibling workers (Wave 1)

| Subname | Subdir | Responsibility |
|---|---|---|
| w0-data-plane   | status-mcp    | StatusReport MCP over the harness (no voice). OWNS the schema. |
| w1-voice-pipecat | voice-pipecat | Approach A: Claude-as-brain pipeline (Pipecat: STT→Claude→TTS). |
| w2-voice-realtime | voice-realtime | Approach B: speech-to-speech (OpenAI Realtime), Claude as tool. |
| w3-voice-local   | voice-local   | Approach C: fully local/private (Whisper.cpp + Piper + Claude). |

If your task starts to overlap a sibling's scope, STOP and report the overlap to
the manager (`cse_01EQxN9fsqss4jJkwCoSjFi5`) via send-to-session instead of
resolving it yourself.

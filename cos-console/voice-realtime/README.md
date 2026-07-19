# voice-realtime — Approach B: speech-to-speech, Claude-as-tool

One of four Wave-1 voice POCs for **cos-console** (see `../PROJECT.md`). This one
deliberately inverts the house style (SETTLED DECISION #2): the **OpenAI Realtime
(GPT) model is the brain** for fast conversational I/O and barge-in, and **Claude
is bolted on as a reasoning tool** (`get_project_status`). The point is to measure
the tradeoff — lower latency / better barge-in vs. losing Claude as the primary
reasoner. Contrast with `../voice-pipecat` (Claude-as-brain) and `../voice-local`.

```
operator speech ─► OpenAI Realtime (GPT) ─► [tool: get_project_status] ─► Claude ─► spoken reply
                        ▲  server VAD + barge-in            │
                        └───────────────────────────────────┘  StatusReport stub (status_report.py)
```

## Layout

| File | Role |
|---|---|
| `main.py` | CLI: `dryrun` (keyless) / `live` (mic+speaker) |
| `realtime_agent.py` | Live OpenAI Realtime WebSocket client + PCM16 audio + barge-in |
| `tools.py` | The single shared tool `get_project_status` (schema + dispatch) |
| `claude_tool.py` | The Claude-as-tool reasoning handoff (real API + no-key mock) |
| `status_report.py` | v0 StatusReport **stub** (owned by w0-data-plane; do not diverge) |
| `dryrun.py` | Transcript-only loop — no audio, no keys, mocks the GPT tool-call decision |

## Quick start (no keys, no mic — inspect the loop)

```bash
python3 main.py dryrun                       # scripted demo turns
python3 main.py dryrun --ask "status on dstrader"
```

The dry-run uses only the Python stdlib. It exercises the real status stub and
the real Claude-routing module (which falls back to a deterministic mock
summarizer when `ANTHROPIC_API_KEY` is unset), so the plumbing is identical to
live minus the audio + GPT brain.

## Live path (real speech-to-speech)

Needs macOS, a mic/speaker, and an OpenAI Realtime key.

```bash
brew install portaudio                       # sounddevice / PortAudio backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                          # fill in OPENAI_API_KEY (+ ANTHROPIC_API_KEY)
python3 main.py live
```

Then say: *"What's the status on dstrader?"* — barge in any time; server VAD
stops the agent mid-sentence.

## Config (`.env`)

See `.env.example`. Key switch for the evaluation:

- `CLAUDE_ROUTING=claude` — Claude summarizes the StatusReport (GPT-brain + Claude-as-tool). **Default.**
- `CLAUDE_ROUTING=raw` — return raw JSON, let the GPT brain reason unaided (shows its ceiling).

## Notes / limits

- **Stubbed data.** `status_report.py` hard-codes fixtures for `dstrader` and
  `cos-console`. When **w0-data-plane** publishes the real StatusReport MCP,
  repoint `get_status_report()` at it — nothing else changes.
- Never commit `.env`. `.gitignore` covers it.
- See `FINDINGS.md` for the head-to-head evaluation and verdict.

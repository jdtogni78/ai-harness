# voice-pipecat — Approach A: Claude-as-brain voice loop

A local macOS voice agent built with [Pipecat](https://github.com/pipecat-ai/pipecat):

```
mic → STT → Claude (tool-use) → TTS → speaker      (barge-in capable)
```

Claude is the reasoning brain. It can call **one** tool in this POC —
`get_project_status(project)` — **stubbed** against W0's frozen **StatusReport
v1.0** contract (`~/dev/cos-console/status-mcp/`) until the data-plane worker's
live MCP is wired. The stub is schema-validated against
`status_report.schema.json` (draft-07).

Target demo: say *"give me a status on dstrader"* → the agent speaks a concise
summary sourced from the StatusReport, and answers a follow-up (*"how many
tests?"*).

**Anti-fabrication guardrail (v1.0):** the report carries an `availability` map
and uses `null` (not `0`) for signals it couldn't collect. The console reads
`availability` before voicing any number — so asked *"how many tests does
cos-console have?"* (a project with no test data) it **says "I don't have test
data"** instead of a fabricated "0 passing". This is demoed in the default
dry-run.

Part of the cos-console Wave 1 voice-approach comparison. Verdict + latency notes
are in [`FINDINGS.md`](./FINDINGS.md).

---

## TL;DR — run the keyless demo right now

No install, no API keys, no mic. Runs on the system Python 3 stdlib alone:

```bash
python3 -m voice_pipecat --dry-run              # scripted 2-turn demo, spoken via `say`
python3 -m voice_pipecat --dry-run --no-audio   # same, no speaker output (CI-safe)
```

Sample output:

```
Providers: STT=whisper  Brain=canned (no key)  TTS=say

🎙  operator: give me a status on dstrader
🔊 console : Here's dstrader. 209 of 214 tests passing, 5 failing, 78 percent
             coverage. 11 tickets done, 2 in progress, 4 to do, and 1 blocked.
             Last deploy to dstrader-docker looks healthy.

🎙  operator: how many tests?
🔊 console : dstrader has 214 tests, 209 passing and 5 failing.

🎙  operator: how many tests does cos-console have?
🔊 console : I don't have test data for cos-console yet.       ← guardrail
```

In `--dry-run` the brain degrades gracefully:

| Condition | Brain used |
|---|---|
| No `ANTHROPIC_API_KEY` | deterministic **canned** brain (formats the stub report) |
| `ANTHROPIC_API_KEY` set + `anthropic` SDK installed | **real Claude** tool-use turn, headless (no mic) |

Feed your own utterances with `--text` (repeatable):

```bash
python3 -m voice_pipecat --dry-run --text "how's dstrader?" --text "did it deploy?"
```

---

## Full live voice loop

### 1. System deps (once)

```bash
brew install portaudio          # PyAudio needs it for mic/speaker
```

### 2. Python deps

```bash
./run.sh live                   # creates .venv, installs requirements.txt, runs
# — or manually —
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> The `silero` and `whisper` extras pull in PyTorch — expect a large (~1–2 GB)
> first install. If you have a Deepgram key you can skip local Whisper.

### 3. Keys (optional — each upgrades one stage)

```bash
cp .env.example .env            # then fill in what you have
```

| Stage | With key | Without key (fallback) |
|---|---|---|
| STT | Deepgram streaming (`DEEPGRAM_API_KEY`) | local Whisper (faster-whisper) |
| Brain | Claude (`ANTHROPIC_API_KEY`) — **required for live** | — (live mode refuses to start) |
| TTS | Cartesia / ElevenLabs | macOS `say` |

`ANTHROPIC_API_KEY` is the only hard requirement for **live** mode (Claude is the
brain). STT and TTS both have keyless local fallbacks.

### 4. Run

```bash
python -m voice_pipecat         # or: ./run.sh live
```

Speak into your mic: *"give me a status on dstrader"*. Barge-in works — start
talking while the agent is speaking and it stops to listen (Silero VAD +
Pipecat interruptions). `Ctrl-C` to quit.

Model defaults to `claude-opus-4-8`. For lower voice latency set
`CLAUDE_MODEL=claude-haiku-4-5` in `.env` (tradeoff discussed in FINDINGS).

---

## Layout

| File | Role |
|---|---|
| `voice_pipecat/status_tool.py` | The single tool + **stub StatusReport** (frozen to W0's v1.0 schema) + `availability` guardrail helper |
| `voice_pipecat/brain.py` | Claude system prompt + keyless `CannedBrain` |
| `voice_pipecat/config.py` | Env-based provider selection (graceful degradation) |
| `voice_pipecat/tts_say.py` | Pipecat `TTSService` wrapping macOS `say` (keyless TTS) |
| `voice_pipecat/bot.py` | Live Pipecat pipeline assembly (imports Pipecat) |
| `voice_pipecat/__main__.py` | CLI: `--dry-run` (Pipecat-free) vs live |

The `--dry-run` path never imports Pipecat, so the demo and the brain/tool logic
are testable before any `pip install`.

## Scope note

`get_project_status` is stubbed here on purpose. The **StatusReport schema is
owned by the w0-data-plane sibling** and frozen at **v1.0**
(`~/dev/cos-console/status-mcp/`). This POC consumes that shape verbatim (stub
reports are validated against `status_report.schema.json`) and does not diverge.
When W0's live MCP is wired, only `status_tool.py` changes.

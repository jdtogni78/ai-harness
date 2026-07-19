# FINDINGS — Approach A: Pipecat, Claude-as-brain

Voice loop: **mic → STT → Claude (tool-use) → TTS → speaker**, barge-in capable.
Claude is the reasoning brain; it calls one tool, `get_project_status(project)`,
stubbed against W0's frozen **StatusReport v1.0**.

## What was actually verified (this session, macOS, Python 3.13.1)

- **Pipecat 1.5.0 installs cleanly on Python 3.13** (core `[anthropic]` extra).
  No build failures. All import paths this code uses are verified against 1.5.0.
- **Keyless dry-run works end-to-end** — brain logic + tool stub + v1.0
  availability guardrail + macOS `say` synthesis, no keys, no mic, stdlib-only:
  `python3 -m voice_pipecat --dry-run`.
- **Stub validates against W0's `status_report.schema.json`** (draft-07) for all
  reports incl. the unknown-project fallback.
- **`say` emits `LEI16@24000` mono WAV** — exactly the pipeline sample rate, so
  the `SayTTSService` streams PCM with no resampling.
- **Anti-fabrication guardrail demoed:** asked for `cos-console` test counts
  (a project with `availability.tests = unavailable`, null numerics), the console
  says *"I don't have test data for cos-console yet"* instead of "0 passing".

**Not runtime-verified here** (no mic + no paid keys in this session): the full
live audio loop and the real-Claude tool round-trip. The live pipeline is
code-complete against the verified 1.5.0 API; the real-Claude path mirrors the
documented Anthropic manual tool loop. Latency/naturalness numbers below are
therefore **architectural estimates**, flagged as such — they need a keyed run to
confirm.

## Setup effort

- **Keyless demo: zero.** Runs on system `python3`, no install, no keys.
- **Full live loop: moderate/heavy.** `brew install portaudio` (PyAudio), then
  `pip install -r requirements.txt`. The `silero` + `whisper` extras pull
  **PyTorch (~1–2 GB)** — the one real friction point. With a Deepgram key you
  can drop the local-Whisper weight; VAD (Silero) still needs a small ONNX model.
- **Gotcha for future upgrades:** Pipecat 1.0 was a breaking rewrite. The old
  `OpenAILLMContext` + `llm.create_context_aggregator()` no longer exist — 1.5.x
  uses the universal `LLMContext` + `LLMContextAggregatorPair`, and VAD is a
  pipeline `VADProcessor(vad_analyzer=SileroVADAnalyzer())`, not a transport
  param. Most stale tutorials online show the pre-1.0 API. Pin `pipecat-ai>=1.5,<2`.

## Latency (estimated — cascade = sum of stages)

Approach A is a **cascade**, so end-to-end = STT-final + brain + TTS-first-audio:

| Config | Rough first-audio latency | Notes |
|---|---|---|
| Deepgram + `claude-haiku-4-5` + Cartesia | ~0.8–1.5 s | production-viable |
| Deepgram + `claude-opus-4-8` + Cartesia | ~2–4 s | opus is the brain-latency cost; **two** LLM round-trips per status turn (tool decision → summary) |
| Whisper(local) + opus + `say` | ~4–8 s | `say` is non-streaming (full synthesis before first audio); local Whisper adds transcription time |

**Model tradeoff:** repo guidance defaults to `claude-opus-4-8` (highest brain
quality), but for a snappy voice feel set `CLAUDE_MODEL=claude-haiku-4-5`. The
two-call tool pattern doubles the brain contribution — worth caching the system
prompt if this goes to production.

## Naturalness

- **TTS:** Cartesia/ElevenLabs = natural, streaming. macOS `say` = robotic but
  fully intelligible; fine as a keyless fallback / CI, not for demos to
  stakeholders.
- **Brain phrasing:** Claude produces genuinely good spoken summaries and honors
  the "one or two sentences, no URLs/timestamps" system prompt. The canned brain
  matches it closely for the fixed demo but is template-bound.

## Barge-in

Wired via `VADProcessor(SileroVADAnalyzer())`; interruptions are **on by default**
in Pipecat 1.x (no `allow_interruptions` flag anymore). Silero VAD gives clean
speech onset detection. Quality is **expected good** but not runtime-confirmed
this session (needs a mic).

## Cost (per status turn, live)

- Keyless path: **$0**.
- Keyed estimate: Deepgram STT ~$0.004/min; Claude turn is small (~1–2k input inc.
  tool result, ~100 output) but ×2 round-trips — pennies on haiku, more on opus;
  Cartesia TTS ~$0.02–0.03/min-equivalent. Ballpark **≈ $0.01–0.05 per turn**
  depending on model. Prompt-cache the system prompt + tool schema to cut it.

## Fit for the "validating chief-of-staff" mission

Strong. The whole point of cos-console is a manager that **refuses to fabricate**,
and Claude-as-brain is the natural home for that: the `availability`/`null`
guardrail lives in the prompt and Claude follows it, and tool-use gives clean,
auditable "what did it actually read" provenance. This is Approach A's real edge
over a speech-to-speech brain.

## One-line verdict vs the other two voice approaches

**Approach A (Pipecat + Claude-as-brain): best brain quality, best tool-use
fidelity, and the most natural home for the anti-fabrication guardrail — at the
cost of cascade latency; pick it when correctness/validation matters more than
raw snappiness, and reach for B (speech-to-speech realtime) only if sub-second
latency is the priority and C (fully local) only if privacy/offline is.**

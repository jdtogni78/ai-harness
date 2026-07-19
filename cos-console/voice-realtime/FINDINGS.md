# FINDINGS — voice-realtime (Approach B: OpenAI Realtime + Claude-as-tool)

**Scope of measurement.** The keyless dry-run path was run and verified here. The
**live speech-to-speech path was built but not run in this environment** (no
Realtime key / no audio hardware in the worker sandbox). Latency and naturalness
figures below are **estimates** grounded in the OpenAI Realtime API's documented
behaviour and architecture, clearly flagged as such — they need a real mic session
to confirm.

## Setup effort — LOW-to-MEDIUM

- Dry-run: **zero setup**, stdlib only. `python3 main.py dryrun` runs and shows the
  full loop + Claude handoff. Makes the repo inspectable with no secrets. ✅
- Live: `brew install portaudio` + `pip install -r requirements.txt` + one API key.
  The Realtime API is a single WebSocket — no STT/TTS/VAD components to wire up
  (server-side VAD + barge-in come for free). That's the big win vs. the Pipecat
  pipeline, which stitches STT→brain→TTS by hand.
- Sharpest edges: the Realtime event protocol (~a dozen event types to handle) and
  PCM16 24 kHz framing for PortAudio. Both are encapsulated in `realtime_agent.py`.

## Latency — EXPECTED VERY LOW (est. ~300–800 ms to first audio)

- Speech-to-speech collapses STT+LLM+TTS into one model, so first-audio latency is
  typically **sub-second** — the standout property of this approach and the reason
  it exists as a counterpoint.
- **BUT the Claude-as-tool call breaks that.** Any `get_project_status` turn pays:
  GPT decides to call the tool → our handler calls Claude (a full Anthropic
  round-trip, est. **~1–3 s** for Opus) → result goes back → GPT speaks it. So the
  snappy turns are the *conversational* ones; the *substantive* ones are gated by
  Claude and feel closer to Approach A. Net: great for "uh-huh / which project?"
  banter, a noticeable beat for the actual status answer.
- Mitigations (future): stream a filler ("let me pull that up…") before the tool
  returns; use a faster Claude tier (Haiku/Sonnet) inside the tool; cache reports.

## Naturalness / barge-in — EXPECTED BEST-IN-CLASS

- Server VAD + native barge-in is the headline. `Speaker.flush()` drops queued
  audio on `input_audio_buffer.speech_started`, so the agent stops mid-sentence the
  instant the operator talks over it — no half-duplex "wait for the beep" feel.
- Single-model prosody is more natural than pipeline TTS. This is the dimension
  where Approach B should clearly beat A and C.

## Cost — MEDIUM-HIGH, and you pay twice

- Realtime audio tokens (in+out) are materially pricier than text. A chatty
  chief-of-staff session adds up faster than a text-brain + TTS pipeline.
- Then **every substantive turn also pays a Claude call** — you're running two
  frontier models. This is the structural cost of "GPT brain + Claude tool."
- Lever: `CLAUDE_ROUTING=raw` skips Claude (GPT reasons over raw JSON) — cheaper and
  faster, but see the reasoning drop below.

## Reasoning-quality tradeoff — the core finding

Built as an A/B switch (`CLAUDE_ROUTING`) so it's measurable, not hand-waved:

- **`raw` (GPT brain alone):** the tool returns raw StatusReport JSON and the
  Realtime model reasons over it. Fine for lookups ("how many tests failing?") but
  the GPT-realtime model is tuned for fast dialogue, not deep synthesis — it tends
  to read numbers back rather than *judge* them ("4 failing, but they're all in the
  slippage module that's mid-refactor, so not alarming"), and it's weaker at
  connecting decisions ↔ open questions ↔ risk. This is the GPT-brain ceiling.
- **`claude` (Claude-as-tool, default):** Claude turns the same JSON into a
  proactive, risk-first spoken answer. Reasoning quality jumps; the cost is the
  latency beat and the second model bill above.
- **The handoff itself works cleanly.** One tool, verbatim question passed through,
  spoken-style system prompt on the Claude side → the Realtime model voices Claude's
  text as-is with good prosody. The seam is invisible to the listener *except* for
  the pause before the answer. The real limitation isn't the mechanism, it's that
  splitting brains means the fast model can't *interleave* reasoning with
  conversation — Claude answers in one shot, so multi-step "and what about X…"
  drill-downs each incur a fresh full round-trip.

## One-line verdict

**Best latency + barge-in of the four by a wide margin, and Claude-as-tool
recovers most of the reasoning quality — but only on turns that don't need it;
the moment you call Claude you pay Approach A's latency and two model bills, so
Approach B wins for a snappy, interruptible "chief of staff" whose deep reasoning
is occasional, and loses if most turns are substantive.**

*(Confirm the latency/naturalness estimates with a real keyed mic session before
we lock the Wave-1 comparison.)*

# FINDINGS — presentation-poc (#106)

What the two POCs proved, what was easy, and what a **live say-and-show voice
loop** still needs. Companion to [README.md](./README.md).

## What works (verified)

- **`deck.generate` → self-contained HTML** for `dstrader` *and* `familyfund`,
  straight off `status_mcp.probe`. One file, inline CSS+JS, no build, no deps;
  the whole `StatusReport` is injected as a single JSON blob. Sizes ~21–26 KB.
- **Anti-fabrication honored end-to-end.** Both projects report `tests:
  unavailable`; the deck renders the dashed **"NO DATA — this is NOT zero"**
  panel and an `n/a` KPI tile, and the narration says it out loud — never a `0`.
  Verified visually: `docs/dstrader-tests-nodata-{1440,390}.png`.
- **Responsive.** Desktop (1440) and phone (390) both render correctly; the
  title/tiles/narration wrap on phone (`docs/*-390.png`).
- **Narration companion** (`out/<project>-status.narration.json`) emitted with
  stable per-slide `{index, widget, narration}` — the reusable say-and-show
  coupling.
- **Narrated MP4** rendered for both projects via **narrate-demo** with **zero
  edits to ai-harness**: `demos/out/{dstrader,familyfund}-status.mp4`
  (H.264+AAC, 1440×900, ~59s, ~1.8 MB). Frame extraction confirms slides
  advance in lockstep with the narration (`docs/video-frame-*.png`).

## What was easy

- **Reusing the baseline design.** The hand-built `dstrader-status.html` already
  built its slides in JS from a data object `R`. Generalizing was mostly:
  (1) inject the real `StatusReport` as `R`, (2) make each slide builder consult
  `availability` before drawing, (3) move narration into the data so both the
  visible `🔊` line and the spoken `say:` line come from one source.
- **Driving the deck from narrate-demo unmodified.** narrate-demo has no
  "click"/"press key" verb, but it does have `goto`. Making the deck **hash-
  routed** (`#N` on load *and* `hashchange`) means each slide advance is just
  `do: goto` / `url: <deck>#N`. Same-document fragment navigations don't reload,
  so the fade animation plays and there's no flash. No new automation verbs.
- **Keyless.** `status_mcp.probe` + macOS `say` need no API keys.

## What bit us (and the fix)

- **Headless screenshot viewport.** `chrome --headless[=new] --screenshot
  --window-size=390,844` does not set the *layout* viewport to 390 — it lays out
  wide and captures a 390px slice, so phone shots looked clipped even though the
  layout was correct (`document.documentElement.scrollWidth == 390`). Fix:
  screenshot via Playwright's viewport emulation (`docs/shoot.py`), still
  headless. Desktop (1440) is unaffected.
- **ffmpeg was not installed.** narrate-demo needs `ffprobe`/`ffmpeg` for
  duration probe + wav concat + mux. Installed via Homebrew (`brew install
  ffmpeg`) — a reversible userland dev dependency, not a prod/daemon change.
  Without it, TTS (`say`) still runs but no MP4 can be produced.
- **narrate wrapper resolves relative script paths against its own dir.** Pass
  an **absolute** path to `narrate render` (the README quickstart does).

## What the live say-and-show voice loop still needs

The deck is already loop-ready: `window.go(n)` is the `show_slide` primitive and
the hash router is the transport-agnostic hook. To go **live** (voice brain
talks and drives in real time, not a pre-rendered video):

1. **A transport.** A tiny websocket/SSE channel the deck subscribes to; the
   brain sends `{cmd:"show_slide", index}` → `go(index)`. (Today the "transport"
   is the URL hash, driven offline by narrate-demo.)
2. **Streaming TTS, keyed.** Swap `say` for a low-latency engine
   (`narrate/tts.py` already has ElevenLabs/OpenAI stubs) so narration starts as
   the slide appears. Keep it **keyless-optional** — `say` stays the default.
3. **Barge-in / Q&A.** The operator interrupts ("show me the deploy details");
   the brain maps intent → `widget` → `go(index)` and speaks an ad-hoc line. The
   `{widget, narration}` companion is the seed vocabulary; ad-hoc answers need
   the brain to query `status-mcp` live per widget.
4. **Freshness.** Decks are generated point-in-time. A live loop should re-probe
   on demand (or subscribe to `status-mcp`) so numbers are current mid-session.

## Known gaps / non-goals (this POC)

- **Narration is templated, not authored by an LLM.** Lines are generated from
  the data with fixed phrasing (honoring anti-fabrication). Good enough to
  present; a voice brain would phrase more naturally.
- **`decisions` depth.** Mined from merge commits only (a `status-mcp` limit,
  noted upstream) — close-work reasoning isn't wired in.
- **Slide set is fixed** (title, tickets, tests, deploy, visual, decisions, open
  questions). Sections that are unavailable still get a slide (a no-data panel),
  which is deliberate — absence is a signal worth showing.
- **No relocation into ai-harness** — per the settled decision, everything stays
  in `cos-console/presentation-poc` until the operator picks a voice stack.

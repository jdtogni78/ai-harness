# surface — FINDINGS (Wave 2)

POC: a local, server-driven React PWA that renders W0's `StatusReport` as a
walkable deck for desktop + phone, driven the way the voice-brain will drive it.

## How the server-driven protocol worked

**Verdict: it works cleanly and the split is the right one.** The browser holds
*no* navigation authority — the server owns `{project, slideIndex}` and
broadcasts it over a websocket; every surface is a pure mirror. Consequences that
turned out to matter:

- **One source of truth = free multi-screen sync.** Desktop big-screen and phone
  show the same slide with zero extra code — a phone that joins mid-talk gets the
  current state on WS connect. The report is cached server-side per project so the
  two screens never show different numbers.
- **Local nav is just another intent.** Keyboard `←/→` and filmstrip taps are
  sent *up* to the server (`{type:"next"}`) rather than mutating local state, so a
  human nudging the desktop stays in lockstep with everything else. This also
  means the brain and the human share exactly one code path.
- **Widget aliases are the useful abstraction for a voice brain.** The brain
  shouldn't need to know slide indices. `server/deck.js` maps loose widget names
  (`coverage→tests`, `board→tickets`, `prod→deploy`, `diagram→architecture`) to
  canonical slide ids, so `{type:"show", widget:"coverage"}` just works. Resolve
  order is index → slideId → widget alias.
- **Intents are tiny and typed.** `goto | show | next | prev | project | reload`.
  A bad intent returns an error and does not move the deck (verified:
  `{"widget":"nope"}` → 400, deck unchanged). Good enough to hand to a tool-use
  model as-is.

Transport reality check: an HTTP `POST /control` (what the driver CLI and the
brain use) and the websocket both feed the *same* `applyIntent()`, and the WS
broadcast is the fan-out. So the brain can drive over plain HTTP while the
surfaces only need a read-only WS subscription — simplest possible split.

## Desktop vs phone

Tested in headless Chromium at **1440×900** and **390×844** (dark-first).

- **Desktop**: KPI tile rows, two-column tests/coverage, wide Mermaid diagram,
  full filmstrip. Reads as a big-screen "room" deck.
- **Phone**: `.cols` collapses to one column; the filmstrip scrolls horizontally;
  `100dvh` + `env(safe-area-inset-*)` keep it out from under the notch/home bar;
  `clamp()` type scales down. The no-data panel wraps and stays legible.
- **PWA**: `vite-plugin-pwa` emits manifest + service worker (installable via
  `npm run build && npm run preview`), so it can go full-screen on a phone.
- Charts: Recharts with `isAnimationActive={false}` — mount animation was fine
  live but disabling it makes the deck deterministic for a big-screen/streamed
  display (and for screenshotting). Colors from the operator's dataviz skill:
  status palette for pass/fail/skip, direct labels, single axis.

## The anti-fabrication contract (honored)

Every slide consults `report.availability[section]` **before** rendering a
number (`src/util.js#isLive`). `dstrader`'s tests are `unavailable` in the real
probe, so:

- Overview's Tests tile shows **`n/a` / "not available (unavailable)"**, not `0`.
- The Tests slide shows a hatched **"NO DATA · this is NOT zero"** panel with the
  probe's own warnings, plus a "what no-data means" explainer — never a 0-height
  bar chart. (`docs/desktop_tests.png`.)
- To prove the *live* chart path exists, `fixtures/demo.json` carries synthetic
  live test data → real green/red/yellow bars + a 76.4% coverage meter
  (`docs/tests_live.png`).

This is the same honesty rule the voice workers follow: `null` = "couldn't look",
`0` = "looked, found zero" — the surface renders them differently, always.

## What the eventual voice-brain needs to emit

To drive this surface, the brain's tool-use layer needs to produce **one small
intent per "show" action**, POSTed to `http://<host>:8787/control`:

```jsonc
{ "type": "show",  "widget": "coverage" }     // while it says "coverage is at 76%"
{ "type": "goto",  "widget": "tickets" }      // "the board has 26 done…"
{ "type": "next" } / { "type": "prev" }       // "next" / "go back"
{ "type": "project", "project": "familyfund" }// "switch to familyfund"
{ "type": "reload" }                          // "refresh that"
```

Concretely, that means:

1. **A `show_slide(widget|index)` tool** exposed to Claude, whose handler is a
   one-line POST to `/control`. Widget vocabulary = the aliases in
   `server/deck.js` (extend that map, not the brain's prompt).
2. **Say-and-show coupling**: emit the `show` intent *as* it starts the matching
   sentence, so the slide is up before the operator hears the number. The deck
   transition is ~320ms, so fire the intent slightly ahead of the TTS clause.
3. **Respect availability in speech too**: the brain should read the same
   `availability` map and *say* "I don't have test data for dstrader" when it puts
   up the no-data slide — the surface already refuses to fabricate; the voice must
   match it or they'll disagree.
4. Nothing else. The brain needs **no** deck state, no slide inventory, no
   rendering knowledge — it emits intents, the server + surface do the rest.

## Rough effort / cost

- Setup: `npm install` (~13s, Node 22), one `npm run dev`. No secrets, no
  external services. Probe shell-out is ~1–2s per project, cached after.
- Runtime cost: **zero** (fully local). The only paid piece is the future brain's
  token use, which is out of scope here.
- Node 16 (system default) is too old for Vite 5 — needs Node ≥ 18; documented in
  README.

## One-line recommendation

Ship the server-driven / widget-alias split as the surface contract: the brain
emits tiny typed intents over HTTP, the surface stays a dumb mirror, and the
`availability` honesty layer lives in the surface so "unknown" can never render as
"zero" — voice and slides then only have to agree on the same map.

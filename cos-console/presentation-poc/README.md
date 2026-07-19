# presentation-poc — the COS presentation layer

*Part of #99 (COS chief-of-staff exploration) · tracked as #106.*

**How the AI presents its work and talks over it.** Two POCs:

1. **`deck.generate`** — turns a `StatusReport` (from the sibling `status-mcp`
   data plane) into one **self-contained HTML deck** (no build, no deps, opens
   on desktop + phone) plus a reusable **narration script**.
2. **narrated video** — wires the generated deck + narration to the existing
   [`narrate-demo`](../../ai-harness/narrate) skill to render an **MP4** of the
   AI presenting the deck and talking over it (macOS `say`, keyless).

The hand-built proof `dstrader-status.html` (at the repo root) is the **visual
baseline**; `deck/` generalizes its exact design to any project.

## Quickstart

```bash
# 1. generate deck + narration companion + narrate-demo script for a project
python -m deck.generate dstrader          # or: familyfund
python -m deck.generate dstrader --from fixtures/dstrader.probe.json   # offline

# produces:
#   out/<project>-status.html             self-contained deck
#   out/<project>-status.narration.json   per-slide {widget, narration}
#   demos/<project>-status.demo.yaml       narrate-demo talk-over script

# 2. render the narrated MP4 (needs ffmpeg on PATH; TTS = macOS `say`)
../../ai-harness/narrate/narrate render "$PWD/demos/dstrader-status.demo.yaml"
#   -> demos/out/dstrader-status.mp4
```

`deck.generate <project>` shells out to `status_mcp.probe <project>` in the
sibling `../status-mcp` (read-only). Use `--from <probe.json>` to run fully
offline against a captured fixture (`fixtures/*.probe.json`).

## Anti-fabrication (the whole point)

The deck reads `StatusReport.availability` before drawing any number. A section
that is not `live`/`partial` — or whose payload is null — renders a **"NO DATA —
this is NOT zero"** panel (or an `n/a` KPI tile), never a fabricated `0`, and the
narration *says so* ("I can't answer how many tests pass — there's no test
data"). See slide 3 (`tests`) in either deck: both projects report tests
`unavailable`, so neither ever shows `0 passing`.

## The narration script — one coupling, two consumers

`out/<project>-status.narration.json` pairs every slide's stable `widget` id
with its spoken line:

```json
{ "slides": [ { "index": 0, "widget": "title",
                "narration": "Here's dstrader. 26 of 41 tickets done ..." }, ... ] }
```

* **narrate-demo** consumes it (baked into the `.demo.yaml`) to talk over a
  recorded walkthrough.
* A **future live voice loop** consumes the same `{widget, narration}` pairs:
  the deck already exposes `window.go(n)` (its `show_slide` primitive) and a
  URL-hash router, so a voice brain can `say(narration)` + `go(index)` in
  lockstep over a socket — exactly the say-and-show coupling narrate-demo
  replays offline.

## How slide advance works in the video

The deck is hash-routed: `deck.html#N` shows slide N (on load **and** on
`hashchange`). So narrate-demo drives it with its **stock `goto` verb** — each
step is `do: goto` / `url: <deck>#N` — with **zero edits to ai-harness** and no
custom automation. The generated `.demo.yaml` is just narration + `goto`s.

## Layout / structure

```
deck/
  generate.py        python -m deck.generate <project>  (deck + companion + demo.yaml)
  template.html      the baseline design, generalized; data injected as one JSON blob
dstrader-status.html hand-built visual baseline (reference; not regenerated)
fixtures/*.probe.json captured StatusReports (offline `--from` source)
out/                 generated decks + narration companions
demos/*.demo.yaml    narrate-demo talk-over scripts
demos/out/           rendered MP4s (gitignored — regenerate with narrate)
docs/                headless screenshots (1440 + 390) + video frames; shoot.py
```

## Verifying renders

`docs/shoot.py` screenshots each deck at **1440px** (desktop) and **390px**
(phone) via Playwright's viewport emulation:

```bash
../../ai-harness/narrate/.venv/bin/python docs/shoot.py   # -> docs/*.png
```

> Note: plain `chrome --headless --screenshot --window-size=390,844` does **not**
> set the *layout* viewport on a phone width — it renders wide and captures a
> 390px slice, so phone shots come out clipped. Playwright's viewport emulation
> (still headless) renders the true mobile layout. Desktop (1440) is fine either
> way.

See [FINDINGS.md](./FINDINGS.md) for what's easy, what the live voice loop still
needs, and known gaps.

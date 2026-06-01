---
name: narrate-demo
description: >-
  Produce an AI-narrated browser demo video (mp4) by driving Chromium with
  Playwright, synthesizing each step's narration with text-to-speech (macOS
  `say` by default, with stubs for ElevenLabs/OpenAI), drawing a glowing
  fake-cursor overlay so the mouse path is visible in the recording, and
  muxing it all into a single file. The demo "script" is a small YAML file
  (`<repo>/demos/<name>.demo.yaml`) with `say:` + `do:` steps, so non-code
  edits are easy. Use when the user wants to "make a demo video", "record a
  narrated walkthrough", "demo this feature", "generate a screencast",
  "produce a marketing/onboarding demo for app X", or asks for "the AI demo
  tool". The CLI lives at `ai-harness/narrate/` — wraps a venv on first run.
---

# narrate-demo (AI-narrated browser demo videos)

A short YAML script + `python -m narrate render` → mp4. Narration timing
auto-pads each action to the length of its synthesized voice line, so audio
and video stay in sync without manual `sleep`s.

## Pipeline (what runs under the hood)

1. **TTS** — each `say:` line is synthesized to AIFF via macOS `say` (free,
   no API key); `ffprobe` measures duration. Stubs for `openai` /
   `elevenlabs` engines exist in `narrate/tts.py` — swap-in is one function.
2. **Browser** — headless Chromium via Playwright, viewport configurable.
   A red glowing **fake-cursor** overlay is injected via
   `context.add_init_script(...)` so mouse paths show up in the recording
   (Playwright's `recordVideo` captures the DOM, not the OS cursor). Each
   action waits to fill its narration's duration.
3. **Concat** narration WAVs via ffmpeg concat-demuxer.
4. **Mux** the recorded `.webm` + narration WAV → `.mp4` (H.264 + AAC).

## Tool

```
narrate/narrate render  <script.yaml>      # build mp4
narrate/narrate preview <script.yaml>      # headed, no recording (iterate selectors)
narrate/narrate init    <path>             # scaffold a starter yaml
narrate/narrate voices                     # list `say -v ?` voices
narrate/narrate render --headed <yaml>     # render with visible browser
```

The wrapper auto-bootstraps `narrate/.venv` (Playwright + Chromium, ~150 MB)
on first invocation. Direct call also works:
`narrate/.venv/bin/python -m narrate ...`.

## YAML schema

Demo scripts live in the target repo at **`<repo>/demos/<name>.demo.yaml`**
so they version alongside the UI they describe.

```yaml
title: App login walkthrough
output: out/login.mp4                 # path relative to the YAML file
viewport: {width: 1280, height: 720}
tts:
  engine: say                         # say | openai | elevenlabs
  voice: Alex                         # `narrate voices` to list options
  rate: 185
steps:
  - say: "Welcome to the app."
    do: intro                         # just sit on a blank page

  - say: "Open the app."
    do: goto
    url: http://localhost:8000

  - say: "Point at the login button."
    do: move
    to: "button:has-text('Login')"    # any Playwright selector

  - say: "Scroll down to see the dashboard."
    do: scroll
    y: 600                            # pixels

  - say: "Hover the first heading below the fold."
    do: move_first_visible_h2

  - say: "That concludes the demo."
    do: wait
    ms: 800
```

Valid `do:` values: `intro`, `goto` (needs `url`), `move` (needs `to`),
`scroll` (needs `y`), `move_first_visible_h2`, `wait` (optional `ms`).

## Workflow

1. **Scaffold a script in the target repo:**
   ```bash
   cd <target-repo>
   <ai-harness>/narrate/narrate init demos/<feature>.demo.yaml --title "<Feature>"
   ```
2. **Iterate with preview** (headed browser, no recording — fast to refine
   selectors, scroll amounts, narration wording):
   ```bash
   <ai-harness>/narrate/narrate preview demos/<feature>.demo.yaml
   ```
3. **Render the final mp4:**
   ```bash
   <ai-harness>/narrate/narrate render demos/<feature>.demo.yaml
   # → demos/out/<feature>.mp4
   ```
4. **Commit the YAML** to the target repo (`out/` is gitignored).

## Targeting a leased preview env

For apps in the env pool, pair this with
[[lease-env]]: lease a preview slot, then point the YAML's `url:` at the
leased URL (e.g. `http://localhost:3021`). The slot's port stays stable for
the worktree, so the same YAML re-renders deterministically.

## Swapping TTS engines

`narrate/tts.py` has an adapter shape:

```python
def synthesize(text, dst_aiff, voice: Voice) -> float: ...
```

`Voice(engine='openai')` / `Voice(engine='elevenlabs')` currently raise
`NotImplementedError` — add the API call there and the rest of the pipeline
is unchanged (durations still measured via `ffprobe`).

## Smoke test

`demos/wikipedia.demo.yaml` (in ai-harness) renders to
`demos/out/wikipedia.mp4` — useful as a known-good baseline when changing
the runner.

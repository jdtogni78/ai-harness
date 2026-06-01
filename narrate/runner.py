"""Drive Playwright + record + mux. Action time is padded to narration length."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from playwright.async_api import async_playwright

from . import ffmpeg_util
from .cursor import INIT_SCRIPT
from .script import DemoScript, Step
from .tts import Voice, synthesize


@dataclass
class _Segment:
    step: Step
    wav: Path
    duration: float


async def render(script: DemoScript, *, headless: bool = True, record: bool = True) -> Path:
    """Run the demo and return the path to the final mp4 (or webm if record-only)."""
    out_dir = script.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".narrate"
    work.mkdir(exist_ok=True)

    # 1. Synthesize narration up front so we know each segment's duration.
    print("[1/4] Synthesizing narration ...")
    segments = _synth_all(script.steps, script.voice, work)
    total = sum(s.duration for s in segments)
    print(f"  total narration: {total:.2f}s")

    # 2. Drive the browser, time each action to its narration length.
    print("[2/4] Driving browser ...")
    video_path = await _drive(script, segments, work, headless=headless, record=record)
    if not record:
        return video_path  # preview mode, no audio mux

    # 3. Concat narration.
    print("[3/4] Building narration track ...")
    master = work / "narration.wav"
    ffmpeg_util.concat_audio([s.wav for s in segments], master)

    # 4. Mux.
    print("[4/4] Muxing video + audio ...")
    ffmpeg_util.mux(video_path, master, script.output)
    print(f"\nDone: {script.output}")
    return script.output


def _synth_all(steps: List[Step], voice: Voice, work: Path) -> List[_Segment]:
    segments: List[_Segment] = []
    for i, step in enumerate(steps):
        aiff = work / f"seg_{i:02d}.aiff"
        wav = work / f"seg_{i:02d}.wav"
        dur = synthesize(step.say, aiff, voice)
        ffmpeg_util.to_wav(aiff, wav)
        segments.append(_Segment(step=step, wav=wav, duration=dur))
        print(f"  {i}: {dur:5.2f}s  {step.say[:64]}")
    return segments


async def _drive(
    script: DemoScript,
    segments: List[_Segment],
    work: Path,
    *,
    headless: bool,
    record: bool,
) -> Path:
    vw, vh = script.viewport
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx_kwargs = dict(viewport={"width": vw, "height": vh})
        if record:
            ctx_kwargs.update(
                record_video_dir=str(work),
                record_video_size={"width": vw, "height": vh},
            )
        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(script=INIT_SCRIPT)

        page = await context.new_page()
        # Pre-load the opening page so recording starts on a painted page instead
        # of white about:blank + the first goto's load flash (which otherwise
        # plays under the first sentence or two of narration). If the demo opens
        # with a goto, navigate there now and skip re-navigating in scene 0.
        first = segments[0].step if segments else None
        prewarmed = bool(first and first.do == "goto" and first.url)
        if prewarmed:
            await page.goto(first.url, wait_until="load")
            await page.evaluate(INIT_SCRIPT)
            await page.wait_for_timeout(500)  # let fonts/CSS settle before t=0 content
        else:
            await page.goto("about:blank")
            await page.evaluate(INIT_SCRIPT)  # init on the blank starter page too
        await _glide(page, vw // 6, vh // 4)
        await page.wait_for_timeout(200)

        for i, seg in enumerate(segments):
            t0 = time.time()
            try:
                if i == 0 and prewarmed:
                    pass  # already navigated during pre-warm; hold on the page
                else:
                    await _do(page, seg.step, script.viewport)
            except Exception as e:
                print(f"  ! action {seg.step.do} failed: {e}")
            elapsed = time.time() - t0
            remaining = seg.duration - elapsed
            if remaining > 0:
                await page.wait_for_timeout(int(remaining * 1000))

        await page.wait_for_timeout(400)
        await context.close()
        await browser.close()

    if not record:
        return Path()  # caller ignores
    videos = sorted(work.glob("*.webm"))
    if not videos:
        raise RuntimeError("Playwright produced no video")
    return videos[-1]


async def _do(page, step: Step, viewport):
    vw, vh = viewport
    if step.do == "intro":
        await _glide(page, vw // 2, vh // 2)
    elif step.do == "goto":
        await page.goto(step.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(250)
        await _glide(page, vw // 2, vh // 4)
    elif step.do == "move":
        box = None
        try:
            el = await page.query_selector(step.to)
            if el:
                box = await el.bounding_box()
        except Exception:
            box = None
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + min(box["height"] / 2, 30)
            await _glide(page, x, y)
    elif step.do == "reveal":
        # Scroll a section/anchor to a fixed offset below the top, then point the
        # cursor at it — the primitive for walking a long page section by section.
        # We deliberately avoid scroll_into_view_if_needed: it does a *minimal*
        # scroll, so a section taller than the viewport (or one already partly in
        # view) lands at y≈0, hidden under a sticky nav bar — and the narration
        # ends up describing a section the viewer can't see. Scrolling the
        # element's top to ~110px below the viewport top keeps its header clear
        # of the sticky jump bar every time.
        try:
            el = await page.query_selector(step.to)
            if el:
                await page.evaluate(
                    """(sel) => {
                        const e = document.querySelector(sel);
                        if (!e) return;
                        const y = e.getBoundingClientRect().top + window.scrollY - 110;
                        window.scrollTo({top: Math.max(0, y), behavior: 'instant'});
                    }""", step.to)
                await page.wait_for_timeout(300)
                box = await el.bounding_box()
                if box:
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + min(box["height"] / 2, 60)
                    await _glide(page, x, y)
        except Exception:
            pass
    elif step.do == "move_first_visible_h2":
        box = await page.evaluate("""
            () => {
              for (const h of document.querySelectorAll('h2')) {
                const r = h.getBoundingClientRect();
                if (r.top > 50 && r.top < window.innerHeight - 50) {
                  return {x: r.x, y: r.y, w: r.width, h: r.height};
                }
              }
              return null;
            }
        """)
        if box:
            await _glide(page, box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
    elif step.do == "scroll":
        steps_n = 40
        each = step.y / steps_n
        for _ in range(steps_n):
            await page.mouse.wheel(0, each)
            await page.wait_for_timeout(15)
    elif step.do == "wait":
        if step.ms:
            await page.wait_for_timeout(step.ms)


async def _glide(page, x: float, y: float):
    await page.mouse.move(x, y, steps=30)
    await page.evaluate(f"window.__narrateMoveCursor && window.__narrateMoveCursor({x},{y})")


def run(script: DemoScript, *, headless: bool = True, record: bool = True) -> Path:
    return asyncio.run(render(script, headless=headless, record=record))

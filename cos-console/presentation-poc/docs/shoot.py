"""Headless screenshots of a generated deck at desktop + phone widths.

Uses Playwright's viewport emulation (via the narrate venv's Chromium) because
the plain `chrome --headless --screenshot --window-size=WxH` path does NOT set
the *layout* viewport on a mobile width — it renders at a wider viewport and
captures a WxH slice, so phone shots come out clipped. Playwright emulates the
device viewport correctly.

    <narrate-venv>/bin/python docs/shoot.py

Emits docs/<project>-<slide>-<width>.png for a few key slides.
"""
import asyncio, os
from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
DOCS = os.path.join(ROOT, "docs")

# (file, slide-index, slug) — title, tickets bar, and the anti-fabrication panel
SHOTS = [
    ("dstrader-status.html", 0, "title"),
    ("dstrader-status.html", 1, "tickets"),
    ("dstrader-status.html", 2, "tests-nodata"),
    ("dstrader-status.html", 3, "deploy"),
    ("familyfund-status.html", 0, "title"),
    ("familyfund-status.html", 1, "tickets"),
    ("familyfund-status.html", 2, "tests-nodata"),
]
WIDTHS = {"1440": (1440, 900), "390": (390, 844)}


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for label, (w, h) in WIDTHS.items():
            ctx = await b.new_context(viewport={"width": w, "height": h},
                                      device_scale_factor=2)
            for fname, idx, slug in SHOTS:
                proj = fname.split("-")[0]
                page = await ctx.new_page()
                await page.goto(f"file://{OUT}/{fname}#{idx}", wait_until="load")
                await page.wait_for_timeout(500)
                dst = os.path.join(DOCS, f"{proj}-{slug}-{label}.png")
                await page.screenshot(path=dst)
                print("wrote", os.path.relpath(dst, ROOT))
                await page.close()
            await ctx.close()
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())

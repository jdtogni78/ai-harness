#!/usr/bin/env python3
"""
cdp_driver.py — attach to the boss's REAL debug-Chrome over CDP and steer it
from a detached worker session via a control file.

This is the Approach 1 driver for the `joint-browser-usage` skill: the boss
launches a real Chrome on a DEDICATED debug profile and logs in once (clearing
Cloudflare Turnstile / 2FA as a human); this driver then ATTACHES to that same
Chrome with Playwright `connect_over_cdp` and drives already-authenticated
pages. Because we attach to the boss's real, already-logged-in browser,
`navigator.webdriver` is unset and the real profile / cookies / fingerprint are
in play — so bot checks that block a Playwright-LAUNCHED browser pass here.

SAFETY (codified — do not remove):
  * We ATTACH, never launch. We NEVER call browser.close(): that would kill the
    boss's Chrome. On exit the real Chrome is left open.
  * Idle auto-detach: after --idle-timeout seconds with no command we detach
    cleanly (driver exits, Chrome stays open) so we never leave an orphaned
    driver looping forever.
  * `goto` drives the ACTIVE tab and CAN hijack whatever the boss is doing —
    coordinate before issuing it.

Control protocol (one command per line, appended to the control file):
  goto <url>   navigate the active tab to <url>
  where        list all open tabs (url :: title), authoritative via /json
  detach       stop the driver, leave Chrome open

State/echo is written to the state file so a detached caller can read results.

Usage:
  python3 cdp_driver.py [--port 9222] [--base DIR] [--idle-timeout 2400]

Run it with the narrate venv which already has playwright + chromium:
  ~/dev/ai-harness/narrate/.venv/bin/python cdp_driver.py --base /tmp/jbu
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


def cdp_targets(port: int):
    """Authoritative open-tab read straight from the CDP HTTP endpoint.

    `ctx.pages` in Playwright can lag behind the real browser state; the
    /json endpoint is the source of truth for what tabs exist right now.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=5
        ) as r:
            data = json.load(r)
        return [t for t in data if t.get("type") == "page"]
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e)}]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=9222,
                    help="remote-debugging-port of the boss's debug Chrome")
    ap.add_argument("--base", default="/tmp/jbu",
                    help="dir holding the control + state files")
    ap.add_argument("--idle-timeout", type=int, default=2400,
                    help="seconds of no commands before auto-detach (0=never)")
    args = ap.parse_args()

    base = Path(args.base)
    control = base / "control.txt"
    state = base / "state.txt"
    cdp = f"http://127.0.0.1:{args.port}"

    base.mkdir(parents=True, exist_ok=True)
    control.write_text("")
    state.write_text("")

    def log(msg: str) -> None:
        with state.open("a") as f:
            f.write(msg + "\n")
        print(msg, flush=True)

    with sync_playwright() as p:
        # ATTACH — never launch. Reuse the boss's existing context + page.
        browser = p.chromium.connect_over_cdp(cdp)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log(f"ATTACHED over CDP; contexts={len(browser.contexts)} "
            f"pages={len(ctx.pages)}")

        pos = 0
        idle = 0
        while True:
            lines = control.read_text().splitlines()
            if pos < len(lines):
                idle = 0
                cmd = lines[pos].strip()
                pos += 1
                if not cmd:
                    continue
                if cmd == "detach":
                    log("DETACH")
                    break
                if cmd.startswith("goto "):
                    url = cmd[5:].strip()
                    try:
                        pages = ctx.pages
                        page = pages[-1] if pages else ctx.new_page()
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=45000)
                        log(f"AT {page.url} :: {page.title()}")
                    except Exception as e:  # noqa: BLE001
                        log(f"NAV-ERR {url}: {e}")
                elif cmd == "where":
                    # Authoritative tab read via /json (ctx.pages can lag).
                    for i, t in enumerate(cdp_targets(args.port)):
                        if "error" in t:
                            log(f"WHERE-ERR {t['error']}")
                        else:
                            log(f"TAB {i}: {t.get('url')} :: {t.get('title')}")
                else:
                    log(f"UNKNOWN CMD: {cmd}")
            else:
                time.sleep(1)
                idle += 1
                if args.idle_timeout and idle > args.idle_timeout:
                    log("IDLE-TIMEOUT detach")
                    break
        # NEVER browser.close() — that kills the boss's Chrome.
    log("DRIVER-EXIT (Chrome left open)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

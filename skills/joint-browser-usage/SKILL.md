---
name: joint-browser-usage
description: >-
  Share ONE real browser between the AI and the boss: the boss logs in once
  (past bot-checks like Cloudflare Turnstile and 2FA), then the AI drives the
  authenticated pages. Two approaches — (1) a real debug-Chrome the AI attaches
  to over CDP with Playwright `connect_over_cdp` and steers via a control file
  (scriptable, deterministic, works detached), and (2) the Claude browser
  EXTENSION acting in the boss's tab (simplest, prompt-driven, interactive).
  Use when the user says "log me in and then you take over", "drive my logged-in
  browser", "attach to my Chrome", "get past the Cloudflare/Turnstile check",
  "I'll sign in, you click through", "use my real session/cookies", or asks the
  AI to operate an authenticated site it can't log into itself (bank, dashboard,
  SaaS console behind a bot-wall).
---

# joint-browser-usage

Let the AI and the boss share **one real browser**. The boss logs in once —
clearing bot-checks (Cloudflare Turnstile), sign-in, and 2FA as a human — and
the AI then drives the already-authenticated pages. This sidesteps the failure
mode where a Playwright-**launched** browser loops on "Just a moment…" forever.

Two approaches. Pick with the decision table below.

## Which approach — decision table

| Situation | Use |
|---|---|
| One-off / interactive; you just want it to click a couple things | **Extension** |
| You want to *describe* the action in prose, no plumbing | **Extension** |
| Multi-step / repeatable flow, scripted | **CDP driver** |
| Need deterministic navigation + raw CDP state reads | **CDP driver** |
| Worker is **detached** (no TTY) and must be steered by file | **CDP driver** |
| A Playwright-launched browser is **Turnstile-blocked** ("Just a moment…") | **CDP driver** (attach to the boss's real login) |

Short version: **Extension = simplest, prompt-driven, interactive.**
**CDP driver = scriptable, deterministic, detached, multi-step.**

---

## Approach 1 — debug-Chrome + CDP (the main deliverable)

The AI attaches to the boss's real Chrome and steers it. Files:
- `scripts/launch_debug_chrome.sh` — boss-side launcher (dedicated profile).
- `scripts/cdp_driver.py` — the AI-side attach + control-file steering loop.

### Why this beats a launched browser (Turnstile rationale)
Attaching to the boss's real, already-logged-in Chrome means
`navigator.webdriver` is **unset**, the **real profile / cookies / fingerprint**
are in play, and the login was done by a **human**. So Cloudflare Turnstile and
similar bot checks **pass**. A Playwright-**launched** browser presents as
automated and gets stuck looping "Just a moment…". The boss logs in **once** in
the debug window (fresh profile ⇒ sign-in + 2FA); after that the AI navigates
authenticated pages freely.

### Steps

1. **Boss launches the debug Chrome** (dedicated profile — Chrome 136+ blocks
   remote-debug on the *default* profile, so a separate `--user-data-dir` is
   **required**):
   ```bash
   skills/joint-browser-usage/scripts/launch_debug_chrome.sh 9222 "$HOME/.chrome-debug-ff"
   ```
   Or the raw command:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-debug-ff" about:blank &
   ```
   Verify the debug endpoint:
   ```bash
   curl -s http://127.0.0.1:9222/json/version
   ```

2. **Boss logs in** in that Chrome window — sign-in, 2FA, and any Turnstile.

3. **AI attaches and steers** (uses the narrate venv, which already has
   playwright + chromium):
   ```bash
   ~/dev/ai-harness/narrate/.venv/bin/python \
     skills/joint-browser-usage/scripts/cdp_driver.py --port 9222 --base /tmp/jbu &
   ```
   The driver **attaches** via `connect_over_cdp` (NOT launch), reuses the
   existing context/page, and loops reading one command per line from
   `/tmp/jbu/control.txt`, echoing results to `/tmp/jbu/state.txt`.

4. **Drive it** by appending commands (one per line):
   ```bash
   echo "where" >> /tmp/jbu/control.txt                 # list tabs
   echo "goto https://example.com/" >> /tmp/jbu/control.txt
   echo "detach" >> /tmp/jbu/control.txt                # stop driver, leave Chrome open
   cat /tmp/jbu/state.txt                               # read what happened
   ```

   Commands: `goto <url>`, `where`, `detach`.

### Authoritative tab read
The `where` command reads tabs straight from
`curl -s http://127.0.0.1:9222/json` (targets with url + title). This is the
**source of truth** — Playwright's `ctx.pages` can lag behind the real browser.

### Safety rules (codified in the driver — keep them)
- **NEVER `browser.close()`** on a CDP-attached real Chrome — it kills the
  boss's browser. The driver leaves Chrome open on every exit path.
- **Dedicated profile** (`--user-data-dir`) isolates the debug session from the
  boss's main Chrome (and is mandatory on Chrome 136+).
- **Idle auto-detach**: the driver detaches after `--idle-timeout` seconds
  (default 2400) of no commands, so it never orphans a looping driver.
- **`goto` hijacks the active tab** — it can interrupt whatever the boss is
  doing. Coordinate before navigating so you don't move the page mid-action.

---

## Approach 2 — Claude browser extension (the alternative)

The Claude browser extension acts directly in the boss's **real, logged-in tab**,
driven by prose instructions — no CDP port, no driver, no control file. The boss
is already authenticated (their normal Chrome), so bot-checks are a non-issue.

- **Pros:** zero plumbing; you *describe* the action and it acts; ideal for
  one-off or interactive tasks.
- **Cons:** less deterministic and less scriptable than the CDP driver; no raw
  CDP state reads; not suited to a detached worker running unattended.

Use it for quick, interactive "click this, read that" tasks. Reach for the CDP
driver when you need a repeatable, scripted, detached, multi-step flow.

---

---

## Deliberately NOT shipped: the *launched*-browser driver

The #68 reference set also had a `driver.py` that **launched** its own Chromium
(`p.chromium.launch(headless=False, …)`) and steered it with the same
control-file loop. It is not included here, on purpose.

It's the approach that **fails**. And note it isn't headless — it's headed — so
headedness was never the issue: **the launch is**. Any launched browser, headed
or headless, gets a fresh automation profile, `navigator.webdriver` set, and no
login, which is exactly what Turnstile blocks. Shipping it as a supported option
would invite someone to retry the thing that already cost a walkthrough.

If you need to drive a browser where there's **no** login and **no** bot-wall,
you don't need this skill — use [[narrate-demo]], which already owns
Playwright-driven Chromium.

## OAuth: a detached worker can't finish one

Directly relevant if you reached for this skill to authorize something:
`claude mcp login` dies in a non-interactive session with *"stdin isn't a
terminal"*. The break is in the **paste-back of the redirect URL**, not the
browser — so this skill carries the consent page but **cannot** finish the flow.
Escalate for an interactive terminal, or use a scoped token from a `chmod 600`
file. Never paste credentials into chat. Full note:
[docs/joint-browser.md](../../docs/joint-browser.md).

## Notes
- Requirements for Approach 1: Google Chrome installed, and the narrate venv
  (`~/dev/ai-harness/narrate/.venv`, has playwright + chromium).
- The control/state files are plain append-and-read text, so a fully detached
  worker (no TTY) can steer the browser and read results.
- Local, non-prod tooling. Don't point it at anything that would expose
  financial data or prod secrets without the boss driving.

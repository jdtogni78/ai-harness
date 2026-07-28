# Joint browser usage (agent + boss share one browser)

Some pages an agent needs are behind a **bot-wall** (Cloudflare Turnstile), a
login, or 2FA. An agent-launched browser cannot get through them. The fix is to
share **one real browser**: the boss logs in once as a human, and the agent then
drives the already-authenticated session.

How-to lives in the skill — [`skills/joint-browser-usage/SKILL.md`](../skills/joint-browser-usage/SKILL.md).
This page records the *why* and the gotchas that cost real time to rediscover.

Built live **2026-07-27** during dstrader #68, when Turnstile blocked an
automated browser from Cloudflare's dashboard (issue #144).

## Two approaches

| Approach | Shape | Reach for it when |
|---|---|---|
| **debug-Chrome + CDP** | Real Chrome on `--remote-debugging-port=9222` with a **dedicated `--user-data-dir`**; agent attaches via Playwright `connect_over_cdp` and steers through a control file. | Scriptable, deterministic, multi-step, or the worker is **detached** (no TTY). |
| **Claude browser extension** | Prompt-driven, acts in the boss's real tab. No CDP plumbing. | One-off / interactive. Simplest thing that works. |

## Why attaching works and launching doesn't

It is the **launch** that breaks, not the headedness. A Playwright-*launched*
browser — headless *or* headed — gets a fresh automation profile, has
`navigator.webdriver` set, and has never been logged in. Turnstile parks it on
"Just a moment…" forever.

Attaching to the boss's real Chrome inverts every one of those: real profile and
cookies, `navigator.webdriver` unset, and a login performed by an actual human.
So the bot check passes. The boss signs in **once** in the debug window (a fresh
debug profile means sign-in + 2FA the first time); after that the agent
navigates authenticated pages freely.

## Gotchas

- **Chrome 136+ refuses remote debugging on the default profile.** A dedicated
  `--user-data-dir` is *required*, not merely tidy. It also isolates the debug
  session from the boss's main Chrome.
- **Never `browser.close()` a CDP-attached real Chrome.** It kills the boss's
  browser, not just your connection. Detach and leave it open.
- **Authoritative tab state is `curl -s http://127.0.0.1:9222/json`.** Playwright's
  `ctx.pages` lags behind the real browser; the CDP HTTP endpoint is the source
  of truth for what tabs exist right now.
- **`goto` hijacks the active tab** — it can yank the page out from under the
  boss mid-action. Coordinate before navigating.
- **A detached worker cannot complete an OAuth flow** — see below.

## A detached worker cannot self-authorize OAuth ("stdin isn't a terminal")

This one generalizes well past this skill: **it is why detached workers cannot
authorize MCP servers at all.** `claude mcp login <server>` prints an authorize
URL, then waits for the redirect URL to be pasted back — and in a non-interactive
session it gives up immediately:

```
Couldn't complete authentication for "<server>": stdin isn't a terminal, so
authentication can't be completed here. Re-run in an interactive terminal —
e.g. `ssh -t` — and paste the redirect URL when prompted.
```

Note the failure is in the **paste-back**, not the browser. So joint-browser
alone does not fix it — it carries the *consent page* (useful when that page is
itself behind a bot-wall or login), but a TTY is still required to finish.
Workarounds, in order of preference:

1. **Escalate to the boss** to run `claude mcp login` in an interactive terminal
   (joint-browser can carry the consent page if it's bot-walled).
2. **A scoped API token** read from a `chmod 600` file — least privilege, and
   never for anything the token can't safely be scoped away from.

**Never paste a token or credential into chat.** Chat transcripts are durable
and get read back by other agents; a `chmod 600` file is the only acceptable
carrier.

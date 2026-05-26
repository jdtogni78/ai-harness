# AI permission gate (`PreToolUse` hook)

A Claude Code **`PreToolUse` hook** that decides, for each matched tool call,
whether to **`allow` / `deny` / `ask`** — so routine-safe operations stop
re-prompting while genuinely-risky ones stay gated. It is the **in-session
enforcement arm** of the session-manager (#22): both consult the *same* stakes
policy in [`session-manager-cases.md`](session-manager-cases.md). One policy, two
enforcement points (the out-of-band manager that answers paused sessions, and
this in-process gate that pre-empts the routine prompts the code-sessions API
[can't even see](session-manager-cases.md#open-questions--findings)).

Entry point: `python3 -m remote_control perm-gate` (reads the hook event on
stdin, prints the decision on stdout). Code: `remote_control/perm_gate.py`.

## How it decides (two tiers)

1. **Static rules** (no model, instant, deterministic) — `remote_control/perm_gate.py`:
   - **deny-list** — safety-disabling / destructive-at-root (`--dangerously-skip-permissions`,
     `rm -rf /`, fork bombs, `mkfs`). Few on purpose: the gate *escalates* risky
     things to a human rather than auto-denying them.
   - **ask-list** — risky / outward-facing / hard to undo (`git push`, `merge`,
     `reset --hard`, `filter-branch`, recursive `rm`, `sudo`, deploy/`kubectl`/
     `terraform apply`, secrets (`sops`/`age`/`.env`), `curl … | sh`).
   - **allow-list** — routine reads / build / test (`git status|diff|log`, `ls`,
     `cat`, `rg`, `pytest`, `npm test`, `make test`, …). A chained command
     (`a && b`) is auto-allowed only if **every** segment is allow-listed.
   - Read-only tools (`Read`, `Glob`, `Grep`, `LS`, `NotebookRead`) → always allow.
2. **AI tier** — only for what the static rules don't cover ("the ambiguous
   middle"): one `POST /v1/messages` (urllib, no SDK, no `claude -p` spawn → can't
   recurse) given the call **plus the stakes policy** from the guideline doc. It
   classifies the call into a **risk tier** (below) and returns
   `{"risk","reason"}`, parsed tolerantly.

## Risk tiers

The AI tier classifies each call's **security / operational risk** on a 4-tier
scale, and `map_risk` turns the tier into a decision via two configurable
thresholds (`risk_ask_at`, `risk_deny_at`). Static rules carry the matching tier
too, so every logged row has a `risk` colour.

| Tier | Meaning | Default action |
|---|---|---|
| 🟢 `green` | safe, routine, reversible (reads, build, test) | allow |
| 🟡 `yellow` | minor caution, still low-risk | allow |
| 🟠 `orange` | risky / outward-facing / hard to undo (push, merge to main, deploy, secrets, large deletes) | **ask** |
| 🔴 `red` | dangerous / a security **threat** / destructive / irreversible (history rewrite + force-push, prod, exfiltration, disabling safety, supply-chain) | **deny** |

Default thresholds: ask at `orange`, deny at `red` (so green/yellow auto-allow).
Tighten with `PERM_GATE_RISK_ASK_AT` / `PERM_GATE_RISK_DENY_AT` (e.g. ask at
`yellow`). An unknown/unparseable tier is treated as `orange` (escalate, never
silently allow).

**Fail-safe, never fail-open.** Any error / timeout / unparseable reply → `ask`
(the human prompt). `main` always exits 0 — a non-zero exit would itself block
the tool.

## Shadow first

Default is **shadow mode** (`PERM_GATE_ENFORCE=0`): it decides and **logs** to
`logs/perm-gate-decisions.jsonl` but emits **no** decision, so the normal
permission flow is unchanged. Review the log, tune the rules / fill the
`GUIDELINE:` lines in `session-manager-cases.md`, then enforce in two safe stages:

1. **Static-only enforce** (`PERM_GATE_ENFORCE=1`, the default once enforcing):
   bind only the deterministic static rules — auto-allow the safe, **deny** the
   clearly-bad (`rm -rf /`), **ask** on risky (push/merge/deploy). The ambiguous
   middle falls through to the **normal human prompt**. No model call on the hot
   path, so the (now synchronous) hook stays fast, and a false-positive can never
   silently block ambiguous work.
2. **Full enforce** (`+ PERM_GATE_ENFORCE_AI=1`): also bind the AI tier's verdict
   for the ambiguous middle — a synchronous model call per ambiguous tool call
   (adds latency). Turn this on only once the shadow AI verdicts are trusted.

> Enforcing requires a **synchronous** hook (drop `async` from the settings
> entry) so the decision is awaited.

For a **zero-latency / zero-cost** shadow run (static logging only, no model
calls on the ambiguous middle), set `PERM_GATE_CONSULT_AI=0`.

## Config (env vars → `PermGateConfig.from_env`)

| Var | Default | Meaning |
|---|---|---|
| `PERM_GATE_ENABLED` | `1` | master switch; `0` → gate is a no-op |
| `PERM_GATE_ENFORCE` | `0` | `0` = shadow (log only); `1` = bind decisions |
| `PERM_GATE_ENFORCE_AI` | `0` | `0` = enforce static rules only (ambiguous → human prompt); `1` = also bind the AI tier |
| `PERM_GATE_CONSULT_AI` | `1` | `0` → ambiguous middle just `ask`s (no model call) |
| `PERM_GATE_MODEL` | `claude-haiku-4-5-20251001` | gate model |
| `PERM_GATE_MAX_TOKENS` | `400` | model reply cap |
| `PERM_GATE_HTTP_TIMEOUT_SECS` | `20` | per-call timeout |
| `PERM_GATE_RISK_ASK_AT` | `orange` | lowest risk tier that escalates to `ask` |
| `PERM_GATE_RISK_DENY_AT` | `red` | lowest risk tier that `deny`s |
| `PERM_GATE_GUIDELINES_FILE` | `docs/session-manager-cases.md` | the policy doc |
| `REMOTE_CONTROL_LOGDIR` | `…/ai-harness/logs` | where the JSONL + log land |

Auth for the AI tier: `ANTHROPIC_API_KEY` (`x-api-key`) if set, else the macOS
keychain OAuth token (`Claude Code-credentials`), same source as the usage-limit
monitor.

## Registering the hook

> `python3 -m remote_control` resolves to the **main checkout**
> (`~/dev/ai-harness`), so this code must be **merged to `main`** before the hook
> is registered — otherwise `perm-gate` is an unknown subcommand there.

Add to `~/.claude/settings.json` (global = all local sessions) — start in shadow:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "PERM_GATE_ENFORCE=0 python3 -m remote_control perm-gate"
          }
        ]
      }
    ]
  }
}
```

Reads (`Read`/`Glob`/`Grep`/`LS`) are intentionally left out of the matcher — no
need to gate them. Flip `PERM_GATE_ENFORCE=1` once the shadow log looks right.

## Testing

`tests/test_perm_gate.py` mirrors the manager's layers, all offline (the network
advisor is injected): static allow/deny/ask fixtures, chained-command safety,
ambiguous → advisor, advisor-error → `ask` fail-safe, shadow vs enforce hook
output, the recursion guard, `parse_verdict` tolerance, and malformed stdin.

"""AI permission gate -- a Claude Code ``PreToolUse`` hook.

Registered (see ``docs/perm-gate.md``) so that before any matched tool call,
Claude Code pipes the call as JSON on stdin and honors the ``permissionDecision``
this prints on stdout. For each request we decide ``allow`` / ``deny`` / ``ask``
in two tiers:

1. **Static rules** (no model, instant, deterministic) -- a deny-list of
   safety-disabling / clearly-malicious commands, an ask-list of risky /
   outward-facing ones (push, merge, deploy, secrets, recursive deletes), and an
   allow-list of routine reads / build / test. Covers the bulk of calls.
2. **AI tier** -- only for what the static rules don't cover ("the ambiguous
   middle"): one Messages-API call given the call plus the **stakes policy** from
   the guideline doc the session-manager already uses
   (``docs/session-manager-cases.md``). One policy, two enforcement points.

Design rules, all load-bearing:

* **Fail-safe, never fail-open.** Any error/timeout/parse-miss -> ``ask`` (the
  human prompt). We never silently ``allow`` on failure, and ``main`` always
  exits 0 (a non-zero exit would itself block the tool).
* **Shadow first** (``PERM_GATE_ENFORCE=0``, the default): decide + log to
  ``perm-gate-decisions.jsonl`` but emit *no* decision, so the normal permission
  flow is unchanged until the verdicts earn trust.
* **No recursion / no agent spawn.** The AI tier is a plain urllib Messages-API
  call -- it does not launch ``claude``, so it can't re-enter this hook. A
  ``PERM_GATE_IN_ADVISOR`` env sentinel is honored as belt-and-suspenders for any
  future advisor that *does* shell out to ``claude``.

The pure helpers (``extract_subject``, ``static_decision``, ``decide``,
``parse_verdict``, ``hook_output``) take their inputs as plain values and the
network advisor as an injected callable, so the whole decision path is unit-
tested offline -- the same shape as ``manager.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .config import PermGateConfig
from .logging_util import make_logger

# Decision values, matching Claude Code's PreToolUse ``permissionDecision``.
ALLOW = "allow"
DENY = "deny"
ASK = "ask"
_VALID = (ALLOW, DENY, ASK)

# Risk tiers (severity), low -> high. The AI tier classifies each call into one of
# these (a security/threat read), and ``map_risk`` turns the tier into a decision
# via two configurable thresholds (cfg.risk_ask_at / risk_deny_at). Static rules
# carry the matching tier too, so every logged row has a risk colour.
GREEN = "green"     # safe, routine, reversible
YELLOW = "yellow"   # minor caution, still low-risk
ORANGE = "orange"   # risky / outward-facing / hard to undo
RED = "red"         # dangerous / security threat / destructive / irreversible
_RISKS = (GREEN, YELLOW, ORANGE, RED)
_RISK_ORDER = {GREEN: 0, YELLOW: 1, ORANGE: 2, RED: 3}
_DECISION_TO_RISK = {ALLOW: GREEN, ASK: ORANGE, DENY: RED}

# An advisor maps a prompt -> (decision, risk, reason); either of decision/risk may
# be "" (``decide`` reconciles them). Injected so tests never hit the network; the
# default implementation is ``make_urllib_advisor``.
Advisor = Callable[[str], Tuple[str, str, str]]

# Tools whose input is read-only -- always safe to auto-allow regardless of args.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})

# --- Static policy --------------------------------------------------------
# Matched case-insensitively against the call's "subject" (for Bash, the command;
# for file tools, the path). DENY/ASK match anywhere in the subject (a dangerous
# fragment in a chained command still trips them); ALLOW must cover *every*
# segment of a chained command (see ``static_decision``).

# Hard block: safety-disabling or clearly-destructive-at-root. Few on purpose --
# the gate escalates risky things to a human (ASK); it only DENIES the indefensible.
DENY_PATTERNS: Tuple[str, ...] = (
    r"--dangerously-skip-permissions",        # never let the agent disable safety
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\*)(\s|/|$)",  # rm -rf / ~ $HOME *
    r":\s*\(\s*\)\s*\{.*\}\s*;",               # fork bomb
    r">\s*/dev/sd[a-z]\b",                     # writing a raw disk
    r"\bmkfs\b",
)

# Escalate to a human: risky, outward-facing, or hard to undo.
ASK_PATTERNS: Tuple[str, ...] = (
    r"\bgit\s+push\b",                          # outward-facing (any push)
    r"\bgit\s+(filter-branch|filter-repo)\b",   # history rewrite
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+merge\b",                         # local merge = medium stakes
    r"\bgit\s+clean\b[^|;&]*-[a-z]*f",          # force-clean (deletes untracked)
    r"\brm\s+-[a-z]*r",                         # any recursive delete
    r"\bsudo\b",
    r"\b(deploy|kubectl|terraform\s+apply|helm\s+(install|upgrade|delete)|docker\s+push)\b",
    r"\b(sops|age)\b",                          # secrets tooling
    r"(^|\s)\.?env(\.|\b)",                      # touching .env / env files
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b",  # curl|sh
    r"\bchmod\s+-?R?\s*777\b",
)

# Auto-allow: routine, reversible, non-outward-facing. Anchored at the start of a
# command segment so they describe the *whole* segment, not a substring.
ALLOW_PATTERNS: Tuple[str, ...] = (
    r"^(ls|pwd|whoami|date|echo|cat|head|tail|wc|file|stat|which|type|env|printenv|uname|hostname|true|cd)\b",
    r"^(grep|rg|find|fd|ag|tree|sed\s+-n|awk)\b",
    r"^git\s+(status|diff|log|show|branch|remote(\s+-v)?|fetch|rev-parse|describe|blame|stash\s+list|ls-files|config\s+--get|config\s+--list|shortlog|tag\s+-l)\b",
    r"^(npm|pnpm|yarn)\s+(test|run\s+test|run\s+lint|run\s+build|run\s+typecheck|ci)\b",
    r"^(pytest|tox|jest|vitest)\b",
    r"^python3?\s+-m\s+(pytest|unittest)\b",
    r"^(go\s+(test|build|vet)|cargo\s+(test|build|check|clippy)|make\s+(test|check|lint|build))\b",
    # Read-only `gh` surface. Curated -- mutating verbs (create/edit/close/
    # delete/merge/comment/...) and the wildcard `gh api` deliberately fall
    # through to the AI tier so PERM_GATE_ENFORCE_AI=1 can bind them. Keep
    # this list to subcommands that genuinely cannot mutate remote state.
    r"^gh\s+pr\s+(view|list|status|checks|diff)\b",
    r"^gh\s+issue\s+(view|list|status)\b",
    r"^gh\s+project\s+(view|list|item-list|field-list)\b",
    r"^gh\s+(repo|release|workflow|run|gist)\s+(view|list)\b",
    r"^gh\s+search\s+(code|commits|issues|prs|repos)\b",
    r"^gh\s+(label|secret|variable)\s+list\b",
    r"^gh\s+(auth\s+status|browse)\b",
)

_DENY_RE = [re.compile(p, re.I) for p in DENY_PATTERNS]
_ASK_RE = [re.compile(p, re.I) for p in ASK_PATTERNS]
_ALLOW_RE = [re.compile(p, re.I) for p in ALLOW_PATTERNS]

# Splits a shell command into the segments that each must be independently safe
# before we auto-allow the whole thing (``a && b``, ``a; b``, ``a | b``).
_SEG_SPLIT = re.compile(r"&&|\|\||[;|]")

# Inert string arguments we strip before ASK-matching, so user prose in
# `--body "..."` / `-m "fix .env loading"` / `cat <<EOF ... EOF` can't trip
# ASK patterns like ``\benv\b`` / ``\b(sops|age)\b``. DENY still sees the full
# subject -- destructive content stays banned even if it hides in a string.
_HEREDOC_RE = re.compile(
    r"<<-?\s*\\?['\"]?(\w+)['\"]?[^\n]*\n.*?(?:^|\n)\s*\1\b",
    re.S | re.M,
)
_SQ_RE = re.compile(r"'[^']*'")          # POSIX single-quotes: no escapes
_DQ_RE = re.compile(r'"(?:[^"\\]|\\.)*"')  # double-quotes: honor \"


def strip_inert_strings(s: str) -> str:
    """Remove HEREDOC bodies and quoted-string contents from a shell command,
    leaving just the command structure (binary + flags + unquoted args). Used
    to keep ASK_PATTERNS from matching user prose inside ``--body``/``-m``
    arguments. Not a real shell parser -- conservative enough that DENY still
    runs against the untouched subject."""
    s = _HEREDOC_RE.sub("", s)
    s = _SQ_RE.sub("", s)
    s = _DQ_RE.sub("", s)
    return s


def extract_subject(tool_name: str, tool_input: Dict) -> str:
    """The string the static rules match and the model reasons about: a Bash
    command, a file tool's path, else a compact dump of the input. Empty input ->
    "" (so a malformed/empty event is treated as unknown, never sent to the AI)."""
    ti = tool_input or {}
    if not ti:
        return ""
    if tool_name == "Bash":
        return str(ti.get("command", "")).strip()
    path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    if path:
        return str(path).strip()
    try:
        return json.dumps(ti, sort_keys=True)[:1000]
    except (TypeError, ValueError):
        return str(ti)[:1000]


def _matches(res, text: str) -> bool:
    return any(r.search(text) for r in res)


def static_decision(tool_name: str, subject: str) -> Optional[Tuple[str, str, str, str]]:
    """Apply the deterministic rules. Returns ``(decision, reason, tier, risk)`` or
    ``None`` when no rule covers it (-> the ambiguous middle, for the AI tier).
    Precedence: read-only tool -> deny -> ask -> allow."""
    if tool_name in READ_ONLY_TOOLS:
        return (ALLOW, f"{tool_name} is read-only", "static-readonly", GREEN)
    if not subject:
        return None
    if _matches(_DENY_RE, subject):
        return (DENY, "matches a deny-listed (unsafe) pattern", "static-deny", RED)
    # ASK matches against the command with quoted/HEREDOC bodies stripped, so
    # prose in ``--body``/``-m`` can't trip patterns like ``\benv\b``.
    if _matches(_ASK_RE, strip_inert_strings(subject)):
        return (ASK, "risky / outward-facing -- needs a human", "static-ask", ORANGE)
    # Auto-allow only if EVERY chained segment is independently allow-listed, so a
    # safe-looking prefix can't smuggle an unvetted command after ``&&``/``;``/``|``.
    if tool_name == "Bash":
        segments = [s.strip() for s in _SEG_SPLIT.split(subject) if s.strip()]
        if segments and all(_matches(_ALLOW_RE, s) for s in segments):
            return (ALLOW, "all segments are allow-listed (read/build/test)",
                    "static-allow", GREEN)
        return None
    # Non-Bash, non-read-only tool (Edit/Write/...): no static verdict -> AI tier.
    return None


def map_risk(risk: str, cfg: PermGateConfig) -> str:
    """Map a risk tier to a decision via the operator's two thresholds: at/above
    ``risk_deny_at`` -> deny, at/above ``risk_ask_at`` -> ask, else allow. An
    unknown tier is treated as orange (escalate, never silently allow)."""
    r = _RISK_ORDER.get(risk, _RISK_ORDER[ORANGE])
    if r >= _RISK_ORDER.get(cfg.risk_deny_at, _RISK_ORDER[RED]):
        return DENY
    if r >= _RISK_ORDER.get(cfg.risk_ask_at, _RISK_ORDER[ORANGE]):
        return ASK
    return ALLOW


def read_guidelines(cfg: PermGateConfig) -> str:
    """The stakes-policy doc handed to the AI tier (empty if absent). Shared with
    the session-manager (see manager.read_guidelines)."""
    try:
        return cfg.guidelines_file.read_text()
    except OSError:
        return ""


def build_advice_prompt(tool_name: str, subject: str, guidelines: str) -> str:
    """The Messages-API prompt for the ambiguous middle: classify the call's
    security/operational RISK into a colour tier (we map the tier -> a decision)."""
    g = (f"\nPOLICY -- the operator's guidelines; weigh these heavily:\n"
         f"---\n{guidelines.strip()}\n---\n"
         if guidelines and guidelines.strip() else "")
    return (
        f"An autonomous coding agent wants to run the tool {tool_name!r} with this "
        f"input:\n---\n{subject}\n---\n{g}\n"
        "Classify the SECURITY / operational RISK of letting it run, on this 4-tier "
        "scale (we map the tier to an action):\n"
        "- green  = safe, routine, reversible (reads, build, test) -> allow\n"
        "- yellow = minor caution, still low-risk -> allow\n"
        "- orange = risky / outward-facing / hard to undo (push, merge to main, "
        "deploy/prod, secrets, large deletes) -> ask a human\n"
        "- red    = dangerous / a security THREAT / destructive / irreversible "
        "(history rewrite + force-push, prod deploy, data exfiltration, disabling "
        "safety, supply-chain) -> deny\n"
        "When unsure, pick the HIGHER tier.\n"
        'Reply with ONLY one JSON object: '
        '{"risk":"green|yellow|orange|red","reason":"<short; name the threat if any>"}'
    )


_JSON_RE = re.compile(r"\{.*\}", re.S)
_DEC_RE = re.compile(r"(?i)\b(allow|deny|ask)\b")
_RISK_RE = re.compile(r"(?i)\b(green|yellow|orange|red)\b")


def parse_verdict(text: str) -> Tuple[str, str, str]:
    """Parse a model reply into ``(decision, risk, reason)``; either of
    decision/risk may be "" (``decide`` reconciles them). Tolerant: tries JSON,
    then a bare risk/decision word; anything unrecognized -> ("", "", reason)."""
    text = (text or "").strip()
    if not text:
        return ("", "", "empty model reply")
    m = _JSON_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            risk = str(obj.get("risk", "")).strip().lower()
            dec = str(obj.get("decision", "")).strip().lower()
            reason = str(obj.get("reason", "")).strip() or "(no reason given)"
            risk = risk if risk in _RISKS else ""
            dec = dec if dec in _VALID else ""
            if risk or dec:
                return (dec, risk, reason)
        except (json.JSONDecodeError, AttributeError):
            pass
    mr = _RISK_RE.search(text)
    if mr:
        return ("", mr.group(1).lower(), text[:200])
    md = _DEC_RE.search(text)
    if md:
        return (md.group(1).lower(), "", text[:200])
    return ("", "", "unparseable model reply")


def _result(decision: str, reason: str, tier: str, subject: str,
            tool: str, risk: str) -> Dict:
    return {"decision": decision, "reason": reason, "tier": tier,
            "subject": subject, "tool": tool, "risk": risk}


def decide(tool_name: str, tool_input: Dict, *, cfg: PermGateConfig,
           guidelines: str = "", advisor: Optional[Advisor] = None) -> Dict:
    """The tiered decision (pure besides the injected ``advisor``). Returns
    ``{decision, reason, tier, subject, tool}``. Static rules first; the AI tier
    only for what they don't cover; fail-safe to ``ask`` everywhere."""
    subject = extract_subject(tool_name, tool_input)
    if not cfg.enabled:
        return _result(ALLOW, "gate disabled", "disabled", subject, tool_name, GREEN)

    static = static_decision(tool_name, subject)
    if static is not None:
        decision, reason, tier, risk = static
        return _result(decision, reason, tier, subject, tool_name, risk)

    if not subject:  # malformed / empty event -> unknown; never deny or hit the AI
        return _result(ASK, "empty / unknown tool input", "no-subject",
                       subject, tool_name, ORANGE)

    # Consult the AI for the ambiguous middle in shadow (for the log) and under
    # full enforce; skip it under static-only enforce so the sync hook stays fast.
    consult = (cfg.consult_ai and advisor is not None
               and (cfg.enforce_ai or not cfg.enforce))
    if consult:
        try:
            decision, risk, reason = advisor(
                build_advice_prompt(tool_name, subject, guidelines))
            if risk in _RISKS:               # risk tier is canonical -> map to action
                decision = map_risk(risk, cfg)
            elif decision in _VALID:         # model gave only a decision -> back-fill risk
                risk = _DECISION_TO_RISK[decision]
            else:                            # neither usable -> escalate (never allow)
                decision, risk = ASK, ORANGE
                reason = reason or "unclassified -> ask"
            return _result(decision, reason, "ai", subject, tool_name, risk)
        except Exception as e:  # network/timeout/parse -> never fail open
            return _result(ASK, f"advisor error: {e}", "ai-error", subject,
                           tool_name, ORANGE)

    return _result(ASK, "no static rule; AI tier off", "no-rule", subject,
                   tool_name, ORANGE)


def hook_output(result: Dict, cfg: PermGateConfig) -> Optional[Dict]:
    """The JSON to print on stdout. ``None`` -> emit nothing, normal permission
    flow proceeds. Shadow (not enforce) -> always None. Under enforce, bind the
    deterministic static rules always; bind the ambiguous middle (ai / no-rule /
    no-subject) only when ``enforce_ai`` -- otherwise it falls through to the
    normal human prompt (safe, zero-latency static-only enforce)."""
    if not cfg.enforce:
        return None
    tier = result.get("tier", "")
    binding = cfg.enforce_ai or tier.startswith("static") or tier == "disabled"
    if not binding:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": result["decision"],
        "permissionDecisionReason": result["reason"],
    }}


def decision_record(result: Dict, ev: Dict, cfg: PermGateConfig,
                    now: Optional[datetime] = None) -> Dict:
    """One append-only log row capturing what the gate decided and whether it was
    actually enforced (vs shadow-logged)."""
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "at": ts,
        "session_id": ev.get("session_id", ""),
        "cwd": ev.get("cwd", ""),
        "tool": result["tool"],
        "subject": (result["subject"] or "")[:500],
        "decision": result["decision"],
        "risk": result.get("risk", ""),
        "tier": result["tier"],
        "reason": result["reason"],
        "enforced": bool(cfg.enforce),
    }


def append_jsonl(path, record: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def run_hook(stdin_text: str, cfg: PermGateConfig, *,
             advisor: Optional[Advisor] = None, log=None,
             env: Optional[Dict[str, str]] = None,
             now: Optional[datetime] = None) -> Optional[Dict]:
    """Full hook cycle: parse the PreToolUse event, decide, log, and return the
    stdout payload (or ``None`` to emit nothing). Never raises."""
    env = os.environ if env is None else env
    # Belt-and-suspenders: if we're somehow running *inside* an advisor's own
    # agent, don't re-gate -- emit nothing and let it proceed.
    if env.get("PERM_GATE_IN_ADVISOR"):
        return None
    try:
        ev = json.loads(stdin_text or "{}")
        if not isinstance(ev, dict):
            ev = {}
    except (json.JSONDecodeError, TypeError):
        ev = {}

    tool_name = ev.get("tool_name", "") or ""
    tool_input = ev.get("tool_input", {}) or {}
    result = decide(tool_name, tool_input, cfg=cfg,
                    guidelines=read_guidelines(cfg), advisor=advisor)

    try:
        append_jsonl(cfg.decisions_file, decision_record(result, ev, cfg, now=now))
    except OSError as e:
        if log:
            log(f"decision log write failed: {e}")
    if log:
        mode = "ENFORCE" if cfg.enforce else "shadow"
        log(f"[{mode}] {tool_name} {result['tier']} "
            f"{result.get('risk', '?')}/{result['decision']} "
            f":: {result['subject'][:120]}")
    return hook_output(result, cfg)


# --- Side-effecting default advisor (not unit-tested; injected in tests) ----

def _get_token(cfg: PermGateConfig, env: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
    """Auth for the Messages API. Prefers ``ANTHROPIC_API_KEY`` (x-api-key); else
    the macOS-keychain OAuth token (same source as the usage-limit monitor)."""
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    base = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if key:
        return key, {**base, "x-api-key": key}
    out = subprocess.run(
        ["security", "find-generic-password", "-s", cfg.keychain_service, "-w"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("no ANTHROPIC_API_KEY and keychain read failed")
    token = json.loads(out.stdout.strip()).get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise RuntimeError("keychain payload missing claudeAiOauth.accessToken")
    return token, {**base, "Authorization": f"Bearer {token}",
                   "anthropic-beta": "oauth-2025-04-20"}


def make_urllib_advisor(cfg: PermGateConfig, log=None,
                        env: Optional[Dict[str, str]] = None) -> Advisor:
    """The default advisor: one ``POST /v1/messages`` (urllib, no SDK), parsed via
    ``parse_verdict``. Raises on any failure so ``decide`` records ``ai-error``
    and falls back to ``ask``."""
    env = os.environ if env is None else env

    def advise(prompt: str) -> Tuple[str, str, str]:
        _token, headers = _get_token(cfg, env)
        body = json.dumps({
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": ("You are a strict but practical permission gate for an "
                       "autonomous coding agent. Default to ASK when unsure."),
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(cfg.api_base + "/messages", data=body,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=cfg.http_timeout) as r:
            data = json.loads(r.read())
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        decision, risk, reason = parse_verdict(text)
        if log:
            log(f"ai risk={risk or '?'} decision={decision or '?'} ({reason[:80]})")
        return decision, risk, reason

    return advise


def main(argv: Optional[List[str]] = None) -> int:
    """Hook entrypoint: read the PreToolUse event from stdin, print the decision
    (if enforcing). ALWAYS exits 0 -- a non-zero exit would itself block the tool."""
    cfg = PermGateConfig.from_env()
    log = make_logger(cfg.log_file, utc=True)
    try:
        advisor = make_urllib_advisor(cfg, log) if cfg.consult_ai else None
        stdin_text = sys.stdin.read()
        out = run_hook(stdin_text, cfg, advisor=advisor, log=log)
        if out is not None:
            print(json.dumps(out))
    except Exception as e:  # absolute last resort: log, emit nothing, don't block
        try:
            log(f"perm-gate fatal: {e}")
        except Exception:
            pass
    return 0

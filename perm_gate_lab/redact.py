"""Defense-in-depth redaction for captured perm_gate text.

The production hook already passes through user data; before any of it lands
in the lab corpus (and especially before it gets sent to an LLM-judge later),
strip obvious credential patterns. False negatives are expected — this is a
mistake-prevention layer, not a security boundary. Same caveat Anthropic
makes about Auto Mode.
"""

from __future__ import annotations

import re
from typing import Iterable, Pattern


_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_KEY",    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN",  re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("BEARER_TOKEN",  re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b")),
    ("SSH_PATH",      re.compile(r"(?:~|/Users/[^/\s'\"]+|/home/[^/\s'\"]+)/\.ssh/[^\s'\"]+")),
    ("ENV_SECRET",    re.compile(
        r"\b([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY))"
        r"\s*=\s*['\"]?([^'\"\s]+)",
    )),
    ("PEM_BLOCK",     re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    )),
]


def redact(text: str | None) -> tuple[str | None, bool]:
    """Return (redacted_text, was_redacted). Empty / None passes through."""
    if not text:
        return text, False
    redacted = text
    hit = False
    for label, pat in _PATTERNS:
        if label == "ENV_SECRET":
            new = pat.sub(lambda m: f"{m.group(1)}=[REDACTED:{label}]", redacted)
        else:
            new = pat.sub(f"[REDACTED:{label}]", redacted)
        if new != redacted:
            hit = True
            redacted = new
    return redacted, hit


def redact_many(values: Iterable[str | None]) -> tuple[list[str | None], bool]:
    """Apply redact() to each value; returns (results, any_hit)."""
    out: list[str | None] = []
    any_hit = False
    for v in values:
        r, hit = redact(v)
        out.append(r)
        any_hit = any_hit or hit
    return out, any_hit

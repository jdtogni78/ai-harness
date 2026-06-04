"""Defense-in-depth redaction for captured session text.

Mistake-prevention layer, not a security boundary. Sessions can paste tokens
into the transcript; strip the obvious patterns at ingest before anything
sees them downstream (clusters, summaries, AI extraction).

Patterns mirror perm_gate_lab/redact.py so the two stay in sync.
"""

from __future__ import annotations

import re
from typing import Pattern


_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("ANTHROPIC_KEY",  re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OPENAI_KEY",     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN",   re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("BEARER_TOKEN",   re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b")),
    ("ENV_SECRET",     re.compile(
        r"\b([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY))"
        r"\s*=\s*['\"]?([^'\"\s]+)",
    )),
    ("PEM_BLOCK",      re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    )),
]


def redact(text: str | None) -> tuple[str | None, bool]:
    """Return (redacted_text, was_redacted)."""
    if not text:
        return text, False
    out = text
    hit = False
    for label, pat in _PATTERNS:
        if label == "ENV_SECRET":
            def _sub(m: re.Match) -> str:
                return f"{m.group(1)}=<REDACTED:{label}>"
            new = pat.sub(_sub, out)
        else:
            new = pat.sub(f"<REDACTED:{label}>", out)
        if new != out:
            hit = True
            out = new
    return out, hit

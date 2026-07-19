"""Provider selection from the environment.

The whole point of this module is graceful degradation: the loop must at least
dry-run with NO paid keys (local Whisper STT + macOS `say` TTS + a keyless canned
brain). Any key that IS present upgrades that stage to a cloud provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


@dataclass(frozen=True)
class Providers:
    anthropic_key: str | None
    deepgram_key: str | None
    cartesia_key: str | None
    elevenlabs_key: str | None
    claude_model: str

    # ---- brain --------------------------------------------------------
    @property
    def has_brain(self) -> bool:
        """True when we can run Claude. Without a key the dry-run path falls
        back to a deterministic canned brain (no LLM)."""
        return self.anthropic_key is not None

    # ---- STT ----------------------------------------------------------
    @property
    def stt_choice(self) -> str:
        return "deepgram" if self.deepgram_key else "whisper"

    # ---- TTS ----------------------------------------------------------
    @property
    def tts_choice(self) -> str:
        if self.cartesia_key:
            return "cartesia"
        if self.elevenlabs_key:
            return "elevenlabs"
        return "say"

    def summary(self) -> str:
        brain = f"claude ({self.claude_model})" if self.has_brain else "canned (no key)"
        return (
            f"STT={self.stt_choice}  Brain={brain}  TTS={self.tts_choice}"
        )


def load_providers() -> Providers:
    # Best-effort .env load so users don't have to `export` everything.
    _load_dotenv()
    return Providers(
        anthropic_key=_get("ANTHROPIC_API_KEY"),
        deepgram_key=_get("DEEPGRAM_API_KEY"),
        cartesia_key=_get("CARTESIA_API_KEY"),
        elevenlabs_key=_get("ELEVENLABS_API_KEY", "ELEVEN_API_KEY"),
        # Per repo guidance default to Opus 4.8; override for lower voice latency.
        claude_model=_get("CLAUDE_MODEL") or "claude-opus-4-8",
    )


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from ./.env if python-dotenv isn't installed.

    Kept dependency-free so the keyless dry-run works before `pip install`.
    """
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except OSError:
        pass

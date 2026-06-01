"""Text-to-speech adapters.

The `say` engine (macOS) is always available and needs no credentials. The
`openai` engine produces natural neural voices — set OPENAI_API_KEY and select
it per-demo in the yaml (`tts: {engine: openai, voice: nova}`). `elevenlabs`
is still a stub.

OpenAI knobs (all optional, env-driven so the Voice dataclass stays minimal):
  OPENAI_API_KEY                  required for engine: openai
  NARRATE_OPENAI_TTS_MODEL        default gpt-4o-mini-tts (cheap, steerable)
  NARRATE_OPENAI_TTS_INSTRUCTIONS tone/style steering (gpt-4o-* models only),
                                  e.g. "Warm, calm product-demo narrator."
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ffmpeg_util import probe_duration

SAY = os.environ.get("NARRATE_SAY") or shutil.which("say") or "/usr/bin/say"


@dataclass
class Voice:
    engine: str = "say"
    name: Optional[str] = None
    rate: int = 185
    instructions: Optional[str] = None  # tone/style steering (openai gpt-4o-* TTS)


def synthesize(text: str, dst_aiff: Path, voice: Voice) -> float:
    """Write narration to dst_aiff, return its duration in seconds."""
    if voice.engine == "say":
        return _say(text, dst_aiff, voice)
    if voice.engine == "openai":
        return _openai(text, dst_aiff, voice)
    if voice.engine == "elevenlabs":
        raise NotImplementedError(
            "tts engine 'elevenlabs' not yet implemented "
            "(stubbed in tts.py — add API call here)"
        )
    raise ValueError(f"unknown tts engine: {voice.engine}")


def _say(text: str, dst: Path, voice: Voice) -> float:
    cmd = [SAY, "-r", str(voice.rate), "-o", str(dst)]
    if voice.name:
        cmd += ["-v", voice.name]
    cmd.append(text)
    subprocess.run(cmd, check=True)
    return probe_duration(dst)


def _openai(text: str, dst: Path, voice: Voice) -> float:
    """Synthesize via OpenAI's TTS API. Writes audio to dst (ffmpeg-readable —
    the runner converts to wav by content, so the .aiff name is fine)."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "engine 'openai' needs OPENAI_API_KEY in the environment "
            "(get one at platform.openai.com/api-keys; this is separate from "
            "a ChatGPT/Codex subscription)"
        )
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "the 'openai' package is not installed in the narrate venv — "
            "run: narrate/.venv/bin/pip install openai"
        ) from e

    model = os.environ.get("NARRATE_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    name = voice.name or "nova"
    # Tone steering: prefer the per-demo yaml field, fall back to the env knob.
    instructions = (voice.instructions
                    or os.environ.get("NARRATE_OPENAI_TTS_INSTRUCTIONS", "")).strip()

    kwargs = dict(model=model, voice=name, input=text, response_format="wav")
    # `instructions` (tone/style steering) is only honored by the gpt-4o-* TTS
    # models; passing it to tts-1 would error.
    if instructions and model.startswith("gpt-4o"):
        kwargs["instructions"] = instructions

    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    with client.audio.speech.with_streaming_response.create(**kwargs) as resp:
        resp.stream_to_file(str(dst))
    return probe_duration(dst)


def list_voices() -> int:
    """Print available `say` voices."""
    proc = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode

"""Text-to-speech adapters.

The `say` engine (macOS) is always available and needs no credentials. The
`openai` and `elevenlabs` adapters are stubs — wiring them up is a one-function
change once you set the env vars.
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


def synthesize(text: str, dst_aiff: Path, voice: Voice) -> float:
    """Write narration to dst_aiff, return its duration in seconds."""
    if voice.engine == "say":
        return _say(text, dst_aiff, voice)
    if voice.engine in {"openai", "elevenlabs"}:
        raise NotImplementedError(
            f"tts engine '{voice.engine}' not yet implemented "
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


def list_voices() -> int:
    """Print available `say` voices."""
    proc = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode

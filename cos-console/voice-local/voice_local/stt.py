"""Speech-to-text via whisper.cpp.

We shell out to `whisper-cli` (brew install whisper-cpp) rather than binding the
lib: it keeps deps light, uses Metal/CoreML automatically on Apple Silicon, and
is trivial to swap for faster-whisper later. Audio is fed as a 16k mono WAV.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from .config import CONFIG


def write_wav(pcm_float: np.ndarray, path: str, sample_rate: int = 16000) -> None:
    """Write mono float32 [-1,1] samples to a 16-bit PCM WAV whisper.cpp can read."""
    clipped = np.clip(pcm_float, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())


def transcribe_wav(wav_path: str) -> tuple[str, float]:
    """Transcribe a WAV file. Returns (text, seconds_elapsed)."""
    bin_ = CONFIG.whisper_bin
    model = CONFIG.whisper_model
    if not Path(model).exists():
        raise FileNotFoundError(
            f"Whisper model not found: {model}\n"
            f"Run scripts/download_models.sh to fetch it."
        )
    # -nt: no timestamps, -np: no progress prints, output plain text to stdout.
    cmd = [bin_, "-m", model, "-f", wav_path, "-nt", "-np", "-l", "en"]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        raise RuntimeError(f"whisper-cli failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip(), elapsed


def transcribe_pcm(pcm_float: np.ndarray, sample_rate: int = 16000) -> tuple[str, float]:
    """Transcribe in-memory audio. Returns (text, seconds_elapsed)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
        write_wav(pcm_float, tmp.name, sample_rate)
        return transcribe_wav(tmp.name)

"""Mic capture with a simple energy-based VAD.

Records from the default input at 16k mono, waits for speech to start (RMS above
threshold), then captures until a trailing silence gap. Deliberately dependency-
light (just sounddevice + numpy). A production build would use webrtcvad or Silero,
but energy VAD is enough to demonstrate the local loop and to time it.

`monitor_speech()` supports barge-in: it returns True as soon as it hears speech,
so the main loop can interrupt TTS playback.
"""
from __future__ import annotations

import time

import numpy as np

from .config import CONFIG

try:
    import sounddevice as sd

    HAVE_SD = True
except Exception:  # sounddevice needs portaudio; POC still works in --text mode
    HAVE_SD = False


BLOCK_MS = 30
BLOCK_SAMPLES = CONFIG.sample_rate * BLOCK_MS // 1000


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block)) + 1e-12))


def record_utterance(max_seconds: float | None = None) -> np.ndarray:
    """Block until the user speaks and finishes. Returns float32 mono @ 16k.

    Raises RuntimeError if sounddevice/portaudio is unavailable.
    """
    if not HAVE_SD:
        raise RuntimeError(
            "sounddevice/portaudio unavailable — install portaudio (brew install portaudio) "
            "and `pip install sounddevice`, or use --text mode."
        )
    max_seconds = max_seconds or CONFIG.max_utterance_s
    silence_blocks_needed = CONFIG.vad_silence_ms // BLOCK_MS
    frames: list[np.ndarray] = []
    started = False
    silent_run = 0
    t_start = time.perf_counter()

    with sd.InputStream(samplerate=CONFIG.sample_rate, channels=1, dtype="float32",
                        blocksize=BLOCK_SAMPLES) as stream:
        while True:
            block, _ = stream.read(BLOCK_SAMPLES)
            block = block.reshape(-1)
            loud = _rms(block) > CONFIG.vad_rms_threshold

            if not started:
                if loud:
                    started = True
                    frames.append(block.copy())
                elif time.perf_counter() - t_start > max_seconds:
                    return np.zeros(0, dtype="float32")  # nothing said
            else:
                frames.append(block.copy())
                silent_run = silent_run + 1 if not loud else 0
                if silent_run >= silence_blocks_needed:
                    break
                if time.perf_counter() - t_start > max_seconds:
                    break

    return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")


def monitor_speech(stop_flag) -> bool:
    """Listen for the onset of speech (barge-in). Returns True if speech detected.

    Runs until `stop_flag()` returns True (e.g. TTS finished). Non-fatal if no mic.
    """
    if not HAVE_SD:
        return False
    consecutive = 0
    with sd.InputStream(samplerate=CONFIG.sample_rate, channels=1, dtype="float32",
                        blocksize=BLOCK_SAMPLES) as stream:
        while not stop_flag():
            block, _ = stream.read(BLOCK_SAMPLES)
            if _rms(block.reshape(-1)) > CONFIG.vad_rms_threshold * 1.3:
                consecutive += 1
                if consecutive >= 3:  # ~90ms of speech = real, not a click
                    return True
            else:
                consecutive = 0
    return False

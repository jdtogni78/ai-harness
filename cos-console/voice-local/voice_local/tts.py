"""Text-to-speech: Piper (primary, fast + fully local) with macOS `say` fallback.

Both engines run 100% on-device, so the privacy promise holds regardless. `say`
lets the POC talk before Piper/voices are installed; Piper sounds noticeably more
natural. Playback returns a handle so the main loop can interrupt it (barge-in).
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from .config import CONFIG


class Speech:
    """A running TTS playback that can be stopped mid-sentence (for barge-in)."""

    def __init__(self, proc: subprocess.Popen, engine: str):
        self._proc = proc
        self.engine = engine
        self._t0 = time.perf_counter()

    def is_playing(self) -> bool:
        return self._proc.poll() is None

    def wait(self) -> float:
        self._proc.wait()
        return time.perf_counter() - self._t0

    def stop(self) -> None:
        if self.is_playing():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _speak_say(text: str) -> Speech:
    proc = subprocess.Popen(["say", "-v", CONFIG.say_voice, text])
    return Speech(proc, "say")


def _speak_piper(text: str) -> Speech:
    """piper synth -> stdout raw s16le -> afplay-compatible pipe.

    Piper emits raw 22.05k mono s16le on stdout with --output-raw; we pipe it into
    `ffplay`/`sox play` if present, else fall back through a temp WAV + afplay.
    """
    import shutil

    voice = CONFIG.piper_voice
    # Prefer streaming raw audio into a player for low latency.
    player = None
    if shutil.which("ffplay"):
        player = ["ffplay", "-hide_banner", "-loglevel", "quiet", "-nodisp",
                  "-autoexit", "-f", "s16le", "-ar", "22050", "-ch_layout", "mono", "-"]
    elif shutil.which("play"):  # sox
        player = ["play", "-q", "-t", "raw", "-r", "22050", "-e", "signed",
                  "-b", "16", "-c", "1", "-"]

    if player is not None:
        piper = subprocess.Popen(
            [CONFIG.piper_bin, "-m", voice, "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        play = subprocess.Popen(player, stdin=piper.stdout, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        piper.stdout.close()  # let play own the pipe

        def _feed():
            try:
                piper.stdin.write(text.encode("utf-8"))
                piper.stdin.close()
            except BrokenPipeError:
                pass

        threading.Thread(target=_feed, daemon=True).start()
        return Speech(play, "piper")

    # No streaming player: synth to a temp WAV, then afplay it.
    import tempfile

    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    synth = subprocess.run(
        [CONFIG.piper_bin, "-m", voice, "-f", wav],
        input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if synth.returncode != 0 or not Path(wav).exists():
        raise RuntimeError("piper synthesis failed; check VL_PIPER_BIN / VL_PIPER_VOICE")
    proc = subprocess.Popen(["afplay", wav])
    return Speech(proc, "piper")


def speak(text: str) -> Speech:
    """Start speaking `text`. Returns a Speech handle (non-blocking)."""
    text = text.strip()
    if not text:
        return _speak_say("")  # no-op
    engine = CONFIG.tts_engine
    if engine == "piper" and CONFIG.piper_available():
        try:
            return _speak_piper(text)
        except Exception as e:  # pragma: no cover - defensive fallback
            print(f"[tts] piper failed ({e}); falling back to macOS say")
    return _speak_say(text)


def speak_blocking(text: str) -> float:
    """Speak and block until done. Returns seconds elapsed."""
    return speak(text).wait()

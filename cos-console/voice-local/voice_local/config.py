"""Central config. All knobs are env-overridable so the POC can dry-run without secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional for a bare dry-run
    pass

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


@dataclass
class Config:
    # --- Brain (the one network hop) ---
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    model: str = _env("VL_MODEL", "claude-opus-4-8")
    max_tokens: int = int(_env("VL_MAX_TOKENS", "1024"))

    # --- STT: whisper.cpp ---
    # `whisper-cli` (brew install whisper-cpp) or a full path.
    whisper_bin: str = _env("VL_WHISPER_BIN", "whisper-cli")
    # base.en = fast + good enough for commands; small.en = more accurate.
    whisper_model: str = _env("VL_WHISPER_MODEL", str(MODELS_DIR / "ggml-base.en.bin"))

    # --- TTS: piper, with macOS `say` as a local fallback ---
    # "piper" | "say"
    tts_engine: str = _env("VL_TTS_ENGINE", "piper")
    piper_bin: str = _env("VL_PIPER_BIN", "piper")
    piper_voice: str = _env("VL_PIPER_VOICE", str(MODELS_DIR / "en_US-lessac-medium.onnx"))
    say_voice: str = _env("VL_SAY_VOICE", "Samantha")

    # --- Audio capture / VAD ---
    sample_rate: int = 16000  # whisper wants 16k mono
    # simple energy VAD: RMS threshold + silence timeout ends an utterance
    vad_silence_ms: int = int(_env("VL_VAD_SILENCE_MS", "800"))
    vad_rms_threshold: float = float(_env("VL_VAD_RMS", "0.012"))
    max_utterance_s: float = float(_env("VL_MAX_UTTERANCE_S", "15"))

    def has_key(self) -> bool:
        return bool(self.anthropic_api_key)

    def piper_available(self) -> bool:
        import shutil

        return bool(shutil.which(self.piper_bin)) and Path(self.piper_voice).exists()

    def whisper_available(self) -> bool:
        import shutil

        return bool(shutil.which(self.whisper_bin) or Path(self.whisper_bin).exists()) and Path(
            self.whisper_model
        ).exists()


CONFIG = Config()

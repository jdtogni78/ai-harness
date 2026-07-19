"""voice-local: fully-local voice loop (Whisper.cpp STT -> Claude brain -> Piper TTS).

Approach C of the cos-console Wave 1 exploration: audio never leaves the machine;
the only network hop is the Claude API call (the brain).
"""

__version__ = "0.1.0"

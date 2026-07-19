"""A macOS `say` TTS service for Pipecat.

Pipecat ships Cartesia/ElevenLabs/etc. but no local `say` service, so this is the
zero-key TTS fallback (the same engine narrate-demo uses). It shells out to
`say`, writes 16-bit mono PCM WAV at the pipeline sample rate, and streams the
audio back as `TTSAudioRawFrame`s.

`say` is not streaming — it renders the whole utterance before we emit audio — so
first-audio latency is higher than a true streaming TTS. That's an accepted
tradeoff for a keyless fallback and is called out in FINDINGS.md.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import AsyncGenerator

from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.tts_service import TTSService

_WAV_HEADER_BYTES = 44
_CHUNK_BYTES = 8192  # ~170ms at 24kHz/16-bit mono


class SayTTSService(TTSService):
    """Text-to-speech via the macOS `say` binary."""

    def __init__(
        self,
        *,
        voice: str = "Samantha",
        sample_rate: int = 24000,
        **kwargs,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._voice = voice

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: generating TTS via `say`: {text!r}")
        rate = self.sample_rate
        tmp_path = None
        try:
            await self.start_ttfb_metrics()
            yield TTSStartedFrame()

            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            proc = await asyncio.create_subprocess_exec(
                "say",
                "-v",
                self._voice,
                "-o",
                tmp_path,
                "--data-format",
                f"LEI16@{rate}",
                "--file-format",
                "WAVE",
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                msg = stderr.decode(errors="replace").strip()
                yield ErrorFrame(f"`say` failed ({proc.returncode}): {msg}")
                return

            await self.stop_ttfb_metrics()
            await self.start_tts_usage_metrics(text)

            with open(tmp_path, "rb") as fh:
                fh.read(_WAV_HEADER_BYTES)  # skip RIFF/WAVE header
                while True:
                    chunk = fh.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield TTSAudioRawFrame(
                        audio=chunk, sample_rate=rate, num_channels=1
                    )
        except Exception as exc:  # noqa: BLE001 - surface as a pipeline frame
            logger.exception("SayTTSService error")
            yield ErrorFrame(f"SayTTSService error: {exc}")
        finally:
            yield TTSStoppedFrame()
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

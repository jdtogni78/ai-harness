"""Live speech-to-speech agent on the OpenAI Realtime API (Approach B).

The Realtime (GPT) model is the fast conversational I/O and does server-side
VAD + barge-in. Claude is bolted on as the ``get_project_status`` reasoning
tool (see tools.py / claude_tool.py).

Audio: 24 kHz mono PCM16 in/out via sounddevice (PortAudio). Requires
OPENAI_API_KEY. Run the dry-run path instead if you have no key/mic.

    python main.py live
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import sys

from tools import GET_PROJECT_STATUS_TOOL, handle_tool_call

REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "cedar")
SAMPLE_RATE = 24000
CHANNELS = 1
BLOCK = 1200  # ~50ms frames

INSTRUCTIONS = (
    "You are a spoken 'chief of staff' for a software team. Keep replies short and "
    "conversational — you are talking out loud. For ANYTHING about a project's "
    "status, tickets, tests, deploys, decisions, or what's blocked, you MUST call "
    "the get_project_status tool and speak its answer; do not guess. Pass the "
    "operator's verbatim question. Be proactive about risk."
)


class Speaker:
    """Threaded PCM16 playback with flush-on-barge-in."""

    def __init__(self):
        import sounddevice as sd

        self._q: queue.Queue[bytes] = queue.Queue()
        self._buf = b""
        self._stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=BLOCK, callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):  # noqa: ARG002
        need = frames * 2  # int16 = 2 bytes
        while len(self._buf) < need:
            try:
                self._buf += self._q.get_nowait()
            except queue.Empty:
                break
        if len(self._buf) >= need:
            outdata[:] = self._buf[:need]
            self._buf = self._buf[need:]
        else:
            outdata[: len(self._buf)] = self._buf
            outdata[len(self._buf) :] = b"\x00" * (need - len(self._buf))
            self._buf = b""

    def play(self, pcm: bytes) -> None:
        self._q.put(pcm)

    def flush(self) -> None:
        """Barge-in: drop everything queued so the agent stops mid-sentence."""
        self._buf = b""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


async def _mic_pump(ws, loop):
    """Capture mic frames and stream them to the Realtime input buffer."""
    import sounddevice as sd

    q: queue.Queue[bytes] = queue.Queue()

    def cb(indata, frames, time_info, status):  # noqa: ARG001
        q.put(bytes(indata))

    with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                           blocksize=BLOCK, callback=cb):
        while True:
            chunk = await loop.run_in_executor(None, q.get)
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode(),
            }))


async def _handle_events(ws, speaker: Speaker):
    async for raw in ws:
        ev = json.loads(raw)
        etype = ev.get("type", "")

        if etype == "response.audio.delta":
            speaker.play(base64.b64decode(ev["delta"]))

        elif etype == "input_audio_buffer.speech_started":
            # Operator started talking over the agent -> barge-in.
            speaker.flush()
            print("\033[2m[barge-in]\033[0m", flush=True)

        elif etype == "response.audio_transcript.done":
            print(f"\033[1m🔊 agent:\033[0m {ev.get('transcript', '')}")

        elif etype == "conversation.item.input_audio_transcription.completed":
            print(f"\033[1m🎙  operator:\033[0m {ev.get('transcript', '')}")

        elif etype == "response.function_call_arguments.done":
            name = ev.get("name", "")
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"\033[2m[tool] {name}({json.dumps(args)})\033[0m")
            output = handle_tool_call(name, args)
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": ev.get("call_id"),
                    "output": output,
                },
            }))
            await ws.send(json.dumps({"type": "response.create"}))

        elif etype == "error":
            print(f"\033[31m[realtime error]\033[0m {ev.get('error')}", file=sys.stderr)


async def _run() -> None:
    import websockets

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set. Use `python main.py dryrun` for a keyless demo.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to Realtime ({REALTIME_MODEL}, voice={VOICE})…")
    async with websockets.connect(
        REALTIME_URL,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
        },
        max_size=None,
    ) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "instructions": INSTRUCTIONS,
                "voice": VOICE,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {"type": "server_vad", "silence_duration_ms": 500},
                "tools": [GET_PROJECT_STATUS_TOOL],
                "tool_choice": "auto",
            },
        }))

        speaker = Speaker()
        loop = asyncio.get_running_loop()
        print("🎧 Listening. Try: “what's the status on dstrader?”  (Ctrl-C to quit)\n")
        try:
            await asyncio.gather(_mic_pump(ws, loop), _handle_events(ws, speaker))
        finally:
            speaker.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nbye.")

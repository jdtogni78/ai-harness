"""Entry point: the local voice loop.

Modes:
  --text     type instead of speaking (works with no mic, no models; still hits Claude)
  --once     one turn then exit (handy for latency timing)
  --self-test dry-run every stage without the API key or models (CI-friendly)

Pipeline:  mic -> whisper.cpp -> Claude -> Piper/say -> speaker
Only the Claude call leaves the machine.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from . import audio, stt, tts
from .brain import Brain
from .config import CONFIG
from .status_stub import get_project_status


def _banner() -> None:
    print("=" * 60)
    print(" voice-local — fully local voice loop (audio stays on-device)")
    print(f"   STT: whisper.cpp   available={CONFIG.whisper_available()}")
    print(f"   TTS: {CONFIG.tts_engine:<6}          piper_available={CONFIG.piper_available()}")
    print(f"   Brain: {CONFIG.model}   key_set={CONFIG.has_key()}")
    print("=" * 60)


def _speak_with_bargein(text: str) -> None:
    """Speak `text`; if the user starts talking, cut playback (barge-in demo)."""
    speech = tts.speak(text)
    interrupted = {"v": False}

    def watch():
        if audio.monitor_speech(lambda: not speech.is_playing()):
            interrupted["v"] = True
            speech.stop()

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    speech.wait()
    t.join(timeout=0.1)
    if interrupted["v"]:
        print("   (barge-in: stopped talking to listen)")


def run_once(brain: Brain, user_text: str, use_voice_out: bool) -> None:
    t0 = time.perf_counter()
    reply, timing = brain.respond(user_text)
    print(f"\nassistant> {reply}")
    print(f"   [brain {timing['api_s']:.2f}s, {timing['tool_calls']} tool call(s)]")
    if use_voice_out:
        t_tts0 = time.perf_counter()
        tts.speak_blocking(reply)
        print(f"   [tts {time.perf_counter() - t_tts0:.2f}s]")
    print(f"   [turn total {time.perf_counter() - t0:.2f}s]")


def loop_text(brain: Brain, use_voice_out: bool, once: bool) -> None:
    print("(type a message, or 'quit')")
    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if user.lower() in {"quit", "exit"}:
            return
        if not user:
            continue
        run_once(brain, user, use_voice_out)
        if once:
            return


def loop_voice(brain: Brain, once: bool) -> None:
    if not CONFIG.whisper_available():
        print("!! whisper model missing — run scripts/download_models.sh, or use --text")
        return
    print("(speak after the prompt; Ctrl-C to quit)")
    while True:
        try:
            print("\n[listening…]")
            pcm = audio.record_utterance()
            if pcm.size == 0:
                continue
            text, stt_s = stt.transcribe_pcm(pcm, CONFIG.sample_rate)
            if not text:
                continue
            print(f"you> {text}   [stt {stt_s:.2f}s]")
            reply, timing = brain.respond(text)
            print(f"assistant> {reply}   [brain {timing['api_s']:.2f}s]")
            _speak_with_bargein(reply)
        except KeyboardInterrupt:
            print("\nbye.")
            return
        if once:
            return


def self_test() -> int:
    """Exercise every stage that doesn't need secrets/models. Returns exit code."""
    print("[self-test] status stub…")
    rep = get_project_status("dstrader")
    assert rep["project"] == "dstrader" and "tests" in rep, "status stub shape wrong"
    print(f"   ok: dstrader has {rep['tests']['count']} tests, "
          f"{rep['tickets']['in_progress']} tickets in progress")

    print("[self-test] unknown project returns valid empty report…")
    empty = get_project_status("nope")
    assert empty["tickets"]["todo"] == 0 and empty["open_questions"], "empty report shape wrong"
    print("   ok")

    print("[self-test] tts (macOS say, local)…")
    prev = CONFIG.tts_engine
    CONFIG.tts_engine = "say"
    tts.speak_blocking("Self test.")
    CONFIG.tts_engine = prev
    print("   ok: spoke a line locally")

    print("[self-test] brain wiring (no API call)…")
    b = Brain()
    assert b.history == [] and len(__import__("voice_local.brain", fromlist=["TOOLS"]).TOOLS) == 1
    print("   ok: brain has exactly one tool (get_project_status)")

    print("\n[self-test] PASS — pipeline wiring is sound. "
          "Add ANTHROPIC_API_KEY + models for the full loop.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="voice-local: fully-local voice loop")
    ap.add_argument("--text", action="store_true", help="type input instead of speaking")
    ap.add_argument("--no-voice-out", action="store_true", help="print replies, don't speak them")
    ap.add_argument("--once", action="store_true", help="one turn then exit")
    ap.add_argument("--self-test", action="store_true", help="dry-run wiring, no key/models needed")
    ap.add_argument("--say", metavar="TEXT", help="one-shot: send TEXT to the brain and exit")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    _banner()
    brain = Brain()

    if args.say is not None:
        if not CONFIG.has_key():
            print("!! ANTHROPIC_API_KEY not set."); return 2
        run_once(brain, args.say, use_voice_out=not args.no_voice_out)
        return 0

    if not CONFIG.has_key():
        print("!! ANTHROPIC_API_KEY not set — set it in .env (see .env.example). "
              "You can still run --self-test.")
        return 2

    if args.text:
        loop_text(brain, use_voice_out=not args.no_voice_out, once=args.once)
    else:
        loop_voice(brain, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())

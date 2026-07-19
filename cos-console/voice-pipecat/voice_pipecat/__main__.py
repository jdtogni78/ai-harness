"""voice_pipecat CLI.

    python -m voice_pipecat                 # live mic→STT→Claude→TTS→speaker loop
    python -m voice_pipecat --dry-run       # no mic; scripted demo, keyless-capable
    python -m voice_pipecat --dry-run --text "give me a status on dstrader"
    python -m voice_pipecat --dry-run --no-audio   # headless (no speaker output)

The dry-run path never imports Pipecat, so it works before `pip install` and
without any API key (canned brain + macOS `say`). If ANTHROPIC_API_KEY is set and
the `anthropic` SDK is installed, dry-run instead exercises the *real* Claude
tool-use turn headlessly.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

from .brain import CannedBrain
from .config import Providers, load_providers

# The demo the brief asks for: status request, one follow-up, plus a turn that
# exercises W0's anti-fabrication guardrail (cos-console has no test data → the
# console SAYS so instead of reading a fabricated "0 passing").
DEMO_SCRIPT = [
    "give me a status on dstrader",
    "how many tests?",
    "how many tests does cos-console have?",
]


def _speak(text: str, enabled: bool, voice: str = "Samantha") -> None:
    if not enabled:
        return
    try:
        subprocess.run(["say", "-v", voice, text], check=False)
    except FileNotFoundError:
        print("  (no `say` binary — skipping audio)", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Dry-run: keyless canned brain                                               #
# --------------------------------------------------------------------------- #
def _dry_run_canned(utterances: list[str], audio: bool) -> None:
    brain = CannedBrain()
    for u in utterances:
        reply = brain.reply(u)
        print(f"\n🎙  operator: {u}")
        print(f"🔊 console : {reply}")
        _speak(reply, audio)


# --------------------------------------------------------------------------- #
# Dry-run: real Claude tool-use, headless (no mic)                            #
# --------------------------------------------------------------------------- #
def _dry_run_claude(p: Providers, utterances: list[str], audio: bool) -> bool:
    """Return True if the real-brain path ran; False to fall back to canned."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False

    from anthropic import Anthropic

    from .brain import SYSTEM_PROMPT
    from .status_tool import anthropic_tool_dict, get_status_report

    client = Anthropic(api_key=p.anthropic_key)
    tools = [anthropic_tool_dict()]
    messages: list[dict] = []

    print(f"[real Claude brain: {p.claude_model}]")
    for u in utterances:
        print(f"\n🎙  operator: {u}")
        messages.append({"role": "user", "content": u})

        # Tool-use loop: run until Claude stops asking for the status tool.
        while True:
            resp = client.messages.create(
                model=p.claude_model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break

            results = []
            for tu in tool_uses:
                project = (tu.input or {}).get("project", "")
                print(f"   ↳ tool get_project_status({project!r})")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": _json(get_status_report(project)),
                    }
                )
            messages.append({"role": "user", "content": results})

        reply = " ".join(b.text for b in resp.content if b.type == "text").strip()
        print(f"🔊 console : {reply}")
        _speak(reply, audio)
    return True


def _json(obj) -> str:
    import json

    return json.dumps(obj)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice_pipecat")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No mic. Scripted demo; keyless canned brain unless ANTHROPIC_API_KEY is set.",
    )
    parser.add_argument(
        "--text",
        action="append",
        help="Utterance to feed in dry-run (repeatable). Defaults to the two-turn demo.",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Dry-run: don't speak replies through the speaker (headless/CI).",
    )
    args = parser.parse_args(argv)

    p = load_providers()
    print(f"Providers: {p.summary()}\n")

    utterances = args.text if args.text else DEMO_SCRIPT
    audio = not args.no_audio

    if args.dry_run:
        if p.has_brain and _dry_run_claude(p, utterances, audio):
            return 0
        if p.has_brain:
            print("(anthropic SDK not installed — using canned brain)\n")
        _dry_run_canned(utterances, audio)
        return 0

    # Live mode.
    if not p.has_brain:
        print(
            "Live mode needs ANTHROPIC_API_KEY (Claude is the brain).\n"
            "Try the keyless demo:  python -m voice_pipecat --dry-run\n",
            file=sys.stderr,
        )
        return 2
    try:
        from .bot import run_live
    except ImportError as exc:
        print(
            f"Pipecat not installed ({exc}).\n"
            "Install deps:  pip install -r requirements.txt\n"
            "Or run the keyless demo:  python -m voice_pipecat --dry-run",
            file=sys.stderr,
        )
        return 2

    asyncio.run(run_live(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

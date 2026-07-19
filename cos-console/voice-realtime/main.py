"""voice-realtime POC entrypoint (Approach B: speech-to-speech + Claude-as-tool).

    python main.py dryrun                     # keyless transcript-only demo
    python main.py dryrun --ask "status on dstrader"
    python main.py live                       # real mic/speaker (needs OPENAI_API_KEY)

See README.md for setup and FINDINGS.md for the evaluation.
"""

from __future__ import annotations

import argparse
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-realtime")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dryrun", help="transcript-only, no audio/keys")
    p_dry.add_argument("--ask", help="single utterance to run instead of the scripted demo")

    sub.add_parser("live", help="live mic/speaker via OpenAI Realtime API")

    args = parser.parse_args()

    if args.cmd == "dryrun":
        import dryrun

        dryrun.main(ask=args.ask)
    elif args.cmd == "live":
        import realtime_agent

        realtime_agent.main()
    else:  # pragma: no cover
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

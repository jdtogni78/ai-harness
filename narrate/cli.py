"""Subcommand dispatch for ``python3 -m narrate <name> [args]``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

USAGE = "usage: python3 -m narrate <render|init|preview|voices> [args]"


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "render":
        return _render(rest)
    if cmd == "init":
        return _init(rest)
    if cmd == "preview":
        return _preview(rest)
    if cmd == "voices":
        from .tts import list_voices
        return list_voices()
    print(f"unknown subcommand: {cmd}\n{USAGE}", file=sys.stderr)
    return 2


def _render(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="narrate render")
    ap.add_argument("script", type=Path, help="path to demo .yaml")
    ap.add_argument("--headed", action="store_true",
                    help="run browser headed (default: headless)")
    args = ap.parse_args(argv)
    from .runner import run
    from .script import load
    script = load(args.script)
    run(script, headless=not args.headed, record=True)
    return 0


def _preview(argv: List[str]) -> int:
    """Headed, no recording — for iterating on selectors and timing."""
    ap = argparse.ArgumentParser(prog="narrate preview")
    ap.add_argument("script", type=Path, help="path to demo .yaml")
    args = ap.parse_args(argv)
    from .runner import run
    from .script import load
    script = load(args.script)
    run(script, headless=False, record=False)
    return 0


def _init(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="narrate init")
    ap.add_argument("path", type=Path,
                    help="output yaml path, e.g. demos/login.demo.yaml")
    ap.add_argument("--title", default=None, help="title (default: derived from filename)")
    args = ap.parse_args(argv)
    from .script import scaffold
    if args.path.exists():
        print(f"refusing to overwrite {args.path}", file=sys.stderr)
        return 1
    args.path.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or args.path.stem.replace(".demo", "").replace("-", " ").title()
    scaffold(args.path, title=title)
    print(f"created {args.path}")
    return 0

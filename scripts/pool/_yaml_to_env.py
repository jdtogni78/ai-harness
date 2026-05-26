#!/usr/bin/env python3
# Read a pool config YAML and print shell-evalable export lines.
#
# Scalars  -> KEY=value        (single-quoted, $/`/\ in values left literal)
# Lists    -> KEY=(a b c)      (each item shlex-quoted)
# Maps     -> recursed, keys joined with `_` and upper-cased
# Leading `~` in string values is expanded to $HOME so the YAML stays portable.

import os
import shlex
import sys

import yaml


def emit(prefix: str, val) -> None:
    if isinstance(val, dict):
        for k, v in val.items():
            emit(f"{prefix}_{k.upper()}" if prefix else k.upper(), v)
        return
    if isinstance(val, list):
        items = " ".join(shlex.quote(_scalar(x)) for x in val)
        print(f"{prefix}=({items})")
        return
    print(f"{prefix}={shlex.quote(_scalar(val))}")


def _scalar(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if s.startswith("~"):
        s = os.path.expanduser(s)
    return s


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: _yaml_to_env.py <config.yaml>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        print("config root must be a mapping", file=sys.stderr)
        return 2
    for k, v in data.items():
        emit(k.upper(), v)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

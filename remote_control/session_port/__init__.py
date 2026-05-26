"""Port agent sessions between Codex and Claude Code.

`codex.py` parses Codex rollout JSONL, `claude.py` builds Claude Code session
JSONL, and `cli.py` wires them into the ``codex-import`` subcommand. The parsing
and building halves are pure (no I/O) so they unit-test without a filesystem;
the rollout-dir reads live in `disk.py` (shared with the unified inventory).
"""

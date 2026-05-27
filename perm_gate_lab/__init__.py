"""perm_gate_lab — corpus + judge-evaluation harness for the perm_gate hook.

Phase 1 scope: capture every decision the production PreToolUse hook makes,
import it into a SQLite corpus, and surface it via a small CLI. UI, judge
runner, and LLM-as-judge scorer come in later phases (see PLAN.md).
"""

__version__ = "0.1.0"

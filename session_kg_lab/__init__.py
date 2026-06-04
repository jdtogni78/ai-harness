"""session_kg_lab — knowledge graph over archived Claude Code sessions.

Phase 1 scope: deterministic ingestion of ~/.claude/projects/*/sessions/*.jsonl
into a SQLite graph (sessions + regex-extracted entities + relations). No LLM
cost. Embeddings, LightRAG extraction, retrieval, and eval come in later
phases (see ../docs/session-kg.md and tickets #74-#80).
"""

__version__ = "0.1.0"

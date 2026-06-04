# session-kg — Knowledge Graph over Archived Claude Code Sessions (POC)

Build a queryable knowledge graph of every Claude Code session ever run on this
account so we can answer questions like:

- *Which sessions touched the supervisor and ended on a usage-limit error?*
- *What decisions did I land last quarter that aren't yet in `DECISIONS.md`?*
- *Show me every session that worked on a ticket linked to `perm-gate`.*
- *Cluster my sessions by theme — what have I actually been spending time on?*

Related: ai-harness#23 (perm_gate, closed) and `perm_gate_lab/` are the precedent
for "subdir + PLAN.md + cli/db/importer/models" POC layout.

## Decisions locked in

- **Lives in** `ai-harness/session_kg_lab/` (new module, CLI `skgctl`).
- **Engine** is **LightRAG** (graph + dual retrieval) running locally, not Microsoft
  GraphRAG (too expensive) and not Zep/Graphiti (heavier infra, temporal semantics
  we don't yet need). LightRAG gives 70–90% of GraphRAG's quality at ~1/100th the
  index cost.
- **Extraction LLM** is **Haiku 4.5** via the Anthropic API. Bounded spend, with
  aggressive prompt caching keyed by `(session_sha, prompt_sha, model)`.
- **Embeddings** are **BGE-small** (or `all-MiniLM-L6-v2`) running locally — free,
  fast, good enough for clustering. Pluggable later for Voyage / text-embedding-3-small.
- **Storage** is SQLite at `~/.session-kg/kg.db` for the structured graph + a
  vector column (sqlite-vec) for embeddings. No Neo4j for the POC — revisit only
  if traversal performance pushes us off SQLite.
- **Ingestion source** is `~/.claude/projects/*/sessions/*.jsonl`. The
  `session-triage` skill already walks this path — reuse its parser, don't
  re-invent.
- **Cross-cutting note**: this is repo-local (ai-harness state), so it stays in
  `docs/`. No `GD-NNNN` entry needed unless a sibling repo wants to join the graph.

## Tiered approach

The whole point is to layer cheap → expensive, so each phase ships on its own and
the cheap layers carry weight even if we never run the expensive ones.

| Tier | Cost | What it produces |
|------|------|------------------|
| 0 — deterministic | free | regex-extracted entities (repo, file, ticket `#NN`, `cse_*`, sha, error class, tool); co-occurrence relations |
| 1 — classical NLP | free | BM25 topic terms; local embeddings; HDBSCAN session clusters; spaCy NER for off-pattern names |
| 2 — AI extraction | bounded $ | Haiku per-session `{summary, goal, outcome, decisions[], blockers[]}`; Sonnet per-cluster cross-session synthesis |

Recommended hybrid: Tier 0 builds the skeleton, Tier 1 clusters it, Tier 2 (Haiku)
enriches nodes, Tier 2 (Sonnet) writes edges between clusters. Spend is bounded by
*clusters*, not *sessions*.

## Storage schema (SQLite)

```
session(id, cse_id, started_at, ended_at, host, cwd, repo, branch,
        title, model, ended_reason, sha256, jsonl_path)

entity(id, type, name, canonical, first_seen, last_seen)
       -- type ∈ {repo, file, ticket, cse, commit, tool, error_class,
                  decision, person, concept}

relation(src_entity_id, dst_entity_id, type, session_id, turn_idx, weight)
         -- type ∈ {touched, mentioned, fixed, broke, blocked_by,
                    spawned, depends_on, references}

embedding(id, owner_type, owner_id, vec)  -- BLOB (sqlite-vec)
       -- owner_type ∈ {session, turn, summary}

summary(id, owner_type, owner_id, model, prompt_sha, content, cost_usd, ts)

cluster(id, method, label, summary_id, ts)
cluster_member(cluster_id, owner_type, owner_id, score)

community(id, level, label, summary_id, parent_id)  -- LightRAG hierarchy
community_member(community_id, entity_id)
```

Dedupe entities by `(type, canonical)`. Dedupe summaries by `prompt_sha` to keep
re-runs cheap.

## Phases (each shippable)

Parent epic: ai-harness#74.

1. **Ingest + Tier 0 extraction** (ai-harness#75). `skgctl ingest` walks
   `~/.claude/projects/*/sessions/*.jsonl`, populates `session` + regex-extracted
   entities/relations. `skgctl stats` and `skgctl q "..."` for simple lookups.
2. **Tier 1 embeddings + clusters** (ai-harness#76). Embed sessions with
   BGE-small; HDBSCAN clusters; BM25 topic terms per cluster. `skgctl cluster`
   and `skgctl themes`.
3. **Tier 2 LightRAG integration** (ai-harness#77). Run LightRAG on the corpus
   with Haiku as the extraction LLM. Persist its entity/relation/community
   output into our schema (so we still own the data).
4. **Retrieval CLI + reranker** (ai-harness#78). `skgctl ask "..."` does query
   expansion → BM25-top-20 + vector-top-20 → RRF merge → cross-encoder rerank
   → top-K → Sonnet answer with citations to session/turn.
5. **Eval harness** (ai-harness#79). Hand-write 20–30 gold-set queries with
   known answers from actual session history; report Recall@5, MRR@3, and a
   Haiku-judged answer score.
6. **manager-ui browse tab (stretch)** (ai-harness#80). Read-only tab on
   `:8765` to browse sessions, clusters, and the graph.

## Reference landscape

### Directly applicable (Claude Code sessions specifically)

- [claude-session-index (GitHub)](https://github.com/lee-fuhr/claude-session-index)
  — closest existing OSS: SQLite + FTS + cross-session synthesis. Read its
  ingestion code as a Tier 0 baseline.
- [Claude Code History Viewer (GitHub)](https://github.com/jhlee0409/claude-code-history-viewer)
  — desktop UI across Claude Code / Codex / Cursor / Aider. Read-only.
- [Session History & Analytics Skill](https://mcpmarket.com/tools/skills/session-history-analytics)
- [Session Logs Skill](https://mcpmarket.com/tools/skills/session-log-analytics)
- [Session Finder for Claude Code](https://mcpmarket.com/tools/skills/session-finder-for-claude-code)
- [I Tested 4 Tools for Browsing Claude Code Session History (dev.to)](https://dev.to/gonewx/i-tested-4-tools-for-browsing-claude-code-session-history-17ie)
- [My Memories — offline 3D KG from chat exports (GitHub)](https://github.com/wednesday-solutions/my-memories)
  — closest in *spirit*: offline, KG, semantic search, multi-tool ingestion.

### KG-memory products (hosted / OSS engines)

- [Zep — agent memory at enterprise scale](https://www.getzep.com/)
- [Graphiti: Knowledge graph memory (Neo4j blog)](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)
- [Beyond Chat Memory (Zep blog)](https://blog.getzep.com/ai-knowledge-graph-memory/)
- [Mem0 — vector-first with optional graph layer](https://mem0.ai/blog/graph-memory-solutions-ai-agents)
- [Zep vs Mem0 benchmarks](https://atlan.com/know/zep-vs-mem0/)
- [Mem0 vs Zep vs LangMem vs MemoClaw (2026)](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- [Cognee — AI Memory Tools Evaluation](https://www.cognee.ai/blog/deep-dives/ai-memory-tools-evaluation)
- [Lenny's Memory (Neo4j blog)](https://neo4j.com/blog/developer/meet-lennys-memory-building-context-graphs-for-ai-agents/)
- [Supermemory](https://supermemory.ai/)

### GraphRAG engines (what we'd run)

- [Microsoft GraphRAG (GitHub)](https://github.com/microsoft/graphrag) — reference impl, expensive.
- [Project GraphRAG (Microsoft Research)](https://www.microsoft.com/en-us/research/project/graphrag/)
- [Graph RAG in 2026: What Works in Production (Paperclipped)](https://www.paperclipped.de/en/blog/graph-rag-production/)
- [GraphRAG in 2026 buyer's guide (Tongbing/Medium)](https://medium.com/@tongbing00/graphrag-in-2026-a-practical-buyers-guide-to-knowledge-graph-augmented-rag-43e5e72d522d)
- [Nano-GraphRAG breakdown](https://gonamlui.com/blog/brief-breakdown-of-nano-graphrag-a-lightweight-alternative-to-graphrag)
- [Awesome-GraphRAG (curated list)](https://github.com/DEEP-PolyU/Awesome-GraphRAG)
- [Neo4j LLM Knowledge Graph Builder](https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/)
- LightRAG itself — repo `HKUDS/LightRAG` (engine choice for this POC).

### RAG techniques worth folding in

- [GraphRAG vs Vector RAG (Meilisearch)](https://www.meilisearch.com/blog/graph-rag-vs-vector-rag)
- [GraphRAG vs Vector RAG architecture decision (TianPan)](https://tianpan.co/blog/2026-04-19-graphrag-vs-vector-rag-architecture-decision)
- [12 Advanced RAG Techniques 2026 (Atlan)](https://atlan.com/know/advanced-rag-techniques/)
- [Hybrid Search for RAG: Vector+Keyword+Reranking 2026](https://www.buildmvpfast.com/blog/hybrid-search-rag-vector-keyword-reranking-2026)
- [RAG Is Not Dead: Advanced Patterns 2026 (dev.to)](https://dev.to/young_gao/rag-is-not-dead-advanced-retrieval-patterns-that-actually-work-in-2026-2gbo)
- [Best Chunking Strategies for RAG 2026 (Firecrawl)](https://www.firecrawl.dev/blog/best-chunking-strategies-rag)
- [Evaluating RAG Chunking Strategies 2026 (FutureAGI)](https://futureagi.com/blog/evaluating-rag-chunking-strategies-2026/)
- [Graph-Aware Late Chunking (arXiv 2026)](https://arxiv.org/html/2603.22633v1)

### Research / provenance

- [Anchor-constrained KG extraction with provenance (MDPI 2026)](https://www.mdpi.com/2073-431X/15/3/178)
  — matters for citing back to specific session+turn when answering.

## Open questions

- Do we ingest tool-call payloads (file contents, bash output)? Inflates the
  corpus but is where most concrete entities live. Default: yes for paths +
  short outputs (<2KB), no for full file edits.
- Should we redact PII / secrets at ingest? Sessions can contain pasted tokens.
  Default: reuse `perm_gate_lab/redact.py` patterns.
- Multi-host: today only ingests sessions on the host running it. Bridging
  mini ↔ note via rsync of the `~/.claude/projects/` slice is a Phase 7 concern.
- Embedding model lock-in: BGE-small dim 384, Voyage dim 1024. Schema stores
  dim explicitly; mixed-dim queries route to the right column.

## Related tickets

Filed on board #2 (Remote Control):

- ai-harness#74 — parent epic
- ai-harness#75 — phase 1 (ingest + Tier 0)
- ai-harness#76 — phase 2 (embeddings + clusters)
- ai-harness#77 — phase 3 (LightRAG + Haiku)
- ai-harness#78 — phase 4 (retrieval CLI)
- ai-harness#79 — phase 5 (eval harness)
- ai-harness#80 — phase 6 stretch (manager-ui tab)

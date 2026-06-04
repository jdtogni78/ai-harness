"""Small dataclasses for Phase-1 ingestion. The DB is authoritative; these
exist so the importer doesn't pass around bare dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class SessionRow:
    cse_id: str | None
    jsonl_path: str
    sha256: str
    host: str | None
    cwd: str | None
    repo: str | None
    branch: str | None
    title: str | None
    model: str | None
    started_at: str | None
    ended_at: str | None
    ended_reason: str | None
    turn_count: int
    redacted: bool


@dataclass(frozen=True)
class EntityRef:
    type: str
    canonical: str
    name: str


@dataclass(frozen=True)
class RelationRef:
    src: EntityRef
    dst: EntityRef
    rel_type: str
    turn_idx: int | None = None
    weight: float = 1.0


@dataclass
class ExtractionResult:
    entities: list[EntityRef] = field(default_factory=list)
    relations: list[RelationRef] = field(default_factory=list)


@dataclass
class IngestStats:
    files_seen: int = 0
    files_skipped: int = 0
    sessions_inserted: int = 0
    sessions_existing: int = 0
    entities_inserted: int = 0
    relations_inserted: int = 0
    redacted_count: int = 0

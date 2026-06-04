"""SQLite connection + Phase-1 schema for the session knowledge graph.

Embedding / summary / cluster / community tables are declared up-front so
later phases don't have to migrate — they fill them.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get(
    "SESSION_KG_DB",
    str(Path.home() / ".session-kg" / "kg.db"),
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cse_id        TEXT,
    jsonl_path    TEXT    NOT NULL UNIQUE,
    sha256        TEXT    NOT NULL,
    host          TEXT,
    cwd           TEXT,
    repo          TEXT,
    branch        TEXT,
    title         TEXT,
    model         TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    ended_reason  TEXT,
    turn_count    INTEGER NOT NULL DEFAULT 0,
    redacted      INTEGER NOT NULL DEFAULT 0,
    ingested_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_repo   ON session(repo);
CREATE INDEX IF NOT EXISTS idx_session_cse    ON session(cse_id);
CREATE INDEX IF NOT EXISTS idx_session_branch ON session(branch);

CREATE TABLE IF NOT EXISTS entity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,
    canonical   TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    UNIQUE (type, canonical)
);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entity(type);

CREATE TABLE IF NOT EXISTS relation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    src_type     TEXT    NOT NULL,
    src_id       INTEGER NOT NULL,
    dst_type     TEXT    NOT NULL,
    dst_id       INTEGER NOT NULL,
    rel_type     TEXT    NOT NULL,
    session_id   INTEGER REFERENCES session(id),
    turn_idx     INTEGER,
    weight       REAL    NOT NULL DEFAULT 1.0,
    UNIQUE (src_type, src_id, dst_type, dst_id, rel_type, session_id, turn_idx)
);
CREATE INDEX IF NOT EXISTS idx_rel_src     ON relation(src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_rel_dst     ON relation(dst_type, dst_id);
CREATE INDEX IF NOT EXISTS idx_rel_session ON relation(session_id);

CREATE TABLE IF NOT EXISTS embedding (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT    NOT NULL,
    owner_id   INTEGER NOT NULL,
    model      TEXT    NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB    NOT NULL,
    UNIQUE (owner_type, owner_id, model)
);

CREATE TABLE IF NOT EXISTS summary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type  TEXT    NOT NULL,
    owner_id    INTEGER NOT NULL,
    model       TEXT    NOT NULL,
    prompt_sha  TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    cost_usd    REAL,
    ts          TEXT    NOT NULL,
    UNIQUE (owner_type, owner_id, prompt_sha)
);

CREATE TABLE IF NOT EXISTS cluster (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    method      TEXT    NOT NULL,
    label       TEXT,
    summary_id  INTEGER REFERENCES summary(id),
    ts          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_member (
    cluster_id  INTEGER NOT NULL REFERENCES cluster(id),
    owner_type  TEXT    NOT NULL,
    owner_id    INTEGER NOT NULL,
    score       REAL,
    UNIQUE (cluster_id, owner_type, owner_id)
);

CREATE TABLE IF NOT EXISTS community (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       INTEGER NOT NULL,
    label       TEXT,
    summary_id  INTEGER REFERENCES summary(id),
    parent_id   INTEGER REFERENCES community(id)
);

CREATE TABLE IF NOT EXISTS community_member (
    community_id INTEGER NOT NULL REFERENCES community(id),
    entity_id    INTEGER NOT NULL REFERENCES entity(id),
    UNIQUE (community_id, entity_id)
);

CREATE TABLE IF NOT EXISTS cost_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    prompt_sha  TEXT    NOT NULL,
    in_tokens   INTEGER,
    out_tokens  INTEGER,
    cost_usd    REAL,
    purpose     TEXT
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    return conn

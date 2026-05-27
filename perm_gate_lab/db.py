"""SQLite connection + schema init for the perm_gate_lab corpus."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(os.environ.get(
    "PERM_GATE_LAB_DB",
    str(Path.home() / ".perm-gate-lab" / "lab.db"),
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_row (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    cwd         TEXT,
    session_id  TEXT,
    args_json   TEXT,
    ctx_json    TEXT,
    redacted    INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_case_source ON case_row(source);
CREATE INDEX IF NOT EXISTS idx_case_tool   ON case_row(tool);
CREATE INDEX IF NOT EXISTS idx_case_ts     ON case_row(ts);

CREATE TABLE IF NOT EXISTS judge (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL,
    guideline_path  TEXT,
    system_prompt   TEXT,
    model           TEXT,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    judge_id    INTEGER NOT NULL REFERENCES judge(id),
    case_set    TEXT,
    ts          TEXT    NOT NULL,
    git_sha     TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_judge ON run(judge_id);

CREATE TABLE IF NOT EXISTS verdict (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER NOT NULL REFERENCES case_row(id),
    run_id      INTEGER NOT NULL REFERENCES run(id),
    verdict     TEXT    NOT NULL,
    risk_tier   TEXT,
    rationale   TEXT,
    tier_used   TEXT,
    model       TEXT,
    latency_ms  INTEGER,
    cost_usd    REAL,
    enforced    INTEGER,
    UNIQUE (case_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_verdict_case ON verdict(case_id);
CREATE INDEX IF NOT EXISTS idx_verdict_run  ON verdict(run_id);

CREATE TABLE IF NOT EXISTS label (
    case_id          INTEGER PRIMARY KEY REFERENCES case_row(id),
    ideal_verdict    TEXT    NOT NULL,
    ideal_risk_tier  TEXT,
    labeler          TEXT,
    ts               TEXT    NOT NULL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS scorer (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    version        TEXT    NOT NULL,
    model          TEXT,
    system_prompt  TEXT,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS score (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id        INTEGER NOT NULL REFERENCES verdict(id),
    scorer_id         INTEGER NOT NULL REFERENCES scorer(id),
    score             INTEGER,
    agrees_with_label INTEGER,
    critique          TEXT,
    ts                TEXT    NOT NULL,
    UNIQUE (verdict_id, scorer_id)
);
CREATE INDEX IF NOT EXISTS idx_score_verdict ON score(verdict_id);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the corpus DB, creating the parent dir + schema as needed."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA)
    return conn

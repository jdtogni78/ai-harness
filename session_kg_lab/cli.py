"""skgctl — Phase-1 CLI over the session knowledge graph.

    skgctl init                      — create the SQLite DB + schema
    skgctl ingest [--host H] [PATH]  — walk ~/.claude/projects (or PATH)
    skgctl stats                     — row counts + breakdowns
    skgctl q --type T --name N       — lookups: sessions touching an entity
    skgctl top --type T              — top-N entities by mention count

$SESSION_KG_DB overrides the DB path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db, importer


def cmd_init(_: argparse.Namespace) -> int:
    conn = db.connect()
    conn.close()
    print(f"db initialised at {db.DEFAULT_DB_PATH}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        root = Path(args.path).expanduser() if args.path else None
        stats = importer.ingest(conn, root=root, host_filter=args.host)
        src = root or importer.DEFAULT_PROJECTS_ROOT
        print(f"ingested from: {src}")
        print(f"  files seen          : {stats.files_seen}")
        print(f"  files skipped       : {stats.files_skipped}")
        print(f"  sessions inserted   : {stats.sessions_inserted}")
        print(f"  sessions already-in : {stats.sessions_existing}")
        print(f"  entities inserted   : {stats.entities_inserted}")
        print(f"  relations inserted  : {stats.relations_inserted}")
        print(f"  sessions redacted   : {stats.redacted_count}")
        return 0
    finally:
        conn.close()


def cmd_stats(_: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        n_sess = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
        n_ent = conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
        n_rel = conn.execute("SELECT COUNT(*) FROM relation").fetchone()[0]
        print(f"sessions : {n_sess}")
        print(f"entities : {n_ent}")
        print(f"relations: {n_rel}")
        print()
        print("entities by type:")
        for r in conn.execute(
            "SELECT type, COUNT(*) AS n FROM entity GROUP BY type ORDER BY n DESC"
        ):
            print(f"  {r['type']:<14} {r['n']}")
        print()
        print("relations by type:")
        for r in conn.execute(
            "SELECT rel_type, COUNT(*) AS n FROM relation "
            "GROUP BY rel_type ORDER BY n DESC LIMIT 20"
        ):
            print(f"  {r['rel_type']:<14} {r['n']}")
        print()
        print("sessions by repo:")
        for r in conn.execute(
            "SELECT COALESCE(repo,'<none>') AS repo, COUNT(*) AS n FROM session "
            "GROUP BY repo ORDER BY n DESC LIMIT 15"
        ):
            print(f"  {r['repo']:<22} {r['n']}")
        return 0
    finally:
        conn.close()


def cmd_q(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        ent = conn.execute(
            "SELECT id, type, canonical, name FROM entity WHERE type = ? AND "
            "(canonical = ? OR name = ?)",
            (args.type, args.name, args.name),
        ).fetchone()
        if not ent:
            print(f"no entity: type={args.type} name={args.name}")
            return 1
        print(f"entity #{ent['id']}: {ent['type']} {ent['canonical']}")
        rows = conn.execute(
            """
            SELECT DISTINCT s.id, s.cse_id, s.repo, s.branch, s.started_at, r.rel_type
              FROM relation r
              JOIN session s ON s.id = r.session_id
             WHERE (r.dst_type = ? AND r.dst_id = ?)
                OR (r.src_type = ? AND r.src_id = ?)
             ORDER BY s.started_at DESC
             LIMIT ?
            """,
            (ent["type"], ent["id"], ent["type"], ent["id"], args.limit),
        ).fetchall()
        for r in rows:
            print(
                f"  s#{r['id']:>5} {r['started_at'] or '-':<32} "
                f"{(r['cse_id'] or '-'):<22} {(r['repo'] or '-'):<14} "
                f"{(r['branch'] or '-'):<22} via:{r['rel_type']}"
            )
        print(f"({len(rows)} session(s))")
        return 0
    finally:
        conn.close()


def cmd_top(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT e.type, e.canonical, e.name, COUNT(r.id) AS mentions
              FROM entity e
              LEFT JOIN relation r
                ON (r.dst_type = e.type AND r.dst_id = e.id)
                OR (r.src_type = e.type AND r.src_id = e.id)
             WHERE e.type = ?
             GROUP BY e.id
             ORDER BY mentions DESC
             LIMIT ?
            """,
            (args.type, args.limit),
        ).fetchall()
        for r in rows:
            print(f"  {r['mentions']:>5}  {r['canonical']}")
        print(f"({len(rows)} entit{'y' if len(rows) == 1 else 'ies'})")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skgctl", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the DB + schema").set_defaults(func=cmd_init)

    p_ing = sub.add_parser("ingest", help="walk ~/.claude/projects/*/sessions/*.jsonl")
    p_ing.add_argument("--host", help="filter to a single host (mini/note)")
    p_ing.add_argument("path", nargs="?", help="override root projects dir")
    p_ing.set_defaults(func=cmd_ingest)

    sub.add_parser("stats", help="row counts + breakdowns").set_defaults(func=cmd_stats)

    p_q = sub.add_parser("q", help="sessions that touch an entity")
    p_q.add_argument("--type", required=True,
                     help="entity type: repo, file, ticket, cse, commit, tool, error_class")
    p_q.add_argument("--name", required=True, help="canonical or display name")
    p_q.add_argument("--limit", type=int, default=20)
    p_q.set_defaults(func=cmd_q)

    p_top = sub.add_parser("top", help="top entities of a type by mention count")
    p_top.add_argument("--type", required=True)
    p_top.add_argument("--limit", type=int, default=15)
    p_top.set_defaults(func=cmd_top)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

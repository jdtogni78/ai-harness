"""pgctl — small CLI over the perm_gate_lab corpus.

Phase 1 commands:
    pgctl init                 — create the SQLite DB + schema
    pgctl import [--from-hook-log] [PATH]
                               — ingest the production hook's decision log
    pgctl list [--source S] [--tool T] [--verdict V] [--limit N]
                               — print recent cases + their production verdict
    pgctl stats                — row counts + breakdowns by source/verdict

All commands respect $PERM_GATE_LAB_DB to override the DB path.
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


def cmd_import(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        path = Path(args.path).expanduser() if args.path else None
        stats = importer.import_hook_log(conn, path=path)
        src = path or importer.DEFAULT_HOOK_LOG
        print(f"imported from: {src}")
        print(f"  records read       : {stats.records_read}")
        print(f"  records skipped    : {stats.records_skipped}")
        print(f"  cases inserted     : {stats.cases_inserted}")
        print(f"  cases already-seen : {stats.cases_existing}")
        print(f"  verdicts inserted  : {stats.verdicts_inserted}")
        print(f"  verdicts already   : {stats.verdicts_existing}")
        print(f"  records redacted   : {stats.redacted_count}")
        return 0
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        clauses = []
        params: list = []
        if args.source:
            clauses.append("c.source = ?")
            params.append(args.source)
        if args.tool:
            clauses.append("c.tool = ?")
            params.append(args.tool)
        if args.verdict:
            clauses.append("v.verdict = ?")
            params.append(args.verdict)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(args.limit)
        rows = conn.execute(
            f"""
            SELECT c.id, c.ts, c.source, c.tool, c.subject,
                   v.verdict, v.risk_tier, v.tier_used
              FROM case_row c
              LEFT JOIN verdict v ON v.case_id = c.id
              {where}
             ORDER BY c.id DESC
             LIMIT ?
            """,
            params,
        ).fetchall()
        for r in rows:
            subj = (r["subject"] or "")[:80].replace("\n", " ")
            print(
                f"#{r['id']:>5} {r['ts']} {r['tool']:<6} "
                f"{(r['verdict'] or '-'):<5} "
                f"{(r['risk_tier'] or '-'):<7} "
                f"{(r['tier_used'] or '-'):<18} {subj}"
            )
        print(f"({len(rows)} row(s))")
        return 0
    finally:
        conn.close()


def cmd_stats(_: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        n_cases = conn.execute("SELECT COUNT(*) FROM case_row").fetchone()[0]
        n_verdicts = conn.execute("SELECT COUNT(*) FROM verdict").fetchone()[0]
        print(f"cases   : {n_cases}")
        print(f"verdicts: {n_verdicts}")
        print()
        print("by source:")
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM case_row GROUP BY source ORDER BY n DESC"
        ):
            print(f"  {r['source']:<18} {r['n']}")
        print()
        print("by verdict:")
        for r in conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM verdict GROUP BY verdict ORDER BY n DESC"
        ):
            print(f"  {r['verdict']:<10} {r['n']}")
        print()
        print("by risk_tier:")
        for r in conn.execute(
            "SELECT risk_tier, COUNT(*) AS n FROM verdict "
            "GROUP BY risk_tier ORDER BY n DESC"
        ):
            print(f"  {(r['risk_tier'] or '-'):<10} {r['n']}")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pgctl", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the DB + schema").set_defaults(func=cmd_init)

    p_imp = sub.add_parser("import", help="import the production hook decision log")
    p_imp.add_argument("--from-hook-log", action="store_true",
                       help="(default behaviour; kept for explicitness)")
    p_imp.add_argument("path", nargs="?",
                       help="JSONL path; defaults to ~/dev/ai-harness/logs/perm-gate-decisions.jsonl")
    p_imp.set_defaults(func=cmd_import)

    p_ls = sub.add_parser("list", help="list recent cases + verdicts")
    p_ls.add_argument("--source")
    p_ls.add_argument("--tool")
    p_ls.add_argument("--verdict")
    p_ls.add_argument("--limit", type=int, default=20)
    p_ls.set_defaults(func=cmd_list)

    sub.add_parser("stats", help="row counts + breakdowns").set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

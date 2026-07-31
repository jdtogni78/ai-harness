#!/usr/bin/env python3
"""Sessions-by-manager dashboard (READ-ONLY).

Groups the boss's live Claude Code sessions by their manager (``[MGR-N]`` parsed
from the title), nests workers under their manager, and renders a static HTML
page whose rows are ``claude://resume?session=<uuid>`` deep links that open the
session in the desktop app.

Why this exists (see issue #158): the app's native groups are stored in a
private local store (``persisted.dframe-group-scopes``) and the sessions API has
no group field, so an *external* dashboard + deep links is the safe way to give
the boss a manager-grouped view without writing any app state.

STRICTLY read-only. It makes ZERO writes to any app store and ZERO writes to the
sessions API (it only issues ``GET /v1/code/sessions`` via the harness) and it
only *reads* transcript files. It never changes a session title.

Two hard problems, and how we solve them:

1. **Parsing the manager linkage from titles.** Titles migrate lazily, so the
   parser tolerates every observed form (see :func:`parse_title`):
     * ``[MGR-20] ...``                     -> manager #20
     * ``[AH.m5][MGR-12] ...``              -> manager #12 (legacy nick-leading)
     * ``[DST.m5][MGR17-W9][#71] ...``      -> worker  #9 of manager #17
     * ``[FE.m5][MGR13][W5] ...``           -> worker  #5 of manager #13 (split)
     * ``[NOMRG][AH.m5] ...`` / ``[INBOX]`` -> explicitly unmanaged
     * ``[AH.m5] just a nick ...``          -> unmanaged (no manager token)

2. **cse_ -> transcript uuid.** The deep link needs the CLI transcript uuid
   (``^[0-9a-f-]{36}$``), NOT the ``cse_`` id. The ``cse_`` id is *not* embedded
   in the transcript, and the sessions API does not return the uuid. But a bridge
   relays turns in lockstep with its local ``claude`` process, so a session's API
   ``last_event_at`` matches its transcript's last internal timestamp to within a
   second or two. We therefore map by **nearest-timestamp greedy assignment**
   (:func:`greedy_match`), scoped per repo, each transcript used at most once.
   Every mapping carries a confidence (delta-based) and low-confidence / unmapped
   rows are flagged in the HTML rather than silently linked to the wrong session.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# ---- confidence thresholds (seconds between API last_event and transcript last-ts)
CONF_HIGH_S = 5.0
CONF_MED_S = 60.0
CONF_LOW_S = 300.0  # beyond this we refuse to claim a mapping


# --------------------------------------------------------------------------- #
# 1. Title parsing
# --------------------------------------------------------------------------- #
@dataclass
class TitleInfo:
    """What we could extract from a session title."""

    role: str  # "manager" | "worker" | "unmanaged"
    manager: Optional[int] = None  # manager ordinal N
    worker: Optional[int] = None  # worker slot M (workers only)
    ticket: Optional[int] = None  # #NNN if present
    nick: Optional[str] = None  # leading [NICK.host] nickname, if any


# bracket tokens, in order
_BRACKET = re.compile(r"\[([^\]]*)\]")
_NICK = re.compile(r"^[A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9]+$")  # e.g. AH.m5, DEV.mini
_MGR_ONLY = re.compile(r"^MGR-(\d+)$")  # manager: dash then digits  -> [MGR-20]
_WORKER_COMBINED = re.compile(r"^MGR(\d+)-W(\d+)$")  # [MGR17-W9]
_MGR_BARE = re.compile(r"^MGR(\d+)$")  # split-form manager half:  [MGR13]
_W_BARE = re.compile(r"^W(\d+)$")  # split-form worker half:   [W5]
_TICKET = re.compile(r"^#(\d+)$")
# body-text fallbacks (some titles carry the linkage outside brackets)
_BODY_WORKER = re.compile(r"\bMGR(\d+)-W(\d+)\b")
_BODY_MGR = re.compile(r"\bMGR-(\d+)\b")


def parse_title(title: str) -> TitleInfo:
    """Parse the manager/worker linkage out of a session *title*.

    Bracket chain is authoritative; a body-text scan is the last resort so that
    titles like ``[DP.m5] MGR7-W21 - fundeck ...`` (linkage outside brackets)
    still group correctly. Returns role="unmanaged" when no manager token or an
    explicit ``[NOMRG]`` / ``[INBOX]`` token is found.
    """
    tokens = _BRACKET.findall(title or "")
    nick = None
    ticket = None

    for t in tokens:
        t = t.strip()
        if _NICK.match(t) and nick is None:
            nick = t
            continue
        m = _TICKET.match(t)
        if m:
            ticket = int(m.group(1))
            continue
    if ticket is None:  # ticket may live in the body, e.g. "... #158 ..."
        mb = re.search(r"#(\d+)\b", title or "")
        if mb:
            ticket = int(mb.group(1))

    # --- worker: combined form [MGR17-W9] ---
    for t in tokens:
        m = _WORKER_COMBINED.match(t.strip())
        if m:
            return TitleInfo("worker", int(m.group(1)), int(m.group(2)), ticket, nick)

    # --- worker: split manager-first form [MGR13][W5] (adjacent tokens) ---
    for i in range(len(tokens) - 1):
        a, b = tokens[i].strip(), tokens[i + 1].strip()
        ma, mb = _MGR_BARE.match(a), _W_BARE.match(b)
        if ma and mb:
            return TitleInfo("worker", int(ma.group(1)), int(mb.group(1)), ticket, nick)

    # --- manager: [MGR-20] ---
    for t in tokens:
        m = _MGR_ONLY.match(t.strip())
        if m:
            return TitleInfo("manager", int(m.group(1)), None, ticket, nick)

    # --- body-text fallbacks (linkage outside brackets) ---
    m = _BODY_WORKER.search(title or "")
    if m:
        return TitleInfo("worker", int(m.group(1)), int(m.group(2)), ticket, nick)
    m = _BODY_MGR.search(title or "")
    if m:
        return TitleInfo("manager", int(m.group(1)), None, ticket, nick)

    # --- nothing managerial ---
    return TitleInfo("unmanaged", None, None, ticket, nick)


# --------------------------------------------------------------------------- #
# 2. Grouping
# --------------------------------------------------------------------------- #
@dataclass
class Group:
    manager_ordinal: Optional[int]  # None => the Unmanaged bucket
    manager_session: Optional[dict] = None  # the [MGR-N] session, if live
    workers: List[dict] = field(default_factory=list)
    others: List[dict] = field(default_factory=list)  # unmanaged rows


def build_groups(sessions: List[dict]) -> List[Group]:
    """Group parsed *sessions* by manager ordinal. Each session dict must carry a
    ``title_info`` (:class:`TitleInfo`). Managers ascending first (workers nested,
    ascending by slot), the Unmanaged bucket last."""
    groups: Dict[Optional[int], Group] = {}

    def g(ordinal: Optional[int]) -> Group:
        if ordinal not in groups:
            groups[ordinal] = Group(ordinal)
        return groups[ordinal]

    for s in sessions:
        ti: TitleInfo = s["title_info"]
        if ti.role == "manager":
            g(ti.manager).manager_session = s
        elif ti.role == "worker":
            g(ti.manager).workers.append(s)
        else:
            g(None).others.append(s)

    ordered: List[Group] = []
    for ordinal in sorted(k for k in groups if k is not None):
        grp = groups[ordinal]
        grp.workers.sort(key=lambda s: (s["title_info"].worker or 0))
        ordered.append(grp)
    if None in groups:  # Unmanaged bucket last
        ordered.append(groups[None])
    return ordered


# --------------------------------------------------------------------------- #
# 3. cse_ -> transcript uuid  (nearest-timestamp greedy match)
# --------------------------------------------------------------------------- #
def _iso_to_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def transcript_last_epoch(path: Path, tail_bytes: int = 32768) -> Optional[float]:
    """Epoch of the last record carrying a ``timestamp`` in a ``.jsonl`` transcript.

    Reads only the file tail (transcripts are append-only and can be large)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ep = _iso_to_epoch(rec.get("timestamp"))
        if ep is not None:
            return ep
    return None


@dataclass
class Transcript:
    uuid: str
    path: Path
    project_dir: str  # e.g. -Users-dtogni-dev-ai-harness
    last_epoch: Optional[float]


def index_transcripts(projects_root: Path) -> List[Transcript]:
    """Read-only scan of ``~/.claude/projects/*/*.jsonl`` -> Transcript records.

    We never open a live leveldb or take a writer lock; plain file reads only."""
    out: List[Transcript] = []
    if not projects_root.is_dir():
        return out
    for proj in projects_root.iterdir():
        if not proj.is_dir():
            continue
        for j in proj.glob("*.jsonl"):
            uuid = j.stem
            if not UUID_RE.match(uuid):
                continue
            out.append(Transcript(uuid, j, proj.name, transcript_last_epoch(j)))
    return out


def greedy_match(
    session_times: Dict[str, float],
    transcript_times: Dict[str, float],
    tol_s: float = CONF_LOW_S,
) -> Dict[str, Tuple[str, float]]:
    """Assign each session id its nearest-in-time transcript uuid, each uuid used
    once. Pure (no I/O) so it is directly unit-testable.

    Returns ``{session_id: (uuid, abs_delta_seconds)}``. Pairs beyond *tol_s* are
    never assigned. Ties resolve deterministically by (delta, session_id, uuid)."""
    pairs: List[Tuple[float, str, str]] = []
    for sid, st in session_times.items():
        if st is None:
            continue
        for uuid, tt in transcript_times.items():
            if tt is None:
                continue
            d = abs(st - tt)
            if d <= tol_s:
                pairs.append((d, sid, uuid))
    pairs.sort()  # nearest first; deterministic tie-break on (sid, uuid)
    used_s, used_u = set(), set()
    result: Dict[str, Tuple[str, float]] = {}
    for d, sid, uuid in pairs:
        if sid in used_s or uuid in used_u:
            continue
        used_s.add(sid)
        used_u.add(uuid)
        result[sid] = (uuid, d)
    return result


def confidence(delta_s: Optional[float]) -> str:
    if delta_s is None:
        return "none"
    if delta_s <= CONF_HIGH_S:
        return "high"
    if delta_s <= CONF_MED_S:
        return "medium"
    if delta_s <= CONF_LOW_S:
        return "low"
    return "none"


def _repo_slug(session: dict) -> Optional[str]:
    """Best-effort repo short-name for a session (e.g. 'ai-harness'), used to
    scope the transcript candidate pool so cross-repo timestamp collisions don't
    steal a mapping."""
    try:
        url = session["config"]["sources"][0]["url"]
        return url.rstrip("/").split("/")[-1].replace(".git", "")
    except (KeyError, IndexError, TypeError):
        return session.get("repo")


def map_sessions_to_uuids(
    sessions: List[dict], transcripts: List[Transcript]
) -> Dict[str, Tuple[Optional[str], Optional[float]]]:
    """Map every session id -> (uuid, delta). Scopes candidates by repo when the
    repo name appears in the transcript's project-dir slug; sessions whose repo
    matches no dir fall back to the global pool (covers temp worktree dirs)."""
    tx_time = {t.uuid: t.last_epoch for t in transcripts}
    tx_by_uuid = {t.uuid: t for t in transcripts}

    # Partition sessions by whether their repo scopes to any project dir.
    result: Dict[str, Tuple[Optional[str], Optional[float]]] = {}
    remaining_sessions = list(sessions)

    # Pass 1: repo-scoped greedy match, per repo, so same-repo neighbours compete
    # only against same-repo transcripts.
    by_repo: Dict[str, List[dict]] = {}
    for s in remaining_sessions:
        by_repo.setdefault(_repo_slug(s) or "", []).append(s)

    claimed_uuids: set = set()
    for repo, sess in by_repo.items():
        if not repo:
            continue
        pool = {
            t.uuid: t.last_epoch
            for t in transcripts
            if repo in t.project_dir and t.uuid not in claimed_uuids
        }
        if not pool:
            continue
        stimes = {s["id"]: _iso_to_epoch(s.get("last_event_at")) for s in sess}
        m = greedy_match(stimes, pool)
        for sid, (uuid, d) in m.items():
            result[sid] = (uuid, d)
            claimed_uuids.add(uuid)

    # Pass 2: anything still unmapped competes globally against leftover uuids
    # (catches worktree/temp project dirs whose name doesn't contain the repo).
    unmapped = [s for s in sessions if s["id"] not in result]
    if unmapped:
        global_pool = {
            u: t for u, t in tx_time.items() if u not in claimed_uuids
        }
        stimes = {s["id"]: _iso_to_epoch(s.get("last_event_at")) for s in unmapped}
        m = greedy_match(stimes, global_pool)
        for sid, (uuid, d) in m.items():
            result[sid] = (uuid, d)
            claimed_uuids.add(uuid)

    for s in sessions:
        result.setdefault(s["id"], (None, None))
    # discard mappings whose confidence is 'none'
    for sid, (uuid, d) in list(result.items()):
        if uuid is not None and confidence(d) == "none":
            result[sid] = (None, None)
        elif uuid is not None and uuid not in tx_by_uuid:
            result[sid] = (None, None)
    return result


# --------------------------------------------------------------------------- #
# 4. Live fetch (only place that talks to the harness / API — read-only GET)
# --------------------------------------------------------------------------- #
def fetch_live_sessions() -> List[dict]:
    """GET the live (active) sessions via the harness. Read-only. Imported lazily
    so the pure logic above stays importable without the harness present."""
    import logging

    from remote_control.config import UsageLimitConfig
    from remote_control.usage_limit import monitor

    log = logging.getLogger("mgr_dashboard")
    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    sessions = monitor.list_sessions(cfg, token, log)
    return [s for s in sessions if s.get("status") == "active"]


# --------------------------------------------------------------------------- #
# 5. HTML rendering
# --------------------------------------------------------------------------- #
def _rel_age(last_event_at: Optional[str], now_epoch: float) -> str:
    ep = _iso_to_epoch(last_event_at)
    if ep is None:
        return "?"
    secs = max(0, int(now_epoch - ep))
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _row_html(s: dict, uuid_map, now_epoch: float, indent: bool) -> str:
    uuid, delta = uuid_map.get(s["id"], (None, None))
    conf = confidence(delta)
    title = _html.escape(s.get("title", ""))
    repo = _html.escape(s.get("repo", "") or _repo_slug(s) or "")
    wstat = _html.escape(s.get("worker_status", "") or "")
    conn = s.get("connection_status", "")
    age = _rel_age(s.get("last_event_at"), now_epoch)
    cls = "worker" if indent else "row"
    dot = "ok" if conn == "connected" else "off"
    if uuid:
        badge = f'<span class="conf {conf}">{conf}</span>'
        link = f'<a class="open" href="claude://resume?session={uuid}" title="uuid {uuid} (Δ{delta:.0f}s)">open ↗</a>'
    else:
        badge = '<span class="conf none">unmapped</span>'
        link = '<span class="open dim" title="no confident transcript match">—</span>'
    return (
        f'<div class="{cls}">'
        f'<span class="dot {dot}"></span>'
        f'<span class="title">{title}</span>'
        f'<span class="meta">{repo} · {wstat} · {age}</span>'
        f"{badge}{link}"
        f"</div>"
    )


def render_html(groups: List[Group], uuid_map, now_epoch: float, note: str = "") -> str:
    n_sessions = sum(
        (1 if g.manager_session else 0) + len(g.workers) + len(g.others) for g in groups
    )
    n_mapped = sum(1 for v in uuid_map.values() if v[0])
    parts: List[str] = []
    for g in groups:
        if g.manager_ordinal is None:
            if not g.others:
                continue
            header = f"Unmanaged &middot; {len(g.others)}"
            body = "".join(_row_html(s, uuid_map, now_epoch, False) for s in g.others)
            parts.append(_details(header, body, open_=False))
            continue
        n = 1 if g.manager_session else 0
        header = f"MGR-{g.manager_ordinal} &middot; {len(g.workers)} worker(s)"
        rows = []
        if g.manager_session:
            rows.append(_row_html(g.manager_session, uuid_map, now_epoch, False))
        else:
            rows.append(
                f'<div class="row ghost"><span class="title">[MGR-{g.manager_ordinal}] manager not live</span></div>'
            )
        rows += [_row_html(w, uuid_map, now_epoch, True) for w in g.workers]
        parts.append(_details(header, "".join(rows), open_=True))

    stamp = _dt.datetime.fromtimestamp(now_epoch, _dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    note_html = f'<p class="note">{_html.escape(note)}</p>' if note else ""
    return _PAGE.format(
        body="".join(parts),
        stamp=stamp,
        n_sessions=n_sessions,
        n_mapped=n_mapped,
        note=note_html,
    )


def _details(header: str, body: str, open_: bool) -> str:
    o = " open" if open_ else ""
    return f"<details{o}><summary>{header}</summary>{body}</details>"


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sessions by manager</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 1.5rem auto; max-width: 900px; padding: 0 1rem; }}
 h1 {{ font-size: 1.2rem; margin: 0 0 .2rem; }}
 .sub {{ color: #888; margin: 0 0 1rem; font-size: .85rem; }}
 .note {{ background: #f4f0d0; color: #554; border-radius: 6px; padding: .5rem .7rem; font-size: .82rem; }}
 @media (prefers-color-scheme: dark) {{ .note {{ background: #2c2a1a; color: #cc9; }} }}
 details {{ border: 1px solid #8883; border-radius: 8px; margin: .5rem 0; padding: .2rem .6rem; }}
 summary {{ cursor: pointer; font-weight: 600; padding: .35rem 0; }}
 .row, .worker {{ display: flex; align-items: center; gap: .5rem; padding: .25rem 0; border-top: 1px solid #8881; }}
 .worker {{ padding-left: 1.4rem; }}
 .ghost .title, .row.ghost {{ color: #999; font-style: italic; }}
 .title {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
 .meta {{ color: #999; font-size: .78rem; white-space: nowrap; }}
 .dot {{ width: .55rem; height: .55rem; border-radius: 50%; flex: none; }}
 .dot.ok {{ background: #3c8; }} .dot.off {{ background: #c55; }}
 .open {{ text-decoration: none; font-size: .8rem; white-space: nowrap; }}
 .open.dim {{ color: #bbb; }}
 .conf {{ font-size: .68rem; padding: .05rem .3rem; border-radius: 4px; }}
 .conf.high {{ background: #3c83; }} .conf.medium {{ background: #fa03; }}
 .conf.low {{ background: #f843; }} .conf.none {{ background: #c553; }}
</style></head><body>
<h1>Sessions by manager</h1>
<p class="sub">{n_mapped}/{n_sessions} rows deep-linkable &middot; generated {stamp} &middot; read-only</p>
{note}
{body}
</body></html>
"""


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #
DEEPLINK_NOTE = (
    "Deep links use claude://resume?session=<uuid>. Confidence = |API last_event "
    "- transcript last-ts|: high ≤5s, medium ≤60s, low ≤300s. "
    "'unmapped' rows had no confident transcript match. Firing a link creates no "
    "duplicate server-side session (verified). See README."
)


def build_dashboard(projects_root: Path) -> str:
    sessions = fetch_live_sessions()
    for s in sessions:
        s["title_info"] = parse_title(s.get("title", ""))
        s.setdefault("repo", _repo_slug(s) or "")
    transcripts = index_transcripts(projects_root)
    uuid_map = map_sessions_to_uuids(sessions, transcripts)
    groups = build_groups(sessions)
    now = max(
        (_iso_to_epoch(s.get("last_event_at")) or 0) for s in sessions
    ) if sessions else 0.0
    return render_html(groups, uuid_map, now or 0.0, note=DEEPLINK_NOTE)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only sessions-by-manager dashboard.")
    ap.add_argument(
        "-o", "--out", default="mgr-dashboard.html", help="output HTML path"
    )
    ap.add_argument(
        "--projects-root",
        default=str(Path.home() / ".claude" / "projects"),
        help="transcript root (default ~/.claude/projects)",
    )
    ap.add_argument("--open", action="store_true", help="open the HTML when done")
    args = ap.parse_args(argv)

    html_str = build_dashboard(Path(args.projects_root))
    out = Path(args.out)
    out.write_text(html_str, encoding="utf-8")
    print(f"wrote {out}")
    if args.open:
        import subprocess

        subprocess.run(["open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

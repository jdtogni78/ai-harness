#!/usr/bin/env python3
"""validate.py — manager-invoked, milestone-driven project validator.

Reads a project's MAJOR END-TO-END GOALS from a per-project, manager-owned
milestones file and, for EACH goal, attempts to PROVE it works with REAL,
resolvable evidence. It is the OUTCOME counterpart to /report (activity): where
/report says "what did my workers do", /validate says "do this project's major
goals actually work, and can you prove it".

    validate.py <project> [--milestones-dir DIR] [--outdir DIR] [--slug NAME]
                          [--json]

Stance (inherited from the #110 validator + the /report honesty bar):
  * Assume nothing works until proven. A milestones file's declared status is a
    LABEL, never an upgrade — evidence always wins, and a mismatch is surfaced.
  * VERIFIED-WORKING requires a resolvable evidence pointer the validator checked
    itself (a file that exists, a pattern found, a command that ran green). This
    reuses the /report skill's verified-vs-claimed classifier (report.resolve_pointer)
    as the canonical "does this pointer resolve" test.
  * Missing proof is a FINDING (UNVERIFIED / NOT-YET / PARTIAL / FAILED), never a
    silent pass and never a fabricated green.

Evidence check kinds (each -> PASS / FAIL / MISSING):
    file:    <path>                          PASS if the path exists on disk
    grep:    {pattern, path}                 PASS if the pattern is found in the file
    command: {run, expect_rc=0, expect_match}PASS if rc matches and expect_match found

Output:
    * a milestone-status TABLE (worst-first) printed as markdown to stdout
    * an HTML deck REUSING cos-console/presentation-poc/deck.generate
    * a per-slide narration.json + narrate-demo .demo.yaml companion (from the deck)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AH_ROOT = HERE.parents[2]                       # ai-harness/
DECK_DIR = AH_ROOT / "cos-console" / "presentation-poc"
MILESTONES_DIR = AH_ROOT / "milestones"

# reuse, not copy: the deck renderer and the /report verified-vs-claimed classifier
sys.path.insert(0, str(DECK_DIR))
sys.path.insert(0, str(AH_ROOT))
sys.path.insert(0, str(AH_ROOT / "skills" / "report" / "scripts"))
from deck.generate import write_generic_deck            # noqa: E402  (reuse)
import report as report_skill                            # noqa: E402  (classifier reuse)

ISO = "%Y-%m-%dT%H:%M:%SZ"

# --------------------------------------------------------------------------- #
# verdicts — worst first (lower rank = worse, sorted to the top of tables)
# --------------------------------------------------------------------------- #
VERDICTS = {
    "failed":     {"rank": 0, "label": "FAILED",           "pill": "fail", "mark": "✗"},
    "unverified": {"rank": 1, "label": "UNVERIFIED",       "pill": "fail", "mark": "?"},
    "not-yet":    {"rank": 2, "label": "NOT-YET",          "pill": "warn", "mark": "◻"},
    "partial":    {"rank": 3, "label": "PARTIAL",          "pill": "warn", "mark": "◐"},
    "verified":   {"rank": 4, "label": "VERIFIED-WORKING", "pill": "ok",   "mark": "✓"},
}
WORKING_VERDICT = "verified"     # the ONLY verdict that requires resolved evidence


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


# --------------------------------------------------------------------------- #
# path resolution — anchored on the /report classifier's honesty bar
# --------------------------------------------------------------------------- #
def resolve_path(raw: str) -> Path | None:
    """A path that actually EXISTS, tried relative to the repo root, ~ and abs.
    Uses the same resolve-or-None discipline as report.resolve_pointer."""
    if not raw:
        return None
    raw = raw.strip()
    for base in (AH_ROOT, None):
        p = (base / raw) if base else Path(raw).expanduser()
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


# --------------------------------------------------------------------------- #
# evidence checks
# --------------------------------------------------------------------------- #
def check_file(chk: dict) -> dict:
    raw = chk.get("file", "")
    p = resolve_path(raw)
    if p:
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        return {"kind": "file", "status": "pass", "pointer": str(p),
                "detail": f"exists ({size} bytes)" if size is not None else "exists",
                "desc": f"file {raw}"}
    return {"kind": "file", "status": "missing", "pointer": None,
            "detail": "not found on this host", "desc": f"file {raw}"}


def check_grep(chk: dict) -> dict:
    spec = chk.get("grep", {})
    pattern = spec.get("pattern", "")
    raw = spec.get("path", "")
    desc = f"grep /{pattern}/ in {raw}"
    p = resolve_path(raw)
    if not p:
        return {"kind": "grep", "status": "missing", "pointer": None,
                "detail": "file not found on this host", "desc": desc}
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        return {"kind": "grep", "status": "missing", "pointer": str(p),
                "detail": f"unreadable: {e}", "desc": desc}
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    if rx.search(text):
        return {"kind": "grep", "status": "pass", "pointer": f"{p}:/{pattern}/",
                "detail": "pattern present", "desc": desc}
    return {"kind": "grep", "status": "fail", "pointer": str(p),
            "detail": "pattern NOT found in file", "desc": desc}


def check_command(chk: dict) -> dict:
    spec = chk.get("command", {})
    run = spec.get("run", "")
    expect_rc = spec.get("expect_rc", 0)
    expect_match = spec.get("expect_match")
    desc = f"command `{run}`"
    if not run:
        return {"kind": "command", "status": "missing", "pointer": None,
                "detail": "no command given", "desc": desc}
    try:
        proc = subprocess.run(run, shell=True, capture_output=True, text=True,
                              timeout=spec.get("timeout", 20), cwd=str(AH_ROOT))
    except subprocess.TimeoutExpired:
        return {"kind": "command", "status": "missing", "pointer": None,
                "detail": "timed out (system likely unreachable)", "desc": desc}
    except Exception as e:                                   # noqa: BLE001
        return {"kind": "command", "status": "missing", "pointer": None,
                "detail": f"could not run: {e}", "desc": desc}
    out = (proc.stdout or "") + (proc.stderr or "")
    snippet = " ".join(out.split())[:160]
    if proc.returncode != expect_rc:
        return {"kind": "command", "status": "fail", "pointer": None,
                "detail": f"rc={proc.returncode} (expected {expect_rc}): {snippet}",
                "desc": desc}
    if expect_match and not re.search(expect_match, out):
        return {"kind": "command", "status": "fail", "pointer": None,
                "detail": f"rc ok but /{expect_match}/ not in output: {snippet}",
                "desc": desc}
    return {"kind": "command", "status": "pass", "pointer": f"$ {run}",
            "detail": f"rc={proc.returncode}" + (f", matched /{expect_match}/"
                                                 if expect_match else ""),
            "desc": desc}


def run_check(chk: dict) -> dict:
    if "file" in chk:
        return check_file(chk)
    if "grep" in chk:
        return check_grep(chk)
    if "command" in chk:
        return check_command(chk)
    return {"kind": "unknown", "status": "missing", "pointer": None,
            "detail": f"unrecognized evidence check: {list(chk)}", "desc": str(chk)}


# --------------------------------------------------------------------------- #
# per-goal verdict
# --------------------------------------------------------------------------- #
def normalize_expect(raw) -> str | None:
    if not raw:
        return None
    key = str(raw).strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {"verified-working": "verified", "working": "verified",
               "done": "verified", "notyet": "not-yet", "todo": "not-yet"}
    key = aliases.get(key, key)
    return key if key in VERDICTS else None


def verdict_for_goal(goal: dict) -> dict:
    checks = [run_check(c) for c in (goal.get("evidence") or [])]
    pending = list(goal.get("pending") or [])
    expect = normalize_expect(goal.get("expect"))

    n_pass = sum(1 for c in checks if c["status"] == "pass")
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_missing = sum(1 for c in checks if c["status"] == "missing")
    has_pending = bool(pending)

    if not checks and not has_pending:
        # nothing automatable — honor an operator-declared NON-working status,
        # else it is simply unproven.
        if expect and expect != WORKING_VERDICT:
            verdict, why = expect, "declared in milestones file (no automated proof)"
        else:
            verdict, why = "unverified", "no evidence checks defined"
    elif n_pass and n_pass == len(checks) and not has_pending:
        verdict, why = "verified", "every evidence check resolved"
    elif n_pass and (has_pending or n_pass < len(checks)):
        bits = []
        if n_pass < len(checks):
            bits.append(f"{n_pass}/{len(checks)} checks resolved")
        if has_pending:
            bits.append(f"{len(pending)} aspect(s) still pending proof")
        verdict, why = "partial", "; ".join(bits)
    elif n_pass == 0 and n_fail:
        verdict, why = "failed", f"{n_fail} check(s) ran but did not confirm the goal"
    else:  # n_pass == 0, all missing (or only pending, no checks)
        if expect == "not-yet":
            verdict, why = "not-yet", "declared not-yet; no evidence resolvable here"
        else:
            verdict, why = "unverified", ("evidence not resolvable from this host "
                                          "(system likely not reachable)")

    # discrepancy: the file CLAIMS a stronger status than the evidence supports.
    discrepancy = None
    if expect and VERDICTS[verdict]["rank"] < VERDICTS[expect]["rank"]:
        discrepancy = (f'milestones file declares "{VERDICTS[expect]["label"]}" but '
                       f'evidence here only supports "{VERDICTS[verdict]["label"]}"')

    pointers = [c["pointer"] for c in checks if c["status"] == "pass" and c["pointer"]]
    return {"verdict": verdict, "why": why, "checks": checks, "pending": pending,
            "expect": expect, "discrepancy": discrepancy, "pointers": pointers,
            "n_pass": n_pass, "n_fail": n_fail, "n_missing": n_missing}


# --------------------------------------------------------------------------- #
# load milestones
# --------------------------------------------------------------------------- #
def load_milestones(project: str, mdir: Path) -> dict:
    path = mdir / f"{project}.yaml"
    if not path.is_file():
        path_yml = mdir / f"{project}.yml"
        path = path_yml if path_yml.is_file() else path
    if not path.is_file():
        available = sorted(p.stem for p in mdir.glob("*.y*ml")) if mdir.is_dir() else []
        sys.exit(f"validate.py: no milestones file for '{project}' at {path}. "
                 f"Available: {', '.join(available) or '(none)'}")
    try:
        import yaml
    except ImportError:
        sys.exit("validate.py: PyYAML is required (pip install pyyaml).")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict) or "goals" not in data:
        sys.exit(f"validate.py: {path} has no 'goals:' list.")
    data["_path"] = str(path)
    return data


def build_result(project: str, mdir: Path) -> dict:
    ms = load_milestones(project, mdir)
    goals = []
    for g in ms.get("goals") or []:
        v = verdict_for_goal(g)
        goals.append({"id": g.get("id", "?"), "goal": g.get("goal", ""),
                      "prove": g.get("prove", ""), "note": g.get("note"),
                      **v})
    # worst-first ordering
    goals.sort(key=lambda g: (VERDICTS[g["verdict"]]["rank"], g["id"]))
    return {"project": ms.get("project", project),
            "title": ms.get("title", f"{project} — major goals"),
            "draft": bool(ms.get("draft")), "system": ms.get("system"),
            "source_file": ms["_path"], "goals": goals}


# --------------------------------------------------------------------------- #
# HTML deck — reuse deck.generate's generic renderer + report's widget helpers
# --------------------------------------------------------------------------- #
esc = report_skill.esc
tile = report_skill.tile
nodata = report_skill.nodata


def counts(goals: list[dict]) -> dict:
    c = {k: 0 for k in VERDICTS}
    for g in goals:
        c[g["verdict"]] += 1
    return c


def draft_banner(res: dict, generated_at: str) -> str:
    draft = res["draft"]
    cls = "warn" if draft else "ok"
    mark = "▲ DRAFT" if draft else "● milestones"
    note = ("  — this milestones file is UNCONFIRMED; goals and proof strategy "
            "are seeds the operator will correct."
            if draft else "")
    sysline = f" · system: {esc(res['system'])}" if res.get("system") else ""
    return (f'<div class="card" style="margin-bottom:14px;padding:10px 14px;font-size:13px">'
            f'<span class="pill {cls}">{esc(mark)}</span> '
            f'generated {esc(generated_at)} · source '
            f'<code>{esc(Path(res["source_file"]).name)}</code>{sysline}'
            f'{esc(note)}</div>')


def verdict_pill(v: str) -> str:
    d = VERDICTS[v]
    return f'<span class="pill {d["pill"]}">{d["mark"]} {d["label"]}</span>'


def build_deck(res: dict, generated_at: str) -> dict:
    goals = res["goals"]
    n = len(goals)
    c = counts(goals)
    proj = res["project"]
    verified = [g for g in goals if g["verdict"] == "verified"]
    slides = []

    # 1. summary -----------------------------------------------------------
    summary_html = (
        draft_banner(res, generated_at)
        + '<div class="grid kpis">'
        + tile(n, "major goals")
        + tile(c["verified"], "VERIFIED-WORKING", na=(c["verified"] == 0))
        + tile(c["partial"] + c["not-yet"], "partial / not-yet",
               na=(c["partial"] + c["not-yet"] == 0))
        + tile(c["unverified"] + c["failed"], "unverified / failed",
               na=(c["unverified"] + c["failed"] == 0))
        + '</div>')
    slides.append({
        "widget": "summary", "kicker": "Milestone validation",
        "title": f"{proj} — do the major goals work?",
        "subtitle": f"{n} goals · generated {generated_at} · "
                    f"{c['verified']} proven working",
        "html": summary_html,
        "narration": (
            f"Milestone validation for {proj}. {n} major goal"
            f"{'s' if n != 1 else ''}. {c['verified']} are VERIFIED-WORKING with "
            f"evidence I resolved myself; {c['partial']} partial, {c['not-yet']} "
            f"not-yet, {c['unverified']} unverified, {c['failed']} failed. "
            + ("This milestones file is a DRAFT — treat the goals as unconfirmed. "
               if res["draft"] else "")
            + "Nothing is shown working without proof."),
    })

    # 2. milestone table (worst-first) -------------------------------------
    rows = []
    for g in goals:
        right = verdict_pill(g["verdict"])
        if g["verdict"] == "verified" and g["pointers"]:
            ev = "; ".join(g["pointers"])[:110]
            evline = (f'<br><span style="color:var(--muted);font-size:12px">'
                      f'evidence: {esc(ev)}</span>')
        else:
            evline = (f'<br><span style="color:var(--todo);font-size:12px">'
                      f'{esc(g["why"])}</span>')
        rows.append(
            f'<li><span>{esc(g["goal"])}'
            f'<br><span style="color:var(--muted);font-size:12px">'
            f'<code>{esc(g["id"])}</code></span>{evline}</span>'
            f'<span style="margin-left:auto">{right}</span></li>')
    table_html = (
        f'<div class="card"><ul class="clean">{"".join(rows)}</ul>'
        f'<p class="sub" style="margin:.8em 0 0;font-size:13px">Worst-first. '
        f'<b>VERIFIED-WORKING</b> is the only verdict that required a resolvable '
        f'evidence pointer — every green row cites it. Everything else is a '
        f'finding, not a pass.</p></div>')
    if c["verified"] < n:
        table_html += nodata(
            f'{n - c["verified"]} of {n} goals are NOT proven working here — they '
            f'are shown at their honest verdict, never upgraded to green.')
    slides.append({"widget": "milestones", "kicker": "Outcomes",
                   "heading": f"Milestones — {c['verified']}/{n} proven working",
                   "html": table_html,
                   "narration": (
                       f"Here are the {n} goals, worst first. "
                       f"{c['verified']} carry a resolvable evidence pointer and are "
                       f"green. The rest are findings — partial, not-yet, unverified "
                       f"or failed — shown honestly, not faked working.")})

    # 3. evidence detail ---------------------------------------------------
    ev_rows = []
    for g in goals:
        checks = g["checks"]
        head = (f'<li style="display:block"><b>{esc(g["id"])}</b> '
                f'{verdict_pill(g["verdict"])}')
        sub = []
        for ch in checks:
            st = ch["status"]
            pill = {"pass": "ok", "fail": "fail", "missing": "fail"}.get(st, "")
            mark = {"pass": "✓", "fail": "✗", "missing": "?"}.get(st, "?")
            sub.append(
                f'<div style="margin:4px 0 0 8px;font-size:13px">'
                f'<span class="pill {pill}">{mark} {esc(st.upper())}</span> '
                f'{esc(ch["desc"])} — <span style="color:var(--muted)">'
                f'{esc(ch["detail"])}</span></div>')
        for pend in g["pending"]:
            sub.append(
                f'<div style="margin:4px 0 0 8px;font-size:13px">'
                f'<span class="pill warn">◐ PENDING</span> '
                f'<span style="color:var(--todo)">{esc(pend)}</span></div>')
        if g.get("note"):
            sub.append(f'<div style="margin:4px 0 0 8px;font-size:12px;'
                       f'color:var(--muted)">note: {esc(g["note"])}</div>')
        if not checks and not g["pending"]:
            sub.append('<div style="margin:4px 0 0 8px;font-size:13px;'
                       'color:var(--muted)">no evidence checks defined</div>')
        if g.get("discrepancy"):
            sub.append(f'<div style="margin:4px 0 0 8px;font-size:13px">'
                       f'<span class="pill fail">⚠︎ DISCREPANCY</span> '
                       f'<span style="color:var(--todo)">{esc(g["discrepancy"])}</span></div>')
        ev_rows.append(head + "".join(sub) + "</li>")
    slides.append({"widget": "evidence", "kicker": "Proof",
                   "heading": "Evidence — what was actually checked",
                   "html": f'<div class="card"><ul class="clean">{"".join(ev_rows)}</ul></div>',
                   "narration": (
                       "This is the proof trail: every evidence check per goal, "
                       "each marked PASS, FAIL or MISSING, with the pointer or the "
                       "reason it could not be resolved. A milestones file that "
                       "over-claims relative to the evidence is flagged as a "
                       "discrepancy.")})

    # 4. gaps / open questions --------------------------------------------
    gaps = []
    for g in goals:
        if g["verdict"] == "verified":
            continue
        gaps.append(f'{g["id"]} — {VERDICTS[g["verdict"]]["label"]}: {g["why"]}')
    for g in goals:
        if g.get("discrepancy"):
            gaps.append(f'{g["id"]} — DISCREPANCY: {g["discrepancy"]}')
    if res["draft"]:
        gaps.append("this milestones file is a DRAFT — confirm the goal list and "
                    "proof strategy with the operator before trusting it")
    if gaps:
        body = "".join(f'<div style="font-size:17px">⚠︎ {esc(x)}</div>' for x in gaps[:9])
        if len(gaps) > 9:
            body += f'<div style="color:var(--muted)">…and {len(gaps)-9} more</div>'
    else:
        body = ('<div style="font-size:20px;color:var(--done)">✓ Every major goal '
                'is proven working with resolved evidence.</div>'
                '<div style="color:var(--muted);font-size:14px;margin-top:8px">'
                'This covers only the goals in the milestones file and the evidence '
                'the validator could resolve on this host. It is not a claim about '
                'anything outside that.</div>')
    slides.append({"widget": "gaps", "kicker": "For you",
                   "heading": f"{len(gaps)} gap{'s' if len(gaps) != 1 else ''} to close",
                   "html": f'<div class="card" style="border-color:rgba(210,153,34,.4)">{body}</div>',
                   "narration": (
                       f"{len(gaps)} thing{'s' if len(gaps) != 1 else ''} to close before "
                       f"{proj} is fully proven. " + (gaps[0] + "." if gaps else
                       "Nothing outstanding among the goals I could check."))})

    return {"title": f"{proj} — milestone validation",
            "subtitle": res["title"], "generated_at": generated_at,
            "source": "validate skill", "slides": slides}


# --------------------------------------------------------------------------- #
# markdown recap
# --------------------------------------------------------------------------- #
def markdown_recap(res: dict, paths: dict, generated_at: str) -> str:
    goals = res["goals"]
    n = len(goals)
    c = counts(goals)
    L = [f"## Milestone validation — `{res['project']}`",
         f"*{n} major goal(s) · generated {generated_at} · source "
         f"`{Path(res['source_file']).name}`*"]
    if res["draft"]:
        L.append("\n> ▲ **DRAFT milestones** — goals/proof are unconfirmed seeds; "
                 "the operator will correct them.")
    if res.get("system"):
        L.append(f"\n> System under test: **{res['system']}**.")
    L += ["",
          f"**{c['verified']}** VERIFIED-WORKING · **{c['partial']}** partial · "
          f"**{c['not-yet']}** not-yet · **{c['unverified']}** unverified · "
          f"**{c['failed']}** failed",
          "",
          "| goal | id | verdict | evidence / why |",
          "|---|---|---|---|"]
    for g in goals:
        vd = VERDICTS[g["verdict"]]["label"]
        if g["verdict"] == "verified" and g["pointers"]:
            eff = "; ".join(g["pointers"])[:70]
        else:
            eff = g["why"][:70]
        goal_short = g["goal"][:60]
        L.append(f"| {goal_short} | `{g['id']}` | **{vd}** | {eff} |")
    disc = [g for g in goals if g.get("discrepancy")]
    if disc:
        L.append("")
        L.append("> ⚠︎ **Discrepancies** (file claims more than the evidence shows): "
                 + "; ".join(f"`{g['id']}` — {g['discrepancy']}" for g in disc))
    unproven = [g for g in goals if g["verdict"] != "verified"]
    if unproven:
        L.append("")
        L.append("> ⚠︎ **Not proven working here:** " +
                 ", ".join(f"`{g['id']}`" for g in unproven) +
                 ". Shown at honest verdict — never upgraded to working without proof.")
    L += ["",
          f"**Deck:** `{paths['html']}` · **narration:** `{paths['narration']}` "
          f"· **demo yaml:** `{paths['demo']}`"]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(prog="validate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="project key (milestones/<project>.yaml)")
    ap.add_argument("--milestones-dir", default=None,
                    help=f"milestones dir (default: {MILESTONES_DIR})")
    ap.add_argument("--outdir", default=None, help="deck output dir")
    ap.add_argument("--slug", default=None, help="output basename")
    ap.add_argument("--json", action="store_true", help="also print raw result JSON")
    args = ap.parse_args(argv)

    mdir = Path(args.milestones_dir) if args.milestones_dir else MILESTONES_DIR
    res = build_result(args.project, mdir)
    now = now_utc()

    slug = args.slug or f"{res['project']}-milestones"
    outdir = args.outdir or str(DECK_DIR / "out")
    deck = build_deck(res, now)
    paths = write_generic_deck(deck, outdir=outdir, slug=slug)

    print(markdown_recap(res, paths, now))
    if args.json:
        print("\n<!-- raw result -->")
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

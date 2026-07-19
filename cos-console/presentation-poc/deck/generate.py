"""StatusReport -> self-contained HTML status deck (+ narration companion).

Usage:
    python -m deck.generate <project>            # probe the live data plane
    python -m deck.generate <project> --from probe.json
    python -m deck.generate <project> --outdir out

Produces, under --outdir (default ./out):
    <project>-status.html            self-contained deck (no build, no deps)
    <project>-status.narration.json  per-slide {widget, narration} companion

Design contract (see status-mcp/SCHEMA.md):
  * NEVER fabricate. `availability[section] != live/partial` (or a null payload)
    renders a NO-DATA panel / "n/a" tile, never a 0. The narration says
    "I don't have <x> data" rather than voicing a fake number.
  * Single self-contained file: the template carries all CSS + JS inline and
    the StatusReport is injected as one JSON blob.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
# The sibling data plane. Read-only: we only ever *run its probe*.
STATUS_MCP = (HERE.parent.parent / "status-mcp").resolve()


# --------------------------------------------------------------------------- #
# data acquisition
# --------------------------------------------------------------------------- #
def probe(project: str) -> dict:
    """Run `python3 -m status_mcp.probe <project>` in the sibling data plane."""
    if not STATUS_MCP.is_dir():
        sys.exit(f"status-mcp not found at {STATUS_MCP} — pass --from <probe.json>")
    proc = subprocess.run(
        [sys.executable, "-m", "status_mcp.probe", project],
        cwd=str(STATUS_MCP), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"probe failed for {project!r}:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout)


def load_report(project: str, from_file: str | None) -> dict:
    if from_file:
        return json.loads(Path(from_file).read_text())
    return probe(project)


# --------------------------------------------------------------------------- #
# availability helpers (the honesty layer)
# --------------------------------------------------------------------------- #
def _avail(report: dict, key: str) -> bool:
    a = (report.get("availability") or {}).get(key)
    return a in ("live", "partial")


# --------------------------------------------------------------------------- #
# narration — generated from the data, honoring anti-fabrication.
# One entry per slide widget; consumed by BOTH the deck (visible 🔊 line) and
# the narration companion / narrate-demo yaml (spoken `say:` line).
# --------------------------------------------------------------------------- #
def build_narration(report: dict) -> dict:
    proj = report.get("project", "this project")
    t = report.get("tickets") or {}
    q = report.get("tests") or {}
    d = report.get("deploy") or {}
    vr = report.get("visual_review") or {}
    dec = report.get("decisions") or []
    oq = report.get("open_questions") or []

    # ---- title ----
    parts = [f"Here's {proj}."]
    if _avail(report, "tickets"):
        prog = f", {t.get('in_progress')} in progress" if t.get("in_progress") else ", none in progress"
        parts.append(f"{t.get('done')} of {t.get('total')} tickets done{prog}.")
    else:
        parts.append("I don't have ticket-board data.")
    if _avail(report, "deploy") and d.get("status"):
        parts.append(f"Last deploy looks {d.get('status')}.")
    else:
        parts.append("No deploy data.")
    if _avail(report, "tests") and q.get("available"):
        parts.append(f"{q.get('passing')} of {q.get('count')} tests passing.")
    else:
        parts.append("I don't have test data, though — flag that.")
    title = " ".join(parts)

    # ---- tickets ----
    if _avail(report, "tickets"):
        tickets = (f"{t.get('done')} done, {t.get('todo')} to do, "
                   f"{t.get('in_progress')} in progress, {t.get('blocked')} blocked. "
                   "This is the board Status, not open-versus-closed.")
    else:
        tickets = "The ticket board wasn't reachable, so I won't guess a count."

    # ---- tests (the guardrail, spoken) ----
    if _avail(report, "tests") and q.get("available"):
        cov = f", {q.get('coverage_pct')} percent coverage" if q.get("coverage_pct") is not None else ""
        fail = f", {q.get('failing')} failing" if q.get("failing") else ", none failing"
        tests = f"{q.get('passing')} of {q.get('count')} tests passing{fail}{cov}."
    else:
        tests = ("I can't answer how many tests pass — there's no test data. "
                 "That's an open item, not a green light.")

    # ---- deploy ----
    if _avail(report, "deploy") and d.get("last_deployed_at"):
        dur = f", took {d.get('duration_s')} seconds" if d.get("duration_s") is not None else ""
        deploy = (f"Shipped {d.get('last_deployed_at')} to {d.get('env')}, "
                  f"commit {str(d.get('commit'))[:10]}, {d.get('status')}{dur}.")
    else:
        deploy = "No deploy record was reachable for this target."

    # ---- visual review ----
    arts = vr.get("artifacts") or []
    if _avail(report, "visual_review"):
        has_video = any((a.get("kind") == "video") for a in arts)
        vtail = "including a rendered walkthrough." if has_video else \
            "but no rendered walkthrough video yet — want me to generate one?"
        visual = f"{len(arts)} review artifacts exist, {vtail}"
    else:
        visual = "The demos directory wasn't reachable."

    # ---- decisions ----
    if _avail(report, "decisions"):
        decisions = (f"{len(dec)} recent decisions, mined from merge commits. "
                     "Deeper close-work reasoning isn't wired yet — a known gap.")
    else:
        decisions = "Decision history wasn't reachable."

    # ---- open questions / close ----
    if oq:
        open_q = f"One thing needs you: {oq[0]} Everything else is on track."
    else:
        open_q = "Nothing needs you right now — everything's on track."

    return {
        "title": title, "tickets": tickets, "tests": tests, "deploy": deploy,
        "visual": visual, "decisions": decisions, "open_questions": open_q,
    }


# Slide order must match the `builders` array in template.html.
SLIDE_ORDER = ["title", "tickets", "tests", "deploy", "visual", "decisions", "open_questions"]


# --------------------------------------------------------------------------- #
# emit
# --------------------------------------------------------------------------- #
def render_html(report: dict, narration: dict) -> str:
    data = dict(report)
    data["narration"] = narration
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tpl = TEMPLATE.read_text()
    title = f"{report.get('project','project')} — status deck"
    return tpl.replace("__TITLE__", title).replace("__DECK_DATA__", blob)


def _yaml_dq(s: str) -> str:
    """Minimal YAML double-quoted scalar (stdlib only — no PyYAML dependency)."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_demo_yaml(report: dict, narration: dict, deck_url: str) -> str:
    """narrate-demo script: each slide is a `goto <deck>#N` step whose spoken
    line is that slide's narration. Slide advance rides the deck's hash router
    (`#N`), so narrate-demo drives it with its stock `goto` verb — no custom
    automation and no edits to ai-harness. `output` is relative to this yaml."""
    proj = report.get("project", "project")
    lines = [
        f"# Generated by `python -m deck.generate {proj}` — narrate-demo talk-over script.",
        "# Render:  ../../ai-harness/narrate/narrate render "
        f"demos/{proj}-status.demo.yaml   (-> demos/out/{proj}-status.mp4)",
        f"title: {_yaml_dq(proj + ' — status readout')}",
        f"output: out/{proj}-status.mp4",
        "viewport: {width: 1440, height: 900}",
        "tts:",
        "  engine: say        # macOS `say`, keyless",
        "  voice: Samantha",
        "  rate: 180",
        "steps:",
    ]
    for n, w in enumerate(SLIDE_ORDER):
        lines.append(f"  - say: {_yaml_dq(narration[w])}")
        lines.append("    do: goto")
        lines.append(f"    url: {_yaml_dq(deck_url + '#' + str(n))}")
    # Hold a beat on the last slide so the closing line isn't cut off.
    lines.append('  - say: "That\'s the readout — ask me for any section in detail."')
    lines.append("    do: wait")
    lines.append("    ms: 900")
    return "\n".join(lines) + "\n"


def narration_companion(report: dict, narration: dict) -> dict:
    """Per-slide {widget, narration} — reusable by narrate-demo AND a live voice loop."""
    return {
        "project": report.get("project"),
        "generated_at": report.get("generated_at"),
        "source": "status_mcp.probe",
        "slides": [{"index": n, "widget": w, "narration": narration[w]}
                   for n, w in enumerate(SLIDE_ORDER)],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deck.generate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="project key (e.g. dstrader, familyfund)")
    ap.add_argument("--from", dest="from_file", help="read StatusReport JSON from a file instead of probing")
    ap.add_argument("--outdir", default="out", help="output directory (default: ./out)")
    args = ap.parse_args(argv)

    report = load_report(args.project, args.from_file)
    narration = build_narration(report)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    proj = report.get("project", args.project)

    html_path = outdir / f"{proj}-status.html"
    narr_path = outdir / f"{proj}-status.narration.json"
    html_path.write_text(render_html(report, narration))
    narr_path.write_text(json.dumps(narration_companion(report, narration), indent=2, ensure_ascii=False))

    # narrate-demo talk-over script (points at the deck we just wrote).
    demos = Path("demos")
    demos.mkdir(parents=True, exist_ok=True)
    demo_path = demos / f"{proj}-status.demo.yaml"
    deck_url = html_path.resolve().as_uri()
    demo_path.write_text(build_demo_yaml(report, narration, deck_url))

    print(f"wrote {html_path}  ({html_path.stat().st_size} bytes, self-contained)")
    print(f"wrote {narr_path}")
    print(f"wrote {demo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

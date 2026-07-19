"""Read-only signal collectors. Each returns (data, availability, warnings).

availability ∈ {"live", "partial", "unavailable"}. Collectors NEVER raise for a
missing signal — they degrade to unavailable + a warning, so one dead signal
never sinks the whole report. Nothing here mutates any real system.
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ProjectConfig

_TIMEOUT = 25  # seconds per external command


# --- small utils ------------------------------------------------------------
def _run(cmd: list[str], cwd: Optional[Path] = None) -> tuple[bool, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        return p.returncode == 0, p.stdout, p.stderr.strip()
    except FileNotFoundError:
        return False, "", f"{cmd[0]}: not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "", f"{cmd[0]}: timed out after {_TIMEOUT}s"
    except Exception as e:  # pragma: no cover - defensive
        return False, "", f"{cmd[0]}: {e}"


def _to_utc_z(ts: str) -> Optional[str]:
    """Normalize an ISO8601 (possibly offset) timestamp to '...Z' UTC."""
    if not ts:
        return None
    try:
        s = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _mtime_z(p: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except OSError:
        return None


def _bucket(state: str) -> str:
    s = (state or "").strip().lower()
    if not s:
        return "todo"
    if "block" in s:
        return "blocked"
    if "progress" in s:
        return "in_progress"
    if "done" in s or "closed" in s or "complete" in s:
        return "done"
    return "todo"


# --- tickets ----------------------------------------------------------------
def collect_tickets(cfg: ProjectConfig):
    warnings: list[str] = []
    empty = {"total": 0, "todo": 0, "in_progress": 0, "done": 0, "blocked": 0,
             "items": [], "source": None}
    if not cfg.gh_project_number:
        return empty, "unavailable", ["tickets: no gh_project_number configured"]

    import json
    ok, out, err = _run([
        "gh", "project", "item-list", str(cfg.gh_project_number),
        "--owner", "@me", "--format", "json", "--limit", "200",
    ])
    if not ok:
        warnings.append(f"tickets: gh failed ({err or 'unknown'}); is `gh auth` set + `project` scope granted?")
        return empty, "unavailable", warnings
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return empty, "unavailable", ["tickets: could not parse gh JSON"]

    counts = {"todo": 0, "in_progress": 0, "done": 0, "blocked": 0}
    items = []
    drafts = 0
    for it in data.get("items", []):
        content = it.get("content") or {}
        repo = content.get("repository")
        number = content.get("number")
        if number is None:                    # board draft note, no issue
            drafts += 1
            continue
        if cfg.gh_repo and repo != cfg.gh_repo:
            continue
        state = it.get("status") or ""
        counts[_bucket(state)] += 1
        items.append({
            "id": str(number),
            "title": content.get("title", ""),
            "state": state or "Todo",
            "url": content.get("url", ""),
            "repo": repo,
        })
    if drafts:
        warnings.append(f"tickets: {drafts} board draft note(s) not attributable to a repo (excluded from counts)")

    tickets = {
        "total": len(items),
        **counts,
        "items": items,
        "source": f"gh project item-list {cfg.gh_project_number} (repo={cfg.gh_repo})",
    }
    return tickets, "live", warnings


# --- tests ------------------------------------------------------------------
def collect_tests(cfg: ProjectConfig):
    warnings: list[str] = []
    null = {"available": False, "count": None, "passing": None, "failing": None,
            "skipped": None, "coverage_pct": None, "last_run": None, "source": None}
    repo = cfg.resolved_repo_path()
    if not repo or not repo.exists():
        return null, "unavailable", ["tests: repo_path not found"]

    sure_dir = repo / cfg.surefire_dir
    xmls = sorted(sure_dir.glob("TEST-*.xml")) + sorted(sure_dir.glob("*.xml")) \
        if sure_dir.exists() else []
    xmls = list(dict.fromkeys(xmls))  # de-dup, keep order

    count = fail = err = skip = 0
    last_run = None
    parsed_any = False
    for x in xmls:
        try:
            root = ET.parse(x).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
        for s in suites:
            parsed_any = True
            count += int(s.get("tests", 0) or 0)
            fail += int(s.get("failures", 0) or 0)
            err += int(s.get("errors", 0) or 0)
            skip += int(s.get("skipped", 0) or 0)
        mz = _mtime_z(x)
        if mz and (last_run is None or mz > last_run):
            last_run = mz

    coverage = _parse_coverage(repo, cfg, warnings)

    if not parsed_any and coverage is None:
        return null, "unavailable", warnings + ["tests: no surefire reports or coverage found (suite not run?)"]

    if not parsed_any:
        # coverage only, no test counts
        return ({"available": True, "count": None, "passing": None, "failing": None,
                 "skipped": None, "coverage_pct": coverage, "last_run": last_run,
                 "source": f"{cfg.jacoco_csv} (no surefire reports)"},
                "partial", warnings + ["tests: coverage found but no surefire reports"])

    failing = fail + err
    result = {
        "available": True,
        "count": count,
        "passing": max(count - failing - skip, 0),
        "failing": failing,
        "skipped": skip,
        "coverage_pct": coverage,
        "last_run": last_run,
        "source": os.path.relpath(sure_dir, repo) + (
            " + jacoco" if coverage is not None else ""),
    }
    avail = "live" if coverage is not None else "partial"
    if coverage is None:
        warnings.append("tests: no JaCoCo coverage artifact found")
    return result, avail, warnings


def _parse_coverage(repo: Path, cfg: ProjectConfig, warnings: list[str]) -> Optional[float]:
    csv_path = repo / cfg.jacoco_csv
    if csv_path.exists():
        try:
            missed = covered = 0
            with csv_path.open(newline="") as f:
                for row in csv.DictReader(f):
                    missed += int(row.get("INSTRUCTION_MISSED", 0) or 0)
                    covered += int(row.get("INSTRUCTION_COVERED", 0) or 0)
            total = missed + covered
            if total:
                return round(covered / total * 100, 1)
        except (OSError, ValueError, KeyError) as e:
            warnings.append(f"tests: jacoco.csv parse issue ({e})")
    xml_path = repo / cfg.jacoco_xml
    if xml_path.exists():
        try:
            root = ET.parse(xml_path).getroot()
            # report-level INSTRUCTION counter is the last direct child <counter>
            for c in reversed([c for c in root if c.tag == "counter"]):
                if c.get("type") == "INSTRUCTION":
                    m = int(c.get("missed", 0)); cov = int(c.get("covered", 0))
                    if m + cov:
                        return round(cov / (m + cov) * 100, 1)
        except (ET.ParseError, ValueError):
            warnings.append("tests: jacoco.xml parse issue")
    return None


# --- deploy -----------------------------------------------------------------
_ROW = re.compile(r"^\|(.+)\|\s*$")


def collect_deploy(cfg: ProjectConfig):
    warnings: list[str] = []
    null = {"last_deployed_at": None, "env": "", "commit": "", "status": "unknown",
            "duration_s": None, "target": cfg.deploy_target, "source": None}
    if not cfg.deploy_index:
        return null, "unavailable", ["deploy: no deploy_index configured"]
    idx = Path(os.path.expanduser(cfg.deploy_index))
    if not idx.exists():
        return null, "unavailable", [f"deploy: index not found at {idx}"]

    try:
        lines = idx.read_text().splitlines()
    except OSError as e:
        return null, "unavailable", [f"deploy: cannot read index ({e})"]

    for line in lines:
        m = _ROW.match(line)
        if not m:
            continue
        cells = [c.strip().strip("`") for c in m.group(1).split("|")]
        if len(cells) < 5:
            continue
        started, target, status, head = cells[0], cells[1], cells[2], cells[3]
        if started.lower().startswith("started") or set(started) <= {"-", ":"}:
            continue  # header / separator
        if cfg.deploy_target and target != cfg.deploy_target:
            continue
        dur = None
        if len(cells) >= 5:
            dm = re.match(r"(\d+(?:\.\d+)?)\s*s", cells[4])
            if dm:
                dur = float(dm.group(1))
        norm = {"success": "ok", "failure": "failed"}.get(status.lower(), "unknown")
        return ({
            "last_deployed_at": _to_utc_z(started),
            "env": target,
            "commit": head,
            "status": norm,
            "duration_s": dur,
            "target": target,
            "source": os.path.relpath(idx, os.path.expanduser("~")),
        }, "live", warnings)

    warnings.append(f"deploy: no rows for target '{cfg.deploy_target}' in index")
    return null, "unavailable", warnings


# --- visual review ----------------------------------------------------------
_VIDEO = {".mp4", ".mov", ".webm", ".m4v"}


def collect_visual_review(cfg: ProjectConfig):
    warnings: list[str] = []
    empty = {"done": False, "artifacts": [], "source": None}
    if not cfg.demos_dir:
        return empty, "unavailable", ["visual_review: no demos_dir configured"]
    d = Path(os.path.expanduser(cfg.demos_dir))
    if not d.exists():
        return empty, "unavailable", [f"visual_review: demos dir not found at {d}"]

    artifacts = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        name = p.name
        low = name.lower()
        if p.suffix.lower() in _VIDEO:
            kind = "video"
        elif low.endswith(".demo.yaml") or low.endswith(".demo.yml"):
            kind = "demo_script"
        elif "explainer" in low and low.endswith(".html"):
            kind = "explainer"
        elif p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}:
            kind = "screenshot"
        else:
            continue
        artifacts.append({"name": name, "kind": kind, "path": str(p),
                          "when": _mtime_z(p)})

    strong = [a for a in artifacts if a["kind"] in ("video", "demo_script", "explainer")]
    result = {
        "done": bool(strong),
        "artifacts": artifacts,
        "source": str(d),
    }
    if not artifacts:
        return result, "unavailable", warnings + ["visual_review: no artifacts in demos dir"]
    if not any(a["kind"] == "video" for a in artifacts):
        warnings.append("visual_review: demo scripts/explainers present but no rendered video (.mp4)")
    return result, "live", warnings


# --- decisions --------------------------------------------------------------
def collect_decisions(cfg: ProjectConfig, limit: int = 12):
    warnings: list[str] = []
    repo = cfg.resolved_repo_path()
    decisions = []
    availability = "unavailable"

    if repo and (repo / ".git").exists():
        ok, out, err = _run(
            ["git", "log", "--merges", "-n", str(limit),
             "--pretty=format:%h|%cI|%s"], cwd=repo)
        if ok:
            availability = "live"
            for line in out.splitlines():
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                sha, when, summary = parts
                decisions.append({
                    "when": _to_utc_z(when),
                    "summary": summary.strip(),
                    "source": "merge-commit",
                    "ref": sha,
                })
        else:
            warnings.append(f"decisions: git log failed ({err})")
    else:
        warnings.append("decisions: repo_path has no .git; merge history unavailable")

    return decisions, availability, warnings

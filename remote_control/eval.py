"""Manager guideline eval harness -- Phase 3 runner (ai-harness#67).

``python3 -m remote_control eval run [--guidelines PATH] [--out DIR]
[--judges claude[,codex]]`` replays every case under
``tests/manager_cases/`` through :func:`manager.analyze_action` and scores
the produced rec against the case's expected rec via one or more LLM
judges (``claude -p`` and/or ``codex exec``). Phases + judge contract live
in ``docs/manager-eval-harness.md``.

Cache key is ``(case_id, guidelines_hash, sha256(actual_rec), judge_name)``
-- judges cache independently so adding a second judge to an already-scored
corpus doesn't re-spend the first one's quota. Output land under
``out/eval/<guidelines_hash>/`` (gitignored).

The pure helpers (hashing, prompt build, judge-output parse, aggregation,
cache I/O) live above ``main`` and are unit-tested with a fake judge. The
subprocess seams (``judge_claude``, ``judge_codex``) and the analyzer call
take ``runner`` injections so the tests don't shell out.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import eval_cases, manager
from .config import CodexConfig, ManagerConfig


EVAL_SCHEMA_VERSION = 1
ALL_JUDGES: Tuple[str, ...] = ("claude", "codex")
DEFAULT_JUDGES: Tuple[str, ...] = ("claude",)
DEFAULT_N_SAMPLES = 3
DEFAULT_WORKERS = 4
DEFAULT_JUDGE_TIMEOUT = 600
VERDICTS: Tuple[str, ...] = ("equivalent", "better", "worse")


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def actual_payload_for_hash(actual: Dict[str, Any]) -> str:
    """Stable JSON of just the two rec halves -- analysis prose changes
    wording every run so it doesn't belong in the cache key; only the
    recommendation does. Whitespace-stripped so " yes " and "yes" share a
    hash."""
    payload = {
        "rec_manager": (actual.get("rec_manager") or "").strip(),
        "rec_session": (actual.get("rec_session") or "").strip(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def actual_hash(actual: Dict[str, Any]) -> str:
    return sha256_text(actual_payload_for_hash(actual))


def guidelines_hash_of(path: Path) -> str:
    """sha256 of the guidelines file contents. Missing file hashes the empty
    string (so a fresh checkout still produces a deterministic out dir
    instead of crashing)."""
    try:
        return sha256_text(Path(path).read_text())
    except OSError:
        return sha256_text("")


# --------------------------------------------------------------------------- #
# Judge prompt + parse
# --------------------------------------------------------------------------- #
JUDGE_PROMPT_TEMPLATE = """\
You are evaluating an autonomous session-manager's recommendation for ONE
situation against the operator's expected recommendation for that same
situation. The manager produces two parallel halves: a MANAGER half (a
lifecycle op the manager carries out itself -- e.g. archive, fork + resume,
or "none") and a SESSION half (a one-line instruction to deliver into the
live session, or "none"). Score the ACTUAL recs against the EXPECTED recs.

SITUATION:
---
{input_block}
---

EXPECTED RECOMMENDATION:
  MANAGER: {expected_manager}
  SESSION: {expected_session}

ACTUAL RECOMMENDATION:
  MANAGER: {actual_manager}
  SESSION: {actual_session}

Reply with EXACTLY one line of JSON and nothing else, exactly this shape:
{{"verdict":"equivalent|better|worse","score":<1-5 integer>,"reason":"<= 30 words"}}

Where:
- "equivalent" -> the actual matches the expected intent (same lifecycle
  op, same instruction -- wording differences are fine).
- "better"     -> the actual is materially better than the expected (rare;
  use only when the expected itself is wrong for this situation).
- "worse"      -> the actual misses, contradicts, or risks harm.
- score: 5 = clearly correct/better; 4 = same intent, minor wording
  difference; 3 = same direction, partial; 2 = wrong direction but
  recoverable; 1 = harmful or dangerously wrong.
"""


def build_judge_prompt(case: Dict[str, Any], actual: Dict[str, Any]) -> str:
    inp = case.get("input") or {}
    act = inp.get("action") or {}
    meta = inp.get("session_meta") or {}
    tail = inp.get("transcript_tail") or []
    tail_lines: List[str] = []
    for t in tail:
        role = (t.get("role") or "?").strip()
        text = (t.get("text") or "").strip()
        if text:
            tail_lines.append(f"[{role}] {text}")
    tail_block = "\n".join(tail_lines) if tail_lines else "(no transcript)"
    input_block = (
        f"action.kind: {act.get('kind', '')}\n"
        f"action.reason: {act.get('reason', '')}\n"
        f"action.question: {act.get('question', '')}\n"
        f"action.fresh: {act.get('fresh', '')}\n"
        f"action.managed: {act.get('managed', '')}\n"
        f"session_meta: {json.dumps(meta) if meta else '{}'}\n"
        f"transcript_tail:\n{tail_block}"
    )
    expected = case.get("expected") or {}
    return JUDGE_PROMPT_TEMPLATE.format(
        input_block=input_block,
        expected_manager=(expected.get("rec_manager") or "none") or "none",
        expected_session=(expected.get("rec_session") or "none") or "none",
        actual_manager=(actual.get("rec_manager") or "none") or "none",
        actual_session=(actual.get("rec_session") or "none") or "none",
    )


def _find_verdict_object(text: str) -> Optional[str]:
    """Brace-balanced scan for the first ``{...}`` containing ``"verdict"``.
    Tolerates the judge wrapping its JSON in fences / preamble / trailing
    prose -- we only care that the object parses and carries a verdict."""
    if not text:
        return None
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                cand = text[start:i + 1]
                if '"verdict"' in cand:
                    try:
                        json.loads(cand)
                        return cand
                    except ValueError:
                        pass
                start = -1
    return None


def parse_judge_output(text: str) -> Dict[str, Any]:
    """Pull ``{verdict, score, reason}`` out of judge stdout. Returns a
    sample dict either way -- an unparseable judge contributes a score=1
    "worse" sample with an ``error`` marker so a bad judge call is visible
    in the aggregate rather than silently dropped."""
    if not text or not text.strip():
        return {"verdict": "worse", "score": 1,
                "reason": "judge produced no output", "error": "empty"}
    cand = _find_verdict_object(text)
    if cand is None:
        return {"verdict": "worse", "score": 1,
                "reason": f"unparseable judge output: {text[:160]!r}",
                "error": "parse"}
    try:
        obj = json.loads(cand)
    except (ValueError, TypeError):
        return {"verdict": "worse", "score": 1,
                "reason": "judge JSON failed to parse", "error": "parse"}
    if not isinstance(obj, dict):
        return {"verdict": "worse", "score": 1,
                "reason": "judge JSON was not an object", "error": "shape"}
    verdict = (obj.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return {"verdict": "worse", "score": 1,
                "reason": f"unknown verdict {verdict!r}", "error": "verdict"}
    try:
        score = int(obj.get("score"))
    except (TypeError, ValueError):
        score = 1
    score = max(1, min(5, score))
    reason = (str(obj.get("reason") or "")).strip()[:500]
    return {"verdict": verdict, "score": score, "reason": reason}


# --------------------------------------------------------------------------- #
# Judge subprocess seams (injectable)
# --------------------------------------------------------------------------- #
JudgeFn = Callable[[str], Dict[str, Any]]


def _run_subprocess(cmd: Sequence[str], *, timeout: int,
                    runner=subprocess.run, log) -> Optional[str]:
    """Run one judge subprocess; return stdout or None on any failure. Logs
    are best-effort -- the parser turns None into a low-score sample so
    one timed-out judge doesn't abort the whole case."""
    try:
        proc = runner(list(cmd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"judge timed out after {timeout}s: {cmd[0]}")
        return None
    except (OSError, ValueError) as e:
        log(f"judge spawn failed: {e}")
        return None
    if proc.returncode != 0:
        log(f"judge rc={proc.returncode}: {(proc.stderr or '')[-200:]}")
        return None
    return (proc.stdout or "").strip() or None


def judge_claude(prompt: str, *, claude_bin: str,
                 model: Optional[str] = None,
                 timeout: int = DEFAULT_JUDGE_TIMEOUT,
                 runner=subprocess.run, log=print) -> Dict[str, Any]:
    """Live judge: ``claude -p --permission-mode plan``. ``plan`` keeps it
    read-only (it doesn't need to touch the repo to score a snippet)."""
    cmd = [str(claude_bin), "-p", "--permission-mode", "plan"]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return parse_judge_output(_run_subprocess(
        cmd, timeout=timeout, runner=runner, log=log) or "")


def judge_codex(prompt: str, *, codex_bin: str,
                timeout: int = DEFAULT_JUDGE_TIMEOUT,
                runner=subprocess.run, log=print) -> Dict[str, Any]:
    """Live second judge: ``codex exec``. Same JSON contract as the claude
    judge so disagreement (verdict mismatch / score delta >= 2) shows up in
    the per-case judges block."""
    cmd = [str(codex_bin), "exec", prompt]
    return parse_judge_output(_run_subprocess(
        cmd, timeout=timeout, runner=runner, log=log) or "")


def default_judges(*, claude_bin: str, codex_bin: str,
                   model: Optional[str] = None,
                   timeout: int = DEFAULT_JUDGE_TIMEOUT,
                   runner=subprocess.run, log=print) -> Dict[str, JudgeFn]:
    """Bind the live subprocess judges as zero-arg-per-prompt callables.
    The runner accepts any ``{name: callable}`` map so tests pass in a
    deterministic fake instead of shelling out."""
    return {
        "claude": lambda p: judge_claude(p, claude_bin=claude_bin, model=model,
                                         timeout=timeout, runner=runner,
                                         log=log),
        "codex": lambda p: judge_codex(p, codex_bin=codex_bin,
                                       timeout=timeout, runner=runner,
                                       log=log),
    }


# --------------------------------------------------------------------------- #
# Sampling + aggregation
# --------------------------------------------------------------------------- #
def aggregate_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``n`` samples -> ``{n, mean, stdev, verdict_mode, samples}``. Uses
    population stdev (``pstdev``) so n=1 reports 0 stdev instead of raising.
    ``verdict_mode`` is the most-common verdict; ties resolve by Counter's
    insertion order, which mirrors sample order."""
    scores: List[int] = []
    verdicts: List[str] = []
    for s in samples:
        try:
            scores.append(int(s.get("score", 0)))
        except (TypeError, ValueError):
            continue
        v = s.get("verdict", "")
        if v:
            verdicts.append(v)
    mean = round(statistics.fmean(scores), 3) if scores else 0.0
    stdev = round(statistics.pstdev(scores), 3) if len(scores) > 1 else 0.0
    verdict_mode = (Counter(verdicts).most_common(1)[0][0]
                    if verdicts else "worse")
    return {"n": len(samples), "mean": mean, "stdev": stdev,
            "verdict_mode": verdict_mode, "samples": samples}


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def cache_path(cache_dir: Path, case_id: str, guidelines_hash_s: str,
               actual_hash_s: str, judge_name: str) -> Path:
    """One file per (case, guidelines, actual, judge) -- that quadruple is
    the cache key in the plan doc. Filename embeds short hash prefixes so
    the cache is human-inspectable on disk; the directory name still
    namespaces by judge so deleting one judge's cache is one ``rm -rf``."""
    return (cache_dir / judge_name
            / f"{case_id}__{guidelines_hash_s[:12]}__{actual_hash_s[:12]}.json")


def load_cached(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_cached(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Per-case judge run
# --------------------------------------------------------------------------- #
def score_case(*, case: Dict[str, Any], actual: Dict[str, Any],
               guidelines_hash_s: str, judge_name: str, judge_fn: JudgeFn,
               cache_dir: Path, n: int = DEFAULT_N_SAMPLES,
               log=print) -> Dict[str, Any]:
    """Run ``n`` judge samples for one (case, actual) -- or return the
    cached aggregate verbatim on cache hit. A raising sample is captured as
    a score=1 worse sample with an ``error`` marker so one bad call doesn't
    drop a whole case."""
    case_id = case.get("id") or "case"
    ah = actual_hash(actual)
    cpath = cache_path(cache_dir, case_id, guidelines_hash_s, ah, judge_name)
    cached = load_cached(cpath)
    if cached is not None:
        cached["cached"] = True
        return cached
    prompt = build_judge_prompt(case, actual)
    samples: List[Dict[str, Any]] = []
    for i in range(n):
        try:
            sample = judge_fn(prompt)
        except Exception as e:
            log(f"judge {judge_name} sample {i} for {case_id} raised: {e}")
            sample = {"verdict": "worse", "score": 1, "reason": str(e),
                      "error": "raised"}
        if not isinstance(sample, dict):
            sample = {"verdict": "worse", "score": 1,
                      "reason": "non-dict sample", "error": "bad-shape"}
        samples.append(sample)
    agg = aggregate_samples(samples)
    agg["cached"] = False
    save_cached(cpath, agg)
    return agg


# --------------------------------------------------------------------------- #
# Analyzer replay + per-case run
# --------------------------------------------------------------------------- #
def replay_case(case: Dict[str, Any], cfg: ManagerConfig, guidelines: str,
                *, runner=subprocess.run, log=print) -> Dict[str, Any]:
    """Reconstruct the case's :class:`Action` and call
    :func:`manager.analyze_action` with the frozen transcript_tail injected,
    so the live analyzer doesn't need the on-disk transcript that may have
    rotated away."""
    inp = case.get("input") or {}
    action_d = inp.get("action") or {}
    action = eval_cases.action_from_jsonable(action_d)
    transcript_tail = inp.get("transcript_tail") or None
    return manager.analyze_action(
        action, cfg=cfg, guidelines=guidelines, log=log, runner=runner,
        transcript_tail=transcript_tail,
    )


def run_case(case: Dict[str, Any], *, cfg: ManagerConfig, guidelines: str,
             guidelines_hash_s: str, out_dir: Path, cache_dir: Path,
             judges: Dict[str, JudgeFn], n: int = DEFAULT_N_SAMPLES,
             analyzer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
             log=print) -> Dict[str, Any]:
    """Replay one case + score with every named judge + persist the result
    JSON. *analyzer* is the (injectable) callable that produces the actual
    rec from the case; defaults to :func:`replay_case` (the live analyzer),
    tests pass a fake so the suite never shells out."""
    case_id = case.get("id") or "case"
    if analyzer is None:
        def analyzer(c: Dict[str, Any]) -> Dict[str, Any]:
            return replay_case(c, cfg, guidelines, log=log)
    actual = analyzer(case)
    if not isinstance(actual, dict):
        actual = {"ok": False, "rec_manager": "", "rec_session": "",
                  "analysis": "", "note": "analyzer returned non-dict",
                  "raw": ""}
    judges_block: Dict[str, Any] = {}
    for name, fn in judges.items():
        judges_block[name] = score_case(
            case=case, actual=actual,
            guidelines_hash_s=guidelines_hash_s,
            judge_name=name, judge_fn=fn,
            cache_dir=cache_dir, n=n, log=log,
        )
    result = {
        "schema": EVAL_SCHEMA_VERSION,
        "case_id": case_id,
        "guidelines_hash": guidelines_hash_s,
        "actual_hash": actual_hash(actual),
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actual": {
            "ok": bool(actual.get("ok")),
            "rec_manager": actual.get("rec_manager", ""),
            "rec_session": actual.get("rec_session", ""),
            "recommendation": actual.get("recommendation", ""),
            "analysis": actual.get("analysis", ""),
            "note": actual.get("note", ""),
        },
        "expected": case.get("expected", {}),
        "judges": judges_block,
    }
    out_path = out_dir / f"{case_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")
    tmp.replace(out_path)
    return result


# --------------------------------------------------------------------------- #
# Suite orchestration
# --------------------------------------------------------------------------- #
def summarize_results(results: Dict[str, Dict[str, Any]],
                      judge_names: Sequence[str]) -> Dict[str, Any]:
    """Suite-level rollup: per-judge mean of per-case mean scores + a
    Counter over the per-case verdict modes (so a regression that flips
    `equivalent` to `worse` across multiple cases is visible at a glance)."""
    out: Dict[str, Any] = {}
    for name in judge_names:
        scores: List[float] = []
        verdicts: List[str] = []
        for r in results.values():
            j = (r.get("judges") or {}).get(name) if isinstance(r, dict) else None
            if not isinstance(j, dict):
                continue
            try:
                scores.append(float(j.get("mean", 0.0)))
            except (TypeError, ValueError):
                continue
            v = j.get("verdict_mode", "")
            if v:
                verdicts.append(v)
        out[name] = {
            "cases": len(scores),
            "mean_score": round(statistics.fmean(scores), 3) if scores else 0.0,
            "verdict_counts": dict(Counter(verdicts)),
        }
    return out


def run_eval(*, cases_dir: Path, out_root: Path, guidelines_path: Path,
             judge_names: Sequence[str], cfg: ManagerConfig,
             judges_factory: Optional[Callable[..., Dict[str, JudgeFn]]] = None,
             analyzer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
             n: int = DEFAULT_N_SAMPLES, workers: int = DEFAULT_WORKERS,
             runner=subprocess.run, log=print) -> Dict[str, Any]:
    """Load every case under *cases_dir*, fan them across a thread pool,
    score each with every named judge, persist the per-case + summary JSON.
    ``judges_factory`` lets tests inject a fake-judge dispatch table; the
    default binds the live ``claude -p`` / ``codex exec`` subprocess
    judges via :func:`default_judges`."""
    cases = eval_cases.list_cases(cases_dir)
    try:
        guidelines = Path(guidelines_path).read_text()
    except OSError:
        guidelines = ""
    gh = sha256_text(guidelines)
    out_dir = out_root / gh
    cache_dir = out_dir / ".cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    codex_bin = str(CodexConfig.from_env().codex_bin)
    factory = judges_factory or default_judges
    all_judges = factory(claude_bin=str(cfg.claude_bin), codex_bin=codex_bin,
                         model=cfg.model, timeout=cfg.investigator_timeout_secs,
                         runner=runner, log=log)
    judges = {name: all_judges[name] for name in judge_names if name in all_judges}
    missing = [n for n in judge_names if n not in all_judges]
    if missing:
        raise ValueError(f"unknown judge(s): {missing!r}; "
                         f"available: {sorted(all_judges)}")
    if not judges:
        raise ValueError(f"no usable judges in {judge_names!r}")

    results: Dict[str, Dict[str, Any]] = {}

    def _do(case: Dict[str, Any]) -> Dict[str, Any]:
        return run_case(case, cfg=cfg, guidelines=guidelines,
                        guidelines_hash_s=gh, out_dir=out_dir,
                        cache_dir=cache_dir, judges=judges, n=n,
                        analyzer=analyzer, log=log)

    workers = max(1, int(workers))
    if workers == 1 or len(cases) <= 1:
        for case in cases:
            cid = case.get("id") or "case"
            try:
                results[cid] = _do(case)
            except Exception as e:
                log(f"case {cid} failed: {e}")
                results[cid] = {"case_id": cid, "error": str(e)}
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_id = {pool.submit(_do, c): (c.get("id") or "case")
                            for c in cases}
            for fut in concurrent.futures.as_completed(future_to_id):
                cid = future_to_id[fut]
                try:
                    results[cid] = fut.result()
                except Exception as e:
                    log(f"case {cid} failed: {e}")
                    results[cid] = {"case_id": cid, "error": str(e)}

    overall = summarize_results(results, list(judges.keys()))
    summary = {
        "schema": EVAL_SCHEMA_VERSION,
        "guidelines_hash": gh,
        "guidelines_path": str(guidelines_path),
        "out_dir": str(out_dir),
        "n_samples": n,
        "judges": list(judges.keys()),
        "n_cases": len(results),
        "cases": sorted(results),
        "overall": overall,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=False) + "\n")
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control eval <run|mine> [args]\n"
    "  run    Replay every test case under tests/manager_cases/ and score "
    "against expected.\n"
    "  mine   Backfill ANSWER cases from ~/.claude/projects/*.jsonl rollouts.\n"
)


def _parse_judges(raw: str) -> List[str]:
    names = [s.strip().lower() for s in (raw or "").split(",") if s.strip()]
    bad = [n for n in names if n not in ALL_JUDGES]
    if bad:
        raise ValueError(f"unknown judge(s) {bad!r}; known: {list(ALL_JUDGES)}")
    return names or list(DEFAULT_JUDGES)


def _cmd_run(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="python3 -m remote_control eval run",
        description="Replay manager test cases and score against expected.")
    p.add_argument("--guidelines",
                   help="path to the guideline doc (default = manager cfg)")
    p.add_argument("--out",
                   help="output root (default = <repo>/out/eval)")
    p.add_argument("--cases",
                   help="cases dir (default = manager cfg test_cases_dir)")
    p.add_argument("--judges", default=",".join(DEFAULT_JUDGES),
                   help=f"comma-list from {list(ALL_JUDGES)} "
                        f"(default {DEFAULT_JUDGES[0]})")
    p.add_argument("--n", type=int, default=DEFAULT_N_SAMPLES,
                   help=f"judge samples per case (default {DEFAULT_N_SAMPLES})")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"thread-pool size (default {DEFAULT_WORKERS})")
    args = p.parse_args(list(argv))
    try:
        judge_names = _parse_judges(args.judges)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    cfg = ManagerConfig.from_env()
    guidelines = Path(args.guidelines or cfg.guidelines_file)
    cases = Path(args.cases or cfg.test_cases_dir)
    out_root = Path(args.out or (cfg.logdir.parent / "out" / "eval"))
    summary = run_eval(
        cases_dir=cases, out_root=out_root, guidelines_path=guidelines,
        judge_names=judge_names, cfg=cfg, n=args.n, workers=args.workers,
        log=print,
    )
    print(json.dumps(summary, indent=2, sort_keys=False))
    return 0


def _cmd_mine(argv: Sequence[str]) -> int:
    from . import eval_mine
    return eval_mine.main(list(argv))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python3 -m remote_control eval`` dispatcher. Sub-subcommands:
    ``run`` (Phase 3 replay+judge, #67) and ``mine`` (Phase 2.5 corpus
    backfill from local rollouts, #71)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "run":
        return _cmd_run(rest)
    if sub == "mine":
        return _cmd_mine(rest)
    print(f"unknown eval subcommand: {sub}\n{USAGE}", file=sys.stderr)
    return 2

"""``python3 -m remote_control manager-ui`` -- a local web UI to REVIEW the
session-manager's analyses of stuck threads and improve its guidelines.

On load it does a live, read-only classify pass and shows **every stuck thread
with its reason**. Each thread can be analyzed by a headless ``claude -p``
investigator -- run with the **current guidelines as policy** -- which reports a
one-line **recommendation** + a ~5-sentence **analysis**. Analyses are cached
(per situation) so they aren't re-spawned needlessly; the background
``manager --loop`` fills them in too. You give free-text **feedback** per thread
(the signal to improve quality) and can **edit the guidelines** doc right here --
which then feeds the next analysis.

Stdlib only (``http.server``); binds ``127.0.0.1`` by default.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Tuple

from .config import ManagerConfig

# --------------------------------------------------------------------------- #
# in-memory analysis jobs (a `claude -p` run is slow -> async; the UI polls)
# --------------------------------------------------------------------------- #
_JOBS: dict = {}                 # session_id -> {"status": running|done|error, "msg"}
_JOBS_LOCK = threading.Lock()
# Manager-rec executions run separately (a `claude -p` that ACTS, so it's slower
# and higher-stakes than analysis) -> their own job map, surfaced per row.
_EXEC_JOBS: dict = {}            # session_id -> {"status": running|done|shadow|error, "msg"}
_EXEC_LOCK = threading.Lock()


def _set_job(sid: str, status: str, msg: str = "") -> None:
    with _JOBS_LOCK:
        _JOBS[sid] = {"status": status, "msg": msg}


def job_status(sid: str) -> dict:
    with _JOBS_LOCK:
        return dict(_JOBS.get(sid) or {})


def _set_exec(sid: str, status: str, msg: str = "") -> None:
    with _EXEC_LOCK:
        _EXEC_JOBS[sid] = {"status": status, "msg": msg}


def exec_status(sid: str) -> dict:
    with _EXEC_LOCK:
        return dict(_EXEC_JOBS.get(sid) or {})


# --------------------------------------------------------------------------- #
# feedback store (keyed by action signature so it sticks across re-detections)
# --------------------------------------------------------------------------- #
def load_feedback(path: Path) -> dict:
    """The feedback store as ``{sig: [{text, at}, ...]}`` (a list of notes per sig),
    migrating the legacy single-note shape on read."""
    from . import manager
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {sig: manager.normalize_feedback_entry(v) for sig, v in data.items()}


def add_feedback(path: Path, sig: str, text: str) -> list:
    """Append a new note to *sig*'s feedback list (history is kept). Returns the list."""
    fb = load_feedback(path)
    fb.setdefault(sig, []).append(
        {"text": text.strip(),
         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fb, indent=2, sort_keys=True))
    tmp.replace(path)
    return fb[sig]


# --------------------------------------------------------------------------- #
# guidelines doc (read/written by the "Edit guidelines" button; fed to analyses)
# --------------------------------------------------------------------------- #
def load_guidelines(cfg: ManagerConfig) -> str:
    try:
        return cfg.guidelines_file.read_text()
    except OSError:
        return ""


def save_guidelines(cfg: ManagerConfig, text: str) -> None:
    p = cfg.guidelines_file
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(p)


# --------------------------------------------------------------------------- #
# api handlers (return (status, json-able); injectable seams for tests)
# --------------------------------------------------------------------------- #
def api_stuck(cfg: ManagerConfig, *, get_token=None, scan=None,
              consider_all: bool = False) -> Tuple[int, dict]:
    """Live read-only detection of every stuck thread, joined with its cached
    analysis (recommendation/analysis), feedback, and any in-flight job."""
    from . import manager
    from .config import UsageLimitConfig
    from .usage_limit import monitor as _monitor
    get_token = get_token or _monitor.get_token
    scan = scan or manager.scan_actions

    ucfg = UsageLimitConfig.from_env()
    log: List[str] = []
    token = get_token(ucfg, log.append)
    if not token:
        return 200, {"ok": False, "rows": [],
                     "error": "could not read OAuth token from keychain"}
    now = datetime.now(timezone.utc)
    actions = scan(cfg, ucfg, token, now=now, consider_all=consider_all, log=log.append)
    by_sig = manager.latest_decision_by_sig(cfg)
    feedback = load_feedback(cfg.feedback_file)
    rows = []
    for a in actions:
        row = manager.action_row(a)
        sig = row.get("sig", "")
        rec = by_sig.get(sig) or {}
        row["recommendation"] = rec.get("recommendation", "")
        row["rec_manager"] = rec.get("rec_manager", "")   # the manager executes this
        row["rec_session"] = rec.get("rec_session", "")   # this is sent to the session
        row["analysis"] = rec.get("analysis", "")
        row["analyzed_at"] = rec.get("ts", "")
        row["has_analysis"] = bool(rec.get("analyzed"))
        row["note"] = rec.get("note", "")   # e.g. why an analysis produced nothing
        row["feedback"] = feedback.get(sig) or []   # list of {text, at} notes
        row["job"] = job_status(a.session_id).get("status", "")
        row["exec"] = exec_status(a.session_id)   # {status, msg} of a manager-rec run
        row["worktree"] = manager.worktree_status(manager.worktree_for(cfg, a.session_id))
        tx = manager.session_transcript(cfg, a.session_id)
        row["last_messages"] = manager.last_messages(tx, n=2)
        # Flag a cached analysis the session has since outgrown: compare its ts to
        # when the session's transcript was last written. Unknown (no local
        # transcript) -> never stale, so cloud/other-host rows aren't mislabeled.
        try:
            mt = tx.stat().st_mtime if tx else None
        except OSError:
            mt = None
        row["session_updated_at"] = (datetime.fromtimestamp(mt, timezone.utc)
                                     .isoformat(timespec="seconds")) if mt else ""
        row["analysis_stale"] = bool(row["has_analysis"]) and manager.analysis_is_stale(
            row["analyzed_at"], mt)
        rows.append(row)
    rows.sort(key=lambda r: (not r["actionable"], r["case"], r["session_id"]))
    return 200, {"ok": True, "at": now.isoformat(timespec="seconds"), "rows": rows,
                 "counts": {"sessions": len(rows),
                            "actionable": sum(1 for r in rows if r["actionable"]),
                            "analyzed": sum(1 for r in rows if r["has_analysis"]),
                            "stale": sum(1 for r in rows if r["analysis_stale"])}}


def run_analysis(cfg: ManagerConfig, session_id: str, *, force: bool = False,
                 get_token=None, runner=None) -> dict:
    """Analyze ONE stuck thread now (investigator + guidelines), append its record.
    Reuses the cached analysis unless *force*. Synchronous core of /api/analyze."""
    import subprocess
    from . import manager
    from .config import UsageLimitConfig
    from .usage_limit import monitor as _monitor
    get_token = get_token or _monitor.get_token
    runner = runner or subprocess.run

    ucfg = UsageLimitConfig.from_env()
    log: List[str] = []
    token = get_token(ucfg, log.append)
    if not token:
        return {"ok": False, "error": "could not read OAuth token from keychain"}
    rec = manager.analyze_session(cfg, ucfg, token, session_id,
                                  now=datetime.now(timezone.utc), force=force,
                                  log=log.append, runner=runner)
    if rec is None:
        return {"ok": False, "error": "not a stuck/actionable thread right now"}
    return {"ok": True, "record": rec}


def api_analyze(cfg: ManagerConfig, payload: dict) -> Tuple[int, dict]:
    """Kick off an async analysis for one session (a `claude -p` run). The UI polls
    /api/stuck for the result. ``force`` re-analyzes even if cached."""
    sid = (payload or {}).get("session_id")
    force = bool((payload or {}).get("force"))
    if not sid:
        return 400, {"error": "need {session_id}"}
    with _JOBS_LOCK:
        cur = _JOBS.get(sid)
        if cur and cur.get("status") == "running":
            return 200, {"status": "running"}
        _JOBS[sid] = {"status": "running", "msg": ""}

    def work():
        try:
            res = run_analysis(cfg, sid, force=force)
            _set_job(sid, "done" if res.get("ok") else "error",
                     res.get("error", "analyzed"))
        except Exception as e:  # noqa: BLE001 (surface to UI, don't crash the server)
            _set_job(sid, "error", str(e))

    threading.Thread(target=work, daemon=True).start()
    return 200, {"status": "started"}


def api_feedback(cfg: ManagerConfig, payload: dict) -> Tuple[int, dict]:
    sig = (payload or {}).get("sig")
    text = ((payload or {}).get("feedback") or "").strip()
    if not sig or not text:
        return 400, {"error": "need {sig, feedback}"}
    return 200, {"sig": sig, "feedback": add_feedback(cfg.feedback_file, sig, text)}


def api_guidelines_get(cfg: ManagerConfig) -> Tuple[int, dict]:
    return 200, {"text": load_guidelines(cfg), "path": str(cfg.guidelines_file)}


def api_guidelines_post(cfg: ManagerConfig, payload: dict) -> Tuple[int, dict]:
    if payload is None or "text" not in payload:
        return 400, {"error": "need {text}"}
    save_guidelines(cfg, payload["text"])
    return 200, {"ok": True, "bytes": len(payload["text"])}


def api_guidelines_suggest(cfg: ManagerConfig, *, runner=None) -> Tuple[int, dict]:
    """Run the guideline-writer over the feedback corpus and return a PROPOSED
    revised doc (not written -- the operator reviews it in the editor and Saves)."""
    import subprocess
    from . import manager
    res = manager.suggest_guidelines(cfg, log=lambda m: None,
                                     runner=runner or subprocess.run)
    return 200, res


def api_send(cfg: ManagerConfig, payload: dict, *, get_token=None, api=None) -> Tuple[int, dict]:
    """Deliver free text into a LIVE session as a user turn -- the operator's
    feedback, or the authorized recommendation. Outward-facing: the ui confirms
    before calling. (For an AskUserQuestion-waiting session, free text is delivered
    but won't resolve the tool call -- the submit-shape blocker, #22.)"""
    from . import manager
    from .config import UsageLimitConfig
    from .usage_limit import monitor as _monitor
    get_token = get_token or _monitor.get_token
    api = api or _monitor.api_request
    sid = (payload or {}).get("session_id")
    text = ((payload or {}).get("text") or "").strip()
    if not sid or not text:
        return 400, {"error": "need {session_id, text}"}
    ucfg = UsageLimitConfig.from_env()
    log: List[str] = []
    token = get_token(ucfg, log.append)
    if not token:
        return 200, {"ok": False, "error": "could not read OAuth token from keychain"}
    ok = manager.post_answer(ucfg, token, sid, text, api=api, log=log.append)
    return 200, {"ok": ok, "error": "" if ok else "post failed (see server log)"}


def run_execution(cfg: ManagerConfig, session_id: str, *, get_token=None,
                  runner=None) -> dict:
    """Carry out the MANAGER half of a session's cached recommendation now -- a
    headless ``claude -p`` that can act. Reconstructs the session's Action (for the
    run dir) and uses the cached ``rec_manager``. Synchronous core of /api/execute.
    Outward-facing: the ui confirms first; still gated by ``execute_enabled`` (off
    -> shadow-logs without spawning)."""
    import subprocess
    from . import manager
    from .config import UsageLimitConfig
    from .usage_limit import monitor as _monitor
    get_token = get_token or _monitor.get_token
    runner = runner or subprocess.run

    ucfg = UsageLimitConfig.from_env()
    log: List[str] = []
    token = get_token(ucfg, log.append)
    if not token:
        return {"ok": False, "error": "could not read OAuth token from keychain"}
    actions = manager.scan_actions(cfg, ucfg, token, now=datetime.now(timezone.utc),
                                   consider_all=True, log=log.append)
    a = next((x for x in actions if x.session_id == session_id), None)
    if a is None:
        return {"ok": False, "error": "session not actionable right now"}
    cached = manager.latest_decision_by_sig(cfg).get(manager.action_sig(a)) or {}
    manager_rec = (cached.get("rec_manager") or "").strip()
    if not manager_rec:
        return {"ok": False, "error": "no MANAGER recommendation to execute -- Analyze first"}
    res = manager.execute_manager_rec(a, cfg=cfg, manager_rec=manager_rec,
                                      guidelines=manager.read_guidelines(cfg),
                                      log=log.append, runner=runner)
    return {"ok": bool(res.get("ok")), "ran": res.get("ran", False),
            "note": res.get("note", ""), "output": res.get("output", ""),
            "error": "" if res.get("ok") else (res.get("note") or "execution failed")}


def api_execute(cfg: ManagerConfig, payload: dict) -> Tuple[int, dict]:
    """Kick off an async run of a session's MANAGER recommendation (a `claude -p`
    that acts). The UI polls /api/stuck for the per-row exec status."""
    sid = (payload or {}).get("session_id")
    if not sid:
        return 400, {"error": "need {session_id}"}
    with _EXEC_LOCK:
        cur = _EXEC_JOBS.get(sid)
        if cur and cur.get("status") == "running":
            return 200, {"status": "running"}
        _EXEC_JOBS[sid] = {"status": "running", "msg": ""}

    def work():
        try:
            res = run_execution(cfg, sid)
            if not res.get("ok"):
                _set_exec(sid, "error", res.get("error", "execution failed"))
            elif res.get("ran"):
                _set_exec(sid, "done", res.get("note", "executed"))
            else:
                _set_exec(sid, "shadow", res.get("note", "execute disabled (shadow)"))
        except Exception as e:  # noqa: BLE001 (surface to UI, don't crash the server)
            _set_exec(sid, "error", str(e))

    threading.Thread(target=work, daemon=True).start()
    return 200, {"status": "started"}


# --------------------------------------------------------------------------- #
# the page (static; all data via the JSON API above)
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Manager review</title>
<style>
 :root{--bd:#d0d7de;--mut:#57606a;--bg:#f6f8fa;--sel:#ddf4ff;--selbd:#54aeff}
 *{box-sizing:border-box} html,body{height:100%}
 body{font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;color:#1f2328;
   display:flex;flex-direction:column}
 header{flex:0 0 auto;background:#fff;border-bottom:1px solid var(--bd);padding:10px 16px;
   display:flex;gap:10px;align-items:center} h1{font-size:15px;margin:0;font-weight:600} .sp{flex:1}
 button{font:inherit;border:1px solid var(--bd);background:#fff;border-radius:6px;
   padding:4px 10px;cursor:pointer} button:hover{background:var(--bg)}
 button.pri{background:#1f6feb;color:#fff;border-color:#1f6feb}
 button:disabled{opacity:.5;cursor:default} .muted{color:var(--mut)}
 code{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:var(--bg);
   padding:0 3px;border-radius:3px} pre{background:var(--bg);padding:6px;border-radius:6px;
   overflow:auto;margin:4px 0} pre code{padding:0;background:none}
 .mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
 .badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
 .answer{background:#fff1e5;color:#bc4c00} .review{background:#ddf4ff;color:#0969da}
 .rescue{background:#ffebe9;color:#cf222e} .defer{background:#f3f0ff;color:#6639ba}
 .skip{background:#eaeef2;color:#57606a}
 .fresh{display:inline-block;margin-left:4px;padding:0 6px;border-radius:10px;
   font-size:10px;font-weight:600;background:#fff8c5;color:#7d4e00}
 .stale{display:inline-block;padding:0 6px;border-radius:10px;
   font-size:10px;font-weight:600;background:#ffebe9;color:#cf222e}
 .dirty{color:#bc4c00;font-weight:600} .na{color:var(--mut);font-style:italic}
 .role{display:inline-block;font-size:10px;font-weight:600;color:#0969da;
   text-transform:uppercase;margin-right:3px} .rec{font-weight:600} .spin{color:#bc4c00;font-weight:600}
 .empty{color:var(--mut);padding:14px}
 textarea{width:100%;font:inherit;border:1px solid var(--bd);border-radius:6px;padding:6px;resize:vertical}
 /* MANAGER/SESSION recommendation split + manager-rec execution status */
 .recwrap{max-width:100%}.recwrap>div{padding:3px 0}
 .tgt{display:inline-block;font-size:10px;font-weight:700;border-radius:4px;
   padding:0 5px;margin-right:4px;vertical-align:1px}
 .tgt.mgr{background:#fff1e5;color:#bc4c00} .tgt.sess{background:#ddf4ff;color:#0969da}
 .exec{font-size:11px;margin-top:4px} .exec.done{color:#1a7f37;font-weight:600}
 .exec.shadow{color:var(--mut)} .exec.error{color:#cf222e} .exec.running{color:#bc4c00}
 /* outlook-style two panes: list | divider | detail */
 #split{flex:1;display:flex;min-height:0}
 #listpane{flex:0 0 auto;width:340px;min-width:180px;max-width:60vw;overflow:auto;
   border-right:1px solid var(--bd);background:#fbfcfd}
 #divider{flex:0 0 6px;cursor:col-resize;background:transparent} #divider:hover{background:#1f6feb66}
 #detailpane{flex:1;min-width:0;overflow:auto;padding:18px 22px;background:#fff}
 .listhead{position:sticky;top:0;z-index:1;background:#fbfcfd;border-bottom:1px solid var(--bd);
   font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);padding:9px 12px 6px}
 .item{padding:8px 12px;border-bottom:1px solid var(--bd);cursor:pointer}
 .item:hover{background:var(--bg)} .item.sel{background:var(--sel);box-shadow:inset 3px 0 0 var(--selbd)}
 .item-title{font-weight:600;margin:3px 0 1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .item-sub{font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .item-flags{margin-top:3px;font-size:10px;color:var(--mut)} .flag{margin-right:8px}
 .flag.ok{color:#1a7f37} .flag.run{color:#bc4c00;font-weight:600} .flag.stale{color:#cf222e;font-weight:600}
 .detbox{max-width:860px} .detail-head h2{font-size:18px;margin:5px 0 2px;line-height:1.25}
 .actbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:14px 0 4px;
   padding-bottom:12px;border-bottom:1px solid var(--bd)}
 .field{margin:16px 0} .field>h3{font-size:11px;text-transform:uppercase;letter-spacing:.04em;
   color:var(--mut);margin:0 0 6px;border-bottom:1px solid var(--bg);padding-bottom:3px}
 .qrow{margin:3px 0} .recent>div{padding:3px 0;border-top:1px solid var(--bg)}
 .fbhist{margin-bottom:8px} .fbhist>div{padding:3px 0;border-top:1px solid var(--bg)}
 textarea.fbnew{min-height:90px} .row-btns{display:flex;gap:6px;margin-top:6px}
 #gpanel{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:10;
   align-items:center;justify-content:center} #gbox{background:#fff;border-radius:10px;
   width:min(900px,92vw);height:80vh;display:flex;flex-direction:column;padding:14px}
 #gbox textarea{flex:1;font-family:ui-monospace,Menlo,monospace;font-size:12px;margin:8px 0}
 .gbar{display:flex;gap:8px;align-items:center}
</style></head><body>
<header><h1>Manager review</h1><span id="counts" class="muted"></span>
 <span id="at" class="muted"></span><span class="sp"></span>
 <label class="muted"><input type="checkbox" id="all"> all repos</label>
 <button id="analyzeAll">Analyze all</button>
 <button id="guidelines">Edit guidelines</button>
 <button id="refresh" class="pri">↻ Detect</button>
</header>
<div id="split">
 <aside id="listpane"><div id="list"><div class="empty">Detecting…</div></div></aside>
 <div id="divider" title="drag to resize"></div>
 <section id="detailpane"><div id="detail"><div class="empty">Select a thread on the left.</div></div></section>
</div>

<div id="gpanel"><div id="gbox">
  <div class="gbar"><b>Guidelines</b> <code id="gpath" class="muted"></code><span class="sp"></span>
    <button id="gsuggest">✨ Suggest from feedback</button>
    <button id="gsave" class="pri">Save</button><button id="gclose">Close</button></div>
  <textarea id="gtext" spellcheck="false"></textarea>
  <div class="muted">Injected into every analysis. "Suggest from feedback" drafts a revision
   from your saved feedback — review it here, then Save to accept.</div>
</div></div>

<script>
const esc=s=>(s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const badge=k=>`<span class="badge ${esc(k)}">${esc(k)}</span>`;
const shortTs=s=>esc((s||"").replace("T"," ").slice(0,16));
let polling=null;
let ROWS={};           // session_id -> latest row (so action buttons read rec text safely)
let selectedSid=null;  // the thread shown in the detail pane (outlook-style master/detail)
const fbDrafts={};     // sig -> in-progress feedback text (survives the 3s re-render)
// minimal, safe markdown -> html (escape first, then format)
function md(s){
  s=esc(s||""); const blocks=[];
  s=s.replace(/```([\s\S]*?)```/g,(m,c)=>{blocks.push(c.replace(/^\n/,""));return "[[CB:"+(blocks.length-1)+"]]";});
  s=s.replace(/`([^`]+)`/g,"<code>$1</code>");
  s=s.replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>");
  s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<em>$2</em>");
  s=s.replace(/^#{1,6}\s?(.*)$/gm,"<b>$1</b>");
  s=s.replace(/^\s*[-*]\s+(.*)$/gm,"• $1");
  s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/\n/g,"<br>");
  return s.replace(/\[\[CB:(\d+)\]\]/g,(m,i)=>"<pre><code>"+blocks[i]+"</code></pre>");
}

function qcell(r){
  const qs=r.questions||[];
  let h=`<div>${md(r.reason)}</div>`;
  if(qs.length) h+=qs.map(q=>`<div class="qrow muted"><b>${esc(q.header)}</b>: `
    +`${esc(q.question)} <i>[${(q.options||[]).map(esc).join(" · ")}]</i></div>`).join("");
  return h;
}
function analysisCell(r){
  if(r.job==="running") return `<span class="spin">analyzing…</span>`;
  if(r.has_analysis){
    const stale=r.analysis_stale
      ?`<span class="stale" title="the session changed after this analysis (updated `
        +`${shortTs(r.session_updated_at)}) — re-analyze for a current read">stale</span> `:"";
    return `<div class="ana">${md(r.analysis)}</div>`
      +`<div class="muted" style="margin-top:3px">${stale}analyzed ${shortTs(r.analyzed_at)}`
      +`${r.analysis_stale?` · session updated ${shortTs(r.session_updated_at)}`:""}</div>`;
  }
  if(r.note) return `<span class="na">⚠ ${esc(r.note)}</span>`;   // tried but produced nothing
  return `<span class="na">not analyzed</span>`;
}
function execNote(r){
  const e=r.exec||{}; if(!e.status) return "";
  if(e.status==="running") return `<div class="exec running spin">running manager rec…</div>`;
  const label={done:"executed ✓",shadow:"shadow — not run (set MANAGER_EXECUTE_ENABLED=1)",
    error:"execute failed"}[e.status]||e.status;
  return `<div class="exec ${esc(e.status)}">${esc(label)}${e.msg?" · "+esc(e.msg):""}</div>`;
}
function recCell(r){
  if(r.job==="running") return `<span class="spin">…</span>`;
  const m=r.rec_manager||"", s=r.rec_session||"";
  if(m||s){
    let h=`<div class="recwrap">`;
    if(m) h+=`<div><span class="tgt mgr">MANAGER</span><span class="rec">${md(m)}</span></div>`;
    if(s) h+=`<div><span class="tgt sess">SESSION</span><span class="rec">${md(s)}</span></div>`;
    return h+execNote(r)+`</div>`;
  }
  if(r.recommendation) return `<div class="recwrap"><span class="rec">${md(r.recommendation)}</span>`
    +execNote(r)+`</div>`;
  return `<span class="na">—</span>`;
}
function wtCell(r){
  const w=r.worktree||{};
  if(!w.exists) return `<span class="muted">${esc(r.repo||"")} · no local worktree</span>`;
  const d=w.dirty?` · <span class="dirty">${w.dirty} dirty</span>`:` · clean`;
  return `<span class="muted">${esc(r.repo||"")} · ${esc(w.branch||"?")}${d}</span>`;
}
// ---- master list (left pane) ----------------------------------------------
function itemHtml(r){
  const sel=r.session_id===selectedSid?" sel":"";
  const fresh=r.fresh?`<span class="fresh" title="within grace -- auto-loop waits">grace</span>`:"";
  const flags=[];
  if(r.job==="running") flags.push(`<span class="flag run">● analyzing…</span>`);
  else if(r.has_analysis) flags.push(r.analysis_stale
    ?`<span class="flag stale">⚠ analysis stale</span>`
    :`<span class="flag ok">✓ analyzed</span>`);
  else flags.push(`<span class="flag">not analyzed</span>`);
  const e=r.exec||{}; if(e.status==="running") flags.push(`<span class="flag run">▷ executing…</span>`);
  else if(e.status==="done") flags.push(`<span class="flag ok">▷ executed</span>`);
  const nfb=(r.feedback||[]).length; if(nfb) flags.push(`<span class="flag">💬 ${nfb}</span>`);
  return `<div class="item${sel}" onclick="select('${esc(r.session_id)}')">`
    +`<div>${badge(r.case)}${fresh}</div>`
    +`<div class="item-title">${esc(r.title||"(untitled)")}</div>`
    +`<div class="item-sub">${wtCell(r)}</div>`
    +`<div class="item-flags">${flags.join("")}</div></div>`;
}
function renderList(stuck,other){
  const sec=(label,rows)=> rows.length
    ? `<div class="listhead">${label} (${rows.length})</div>`+rows.map(itemHtml).join("")
    : `<div class="listhead">${label}</div><div class="empty">none</div>`;
  document.getElementById("list").innerHTML=sec("Actionable",stuck)+sec("Other",other);
}
function recentCell(r){
  const m=r.last_messages||[];
  if(!m.length) return `<span class="na">—</span>`;
  return `<div class="recent">`+m.map(x=>
    `<div><span class="role">${esc(x.role)}</span> ${md(x.text)}</div>`).join("")+`</div>`;
}
function fbCell(r){
  const items=r.feedback||[];
  const hist=items.length?`<div class="fbhist">`+items.map(e=>
    `<div>• ${md(e.text)}<span class="muted"> · ${shortTs(e.at)}</span></div>`).join("")+`</div>`:"";
  return hist
    +`<textarea class="fbnew" data-sig="${esc(r.sig)}" placeholder="add feedback…"></textarea>`
    +`<div class="row-btns"><button onclick="addFb(this)">Add note</button></div>`;
}
// ---- detail (right pane) --------------------------------------------------
function detailHtml(r){
  const running=r.job==="running";
  return `<div class="detbox">
    <div class="detail-head"><div>${badge(r.case)}`
    +`${r.fresh?`<span class="fresh">grace</span>`:""}</div>`
    +`<h2>${esc(r.title||"(untitled)")}</h2>`
    +`<div class="mono muted">${esc(r.session_id||"")}</div>`
    +`<div class="muted">${wtCell(r)}</div></div>
    <div class="actbar">`
    +`<button ${running?"disabled":""} onclick="analyze('${esc(r.session_id)}',false)">Analyze</button>`
    +`<button ${running?"disabled":""} onclick="analyze('${esc(r.session_id)}',true)">Re-analyze</button>`
    +`<button onclick="addNote()" title="add a feedback note (saved for the next analysis; not sent)">+ Add note</button>`
    +`<span class="sp"></span>`
    +`<button class="act" data-sid="${esc(r.session_id)}" onclick="sendSession(this)" `
    +`${r.rec_session?"":"disabled"} title="post the SESSION guidance into the live session">→ send to session</button>`
    +`<button class="act" data-sid="${esc(r.session_id)}" onclick="runManager(this)" `
    +`${r.rec_manager?"":"disabled"} title="execute the MANAGER rec via a headless claude -p">▷ run manager rec</button>`
    +`<button class="act" data-sid="${esc(r.session_id)}" onclick="sendFb(this)" `
    +`title="post your feedback into the live session">→ send feedback</button>`
    +`</div>
    <div class="field"><h3>Reason · question</h3>${qcell(r)}</div>
    <div class="field"><h3>Recommendation</h3>${recCell(r)}</div>
    <div class="field"><h3>Analysis</h3>${analysisCell(r)}</div>
    <div class="field"><h3>Recent activity</h3>${recentCell(r)}</div>
    <div class="field"><h3>Feedback</h3>${fbCell(r)}</div>
  </div>`;
}
function renderDetail(){
  const el=document.getElementById("detail");
  if(!selectedSid){ el.innerHTML=`<div class="empty">Select a thread on the left.</div>`; return; }
  const r=ROWS[selectedSid];
  if(!r){ el.innerHTML=`<div class="empty">That thread is no longer detected. Pick another.</div>`; return; }
  // preserve feedback focus/caret across re-renders (the 3s analysis/exec poll)
  const a=document.activeElement, wasFb=!!(a&&a.classList&&a.classList.contains("fbnew"));
  const ss=wasFb?a.selectionStart:null, se=wasFb?a.selectionEnd:null;
  el.innerHTML=detailHtml(r);
  const ta=el.querySelector("textarea.fbnew");
  if(ta){
    if(fbDrafts[r.sig]!==undefined) ta.value=fbDrafts[r.sig];
    ta.addEventListener("input",()=>{ fbDrafts[r.sig]=ta.value; });
    if(wasFb){ ta.focus(); if(ss!=null) ta.setSelectionRange(ss,se); }
  }
}
function select(sid){ selectedSid=sid; renderList(lastStuck,lastOther); renderDetail();
  document.getElementById("detailpane").scrollTop=0; }
let lastStuck=[], lastOther=[];
async function load(){
  const all=document.getElementById("all").checked?"?all=1":"";
  let d; try{ d=await (await fetch("/api/stuck"+all)).json(); }
  catch(e){ document.getElementById("list").innerHTML=`<div class="empty">load failed</div>`; return; }
  document.getElementById("at").textContent=d.at?("· "+d.at.replace("T"," ")):"";
  if(!d.ok){ document.getElementById("list").innerHTML=`<div class="empty">${esc(d.error||"scan failed")}</div>`;
    ROWS={}; renderDetail(); return; }
  document.getElementById("counts").textContent=
    `${d.counts.actionable} actionable · ${d.counts.analyzed} analyzed · ${d.counts.sessions} sessions`;
  ROWS={}; d.rows.forEach(r=>ROWS[r.session_id]=r);
  lastStuck=d.rows.filter(r=>r.actionable); lastOther=d.rows.filter(r=>!r.actionable);
  if(!selectedSid){ const f=lastStuck[0]||lastOther[0]; if(f) selectedSid=f.session_id; }  // auto-select first
  renderList(lastStuck,lastOther);
  renderDetail();
  const busy=d.rows.some(r=>r.job==="running"||(r.exec&&r.exec.status==="running"));
  if(busy && !polling){ polling=setInterval(load,3000); }
  if(!busy && polling){ clearInterval(polling); polling=null; }
}
// draggable divider between the two panes (list width persisted)
(function(){
  const dv=document.getElementById("divider"), lp=document.getElementById("listpane");
  const w0=parseInt(localStorage.getItem("mgr_listw")); if(w0>=180&&w0<=900) lp.style.width=w0+"px";
  dv.addEventListener("mousedown",e=>{
    e.preventDefault(); const x=e.pageX, w=lp.getBoundingClientRect().width;
    document.body.style.cursor="col-resize";
    const mv=ev=>{ lp.style.width=Math.min(900,Math.max(180,Math.round(w+ev.pageX-x)))+"px"; };
    const up=()=>{ document.removeEventListener("mousemove",mv); document.removeEventListener("mouseup",up);
      document.body.style.cursor=""; try{localStorage.setItem("mgr_listw",parseInt(lp.style.width));}catch(e){} };
    document.addEventListener("mousemove",mv); document.addEventListener("mouseup",up);
  });
})();
async function postSend(btn,sid,text){
  if(!confirm("Post this into "+sid+" as a live message?\n\n"+text)) return;
  const t=btn.textContent; btn.disabled=true; btn.textContent="sending…";
  try{
    const d=await (await fetch("/api/send",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({session_id:sid,text})})).json();
    btn.textContent=d.ok?"sent ✓":"failed"; if(!d.ok&&d.error) alert(d.error);
  }catch(e){ btn.textContent="failed"; }
  finally{ setTimeout(()=>{btn.textContent=t; btn.disabled=false;},1600); }
}
function sendSession(btn){
  const sid=btn.dataset.sid, text=((ROWS[sid]||{}).rec_session||"").trim();
  if(!text){ alert("no SESSION recommendation yet — Analyze first"); return; }
  postSend(btn,sid,"[manager review] suggested action: "+text);
}
function sendFb(btn){
  const ta=document.querySelector("#detail textarea.fbnew");
  const text=(ta?ta.value:"").trim();
  if(!text){ alert("no feedback to send"); return; }
  postSend(btn,btn.dataset.sid,"[operator note] "+text);
}
async function runManager(btn){
  const sid=btn.dataset.sid, mrec=((ROWS[sid]||{}).rec_manager||"").trim();
  if(!mrec){ alert("no MANAGER recommendation yet — Analyze first"); return; }
  if(!confirm("Execute this MANAGER action via a headless claude -p in "+sid+"?\n\n"+mrec
    +"\n\n(Needs MANAGER_EXECUTE_ENABLED=1; otherwise it only shadow-logs.)")) return;
  btn.disabled=true; btn.textContent="running…";
  try{
    await fetch("/api/execute",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({session_id:sid})});
    if(!polling) polling=setInterval(load,3000);
    load();
  }catch(e){ btn.textContent="failed";
    setTimeout(()=>{btn.textContent="▷ run manager rec"; btn.disabled=false;},1600); }
}
async function analyze(sid,force){
  await fetch("/api/analyze",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({session_id:sid,force})});
  if(!polling) polling=setInterval(load,3000);
  load();
}
async function analyzeAll(){
  const all=document.getElementById("all").checked?"?all=1":"";
  const d=await (await fetch("/api/stuck"+all)).json();
  for(const r of (d.rows||[]).filter(r=>r.actionable && !r.has_analysis && r.job!=="running"))
    fetch("/api/analyze",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({session_id:r.session_id,force:false})});
  if(!polling) polling=setInterval(load,3000);
  setTimeout(load,500);
}
async function addFb(btn){
  const ta=document.querySelector("#detail textarea.fbnew");
  const text=ta.value.trim(); if(!text){ alert("nothing to add"); return; }
  const r=await fetch("/api/feedback",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({sig:ta.dataset.sig,feedback:text})});
  if(r.ok){ delete fbDrafts[ta.dataset.sig]; ta.value=""; load(); }
  else { alert("save failed: "+(await r.text())); }
}
// top-of-detail shortcut: add a feedback note without scrolling to the field.
// uses whatever you've typed in the Feedback box, else prompts.
async function addNote(){
  const r=ROWS[selectedSid]; if(!r){ alert("no thread selected"); return; }
  const ta=document.querySelector("#detail textarea.fbnew");
  let text=(ta&&ta.value.trim())||"";
  if(!text) text=((window.prompt("Add a feedback note for this thread:")||"")).trim();
  if(!text) return;
  const resp=await fetch("/api/feedback",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({sig:r.sig,feedback:text})});
  if(resp.ok){ delete fbDrafts[r.sig]; if(ta) ta.value=""; load(); }
  else { alert("save failed: "+(await resp.text())); }
}
const gp=document.getElementById("gpanel");
document.getElementById("guidelines").onclick=async()=>{
  const d=await (await fetch("/api/guidelines")).json();
  document.getElementById("gtext").value=d.text||""; document.getElementById("gpath").textContent=d.path||"";
  gp.style.display="flex";
};
document.getElementById("gsuggest").onclick=async()=>{
  const b=document.getElementById("gsuggest"); const t=b.textContent;
  b.disabled=true; b.textContent="thinking… (up to ~2 min)";
  try{
    const r=await (await fetch("/api/guidelines/suggest",{method:"POST"})).json();
    if(r.ok){ document.getElementById("gtext").value=r.text; b.textContent="drafted — review & Save"; }
    else { alert(r.error||"suggest failed"); b.textContent=t; }
  }catch(e){ alert("suggest failed"); b.textContent=t; }
  finally{ b.disabled=false; setTimeout(()=>{b.textContent=t;},2500); }
};
document.getElementById("gclose").onclick=()=>gp.style.display="none";
gp.onclick=e=>{ if(e.target===gp) gp.style.display="none"; };
document.getElementById("gsave").onclick=async()=>{
  const b=document.getElementById("gsave"); b.disabled=true; b.textContent="Saving…";
  const r=await fetch("/api/guidelines",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({text:document.getElementById("gtext").value})});
  b.disabled=false; b.textContent=r.ok?"Saved ✓":"failed";
  setTimeout(()=>{b.textContent="Save"; if(r.ok) gp.style.display="none";},900);
};
document.getElementById("refresh").onclick=load;
document.getElementById("analyzeAll").onclick=analyzeAll;
document.getElementById("all").onchange=load;
load();
</script></body></html>"""


# --------------------------------------------------------------------------- #
# http server
# --------------------------------------------------------------------------- #
def make_handler(cfg: ManagerConfig):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/stuck":
                self._json(*api_stuck(cfg, consider_all=("all=1" in query)))
            elif path == "/api/guidelines":
                self._json(*api_guidelines_get(cfg))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/analyze":
                self._json(*api_analyze(cfg, self._body()))
            elif path == "/api/feedback":
                self._json(*api_feedback(cfg, self._body()))
            elif path == "/api/send":
                self._json(*api_send(cfg, self._body()))
            elif path == "/api/execute":
                self._json(*api_execute(cfg, self._body()))
            elif path == "/api/guidelines":
                self._json(*api_guidelines_post(cfg, self._body()))
            elif path == "/api/guidelines/suggest":
                self._json(*api_guidelines_suggest(cfg))
            else:
                self._json(404, {"error": "not found"})

    return Handler


def serve(cfg: ManagerConfig, *, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = False) -> int:
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    url = f"http://{host}:{port}/"
    print(f"manager-ui on {url}", flush=True)
    print(f"  decisions: {cfg.decisions_file}", flush=True)
    print(f"  guidelines: {cfg.guidelines_file}", flush=True)
    print("  Ctrl-C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        httpd.server_close()
    return 0


USAGE = (
    "usage: python3 -m remote_control manager-ui [--port N] [--host ADDR] [--open]\n"
    "  Local web UI to review the manager's analyses of stuck threads: it shows\n"
    "  every stuck thread + reason, runs a guideline-aware `claude -p` per thread\n"
    "  (recommendation + analysis, cached), takes feedback, and lets you edit the\n"
    "  guidelines doc. Detect/Analyze/Re-analyze from the page.\n"
    "  --port : port to bind (default 8765)\n"
    "  --host : address to bind (default 127.0.0.1, local only)\n"
    "  --open : open the page in a browser on start"
)


def main(argv: Optional[List[str]] = None) -> int:
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    host, port, open_browser = "127.0.0.1", 8765, False
    i = 0
    try:
        while i < len(argv):
            a = argv[i]
            if a == "--port":
                i += 1; port = int(argv[i])
            elif a == "--host":
                i += 1; host = argv[i]
            elif a == "--open":
                open_browser = True
            elif a in ("-h", "--help"):
                print(USAGE); return 0
            else:
                raise ValueError(f"unknown arg: {a}")
            i += 1
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    return serve(ManagerConfig.from_env(), host=host, port=port, open_browser=open_browser)

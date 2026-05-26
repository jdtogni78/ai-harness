"""Prefix Claude Code session titles with a per-repo nickname, so chats from the
same repo cluster together in the app's session list.

The code-sessions API has no groups/folders, and the session ``tags`` field is
read-only (PUT accepts it but silently ignores it). The session ``title`` IS
writable (``PUT /v1/code/sessions/{id}`` with ``{"title": ...}``), so a
``[NICK] `` title prefix is the only repo-grouping the platform allows. Auth and
the urllib client are reused from the usage-limit monitor (same keychain OAuth
token; see ``docs/usage-limit-monitor-v2.md``).

Repo derivation per session (first hit wins):
  1. ``config.sources[].url`` -> git repo basename (cloud / CLI-launched).
  2. a local bridge worktree ``~/dev/<repo>/.claude/worktrees/bridge-<id>``
     -> ``<repo>`` (bridge / app-launched sessions have empty ``config.sources``).
  3. a local transcript folder ``~/.claude/projects/-Users-<u>-dev-<repo>--claude-worktrees-bridge-cse-<id>``
     -> ``<repo>``. The transcript dir survives bridge-env deletion (which wipes
     (2)), so this catches sessions whose worktree was reaped but whose history
     is still on disk on the host that ran them.
  4. a ``logs/mm-<repo>.log`` tail mentioning ``session_<id>?from=cli``
     -> ``<repo>``. Catches desktop-app sessions launched directly in a checkout
     (no bridge worktree, transcript folder is the plain ``-Users-<u>-dev[-<repo>]``
     with ``<uuid>.jsonl`` files rather than ``<cse>.jsonl``), so (2) and (3)
     both miss them. The cse-id is the only thing claude's TUI prints on
     connect, and the supervisor pipes that stdout into ``mm-<repo>.log``.

The bracketed prefix is built from a **format template** (``SESSION_TITLE_FORMAT``
env, or a ``format=`` line in ``session-nicknames.txt``; default ``{nick}.{host}``)
whose ``{token}`` placeholders resolve from different sources per session:

  ``{nick}``    repo nickname (AO, CRC; from the nickname map)
  ``{repo}``    full repo basename (AppOne)
  ``{host}``    host nickname (``mini``/``note``) -- LOCAL bridge sessions only
  ``{branch}``  the worktree's git branch -- LOCAL bridge sessions only (1 git call)
  ``{id}``      full session id (``cse_01ABC...``)
  ``{shortid}`` compact id handle (``cse_`` stripped, first 8 chars)
  ``{engine}``  agent engine (``claude``)

A LOCAL bridge session (derived via (2)) physically runs on this host, so its
host/branch tokens are filled (``[AO.mini]``). Cloud / CLI sessions (derived via
(1)) live on no one host, so those tokens are empty and the default renders
host-less (``[AO]``). An empty token collapses together with one adjacent
separator, so ``{nick}.{host}`` is ``AO.mini`` locally and ``AO`` in the cloud.
Keeping the host token cloud-empty also stops two hosts' self-heal passes from
fighting over a shared cloud session (both render the same host-less prefix).

The pure helpers (no network, no clock, no filesystem) live above ``main`` so the
nickname map, template rendering, repo derivation, and rename planning unit-test
against plain dicts; ``{branch}`` resolution (the one git call) is injected.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from .config import DEV, LOGDIR, REPO, UsageLimitConfig, host_nickname
from .usage_limit import monitor

# Built-in repo -> nickname map (keys matched case-insensitively). The editable
# `session-nicknames.txt` at the repo root and the SESSION_TITLE_NICKNAMES env
# var override/extend these; see build_nickname_map.
DEFAULT_NICKNAMES: Dict[str, str] = {
    "app-one": "AO",
    "claude-remote-control": "CRC",
    "job-search": "JOB",
    "app-two": "A2",
    "dev": "DEV",
}

NICKNAMES_FILE = f"{REPO}/session-nicknames.txt"

# Where Claude Code stores per-session transcript dirs (one per bridge session).
# Folder names encode the original cwd with ``/`` ``.`` ``_`` collapsed to ``-``,
# so a bridge worktree at ``<dev>/<repo>/.claude/worktrees/bridge-cse_<sid>``
# becomes ``-Users-<u>-dev-<repo>--claude-worktrees-bridge-cse-<sid>``. The dir
# outlives the bridge env that originally created it, which is why it is the
# right repo source for sessions whose worktree dir has been deleted.
PROJECTS_DIR = "~/.claude/projects"

# The fixed suffix every encoded bridge-worktree project dir ends with, between
# the encoded ``<dev>/<repo>`` prefix and the session id. Built from the literal
# ``/<repo>/.claude/worktrees/bridge-cse_`` path tail with ``/`` ``.`` ``_`` -> ``-``.
_PROJECT_DIR_SUFFIX = "--claude-worktrees-bridge-cse-"

# The OSC-8 hyperlink the ``claude`` TUI prints to its stdout (captured into
# ``logs/mm-<repo>.log`` by the remote-control supervisor) when a desktop-app
# session connects. The path component is ``session_<id>``, *without* the
# ``cse_`` prefix the API uses, so the extractor re-adds it. This is the only
# on-disk artifact tying a desktop-app session id to its host+repo when the
# session was launched directly in a checkout (no bridge worktree, no
# ``--claude-worktrees-bridge-cse-`` transcript dir).
_SESSION_LINK_RE = re.compile(r"session_([A-Za-z0-9]+)\?from=cli")

# Tail bytes read from each ``mm-<repo>.log`` for the cse-id extractor. The logs
# grow unbounded (no rotation), and only the recent tail reflects *currently*
# connected sessions -- stale ids from sessions that have since moved to another
# host (or been archived) drop out naturally as the tail window slides past them.
MM_LOG_TAIL_BYTES = 64_000

# The prefix format template. Precedence (low->high): this default < a `format=`
# line in session-nicknames.txt < the SESSION_TITLE_FORMAT env var. See
# title_format and the module docstring for the available {tokens}.
DEFAULT_TITLE_FORMAT = "{nick}.{host}"

# A title prefix we own: a bracketed token (any chars but a closing `]`) at the
# very start, then whitespace. The format template is user-configurable, so this
# must strip ANY separator it could emit (``.`` ``/`` ``@`` ``:`` ...) or a re-run
# would stack instead of replace. The <=64 cap is the one guard that keeps us from
# eating a long bracketed sentence a human happens to start a title with.
_PREFIX_RE = re.compile(r"^\[[^\]]{1,64}\]\s+")

# Separators the title-format template can emit between tokens. Used to split an
# existing ``[NICK.HOST] ...`` prefix back into its segments so a self-heal pass
# can detect that another host's monitor already claimed the session and leave
# it alone (see existing_prefix_host).
_PREFIX_SEP_RE = re.compile(r"[.\s/@:]")


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
# ``format`` is a reserved key in session-nicknames.txt -- a ``format=`` line
# sets the title template (see title_format), not a nickname for a repo named
# "format" -- so the nickname parser skips it.
_RESERVED_NICK_KEYS = frozenset({"format"})


def parse_nickname_map(text: str) -> Dict[str, str]:
    """Parse ``repo=NICK`` lines (``#`` comments + blanks ignored) into a
    lowercased-key map. Last definition of a repo wins; the reserved ``format``
    key is skipped (it configures the title template instead)."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry or "=" not in entry:
            continue
        repo, _, nick = entry.partition("=")
        repo, nick = repo.strip().lower(), nick.strip()
        if repo and nick and repo not in _RESERVED_NICK_KEYS:
            out[repo] = nick
    return out


def build_nickname_map(file_text: str = "", env_value: str = "") -> Dict[str, str]:
    """Merge precedence (low -> high): built-ins < file < env. ``env_value`` is a
    comma-separated ``repo=NICK`` list."""
    nmap = dict(DEFAULT_NICKNAMES)
    nmap.update(parse_nickname_map(file_text))
    nmap.update(parse_nickname_map(env_value.replace(",", "\n")))
    return nmap


# --------------------------------------------------------------------------- #
# Title format template
# --------------------------------------------------------------------------- #
def parse_format_line(text: str) -> Optional[str]:
    """The template from a ``format=<template>`` line in session-nicknames.txt
    (last one wins), or None. ``#`` comments and blanks are ignored; the value is
    taken verbatim after ``=`` (templates can contain spaces / punctuation)."""
    found: Optional[str] = None
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        key, sep, val = entry.partition("=")
        if sep and key.strip().lower() == "format" and val.strip():
            found = val.strip()
    return found


def title_format(file_text: str = "", env_value: str = "") -> str:
    """The active prefix template. Precedence (high -> low): ``env_value``
    (SESSION_TITLE_FORMAT) > a ``format=`` line in *file_text* > DEFAULT."""
    if env_value.strip():
        return env_value.strip()
    return parse_format_line(file_text) or DEFAULT_TITLE_FORMAT


_TOKEN_RE = re.compile(r"\{(\w+)\}")


def render_template(template: str, values: Dict[str, str]) -> str:
    """Render ``{token}`` placeholders from *values*. An empty/missing token
    collapses together with ONE adjacent separator char (the literal binding it to
    a neighbor): it drops the trailing separator of the preceding literal, else a
    leading separator of the following literal. So ``{nick}.{host}`` is ``AO.mini``
    when host is set and ``AO`` when it's empty (no dangling dot). Unknown tokens
    count as empty; end whitespace is trimmed."""
    segs: List = []  # ('lit', text) | ('tok', value)
    pos = 0
    for m in _TOKEN_RE.finditer(template):
        if m.start() > pos:
            segs.append(("lit", template[pos:m.start()]))
        segs.append(("tok", str(values.get(m.group(1)) or "")))
        pos = m.end()
    if pos < len(template):
        segs.append(("lit", template[pos:]))

    out: List[str] = []
    for i, seg in enumerate(segs):
        if seg[0] == "lit":
            out.append(seg[1])
        elif seg[1]:                          # non-empty token
            out.append(seg[1])
        elif out and out[-1] and not out[-1][-1].isalnum():
            out[-1] = out[-1][:-1]            # drop a trailing separator already emitted
            if not out[-1]:
                out.pop()
        elif (i + 1 < len(segs) and segs[i + 1][0] == "lit"
              and segs[i + 1][1] and not segs[i + 1][1][0].isalnum()):
            segs[i + 1] = ("lit", segs[i + 1][1][1:])  # drop the following separator
    return "".join(out).strip()


def repo_basename_from_url(url: str) -> str:
    """``https://github.com/me/AppOne.git`` -> ``AppOne``."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _words(name: str) -> List[str]:
    """Split a repo name on non-alphanumerics and camelCase boundaries."""
    s = re.sub(r"[^0-9A-Za-z]+", " ", name)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    return [w for w in s.split() if w]


def derive_nickname(repo: str) -> str:
    """Fallback when a repo isn't in the map: initials for multi-word names
    (``claude-remote-control`` -> ``CRC``), else the first 3 chars
    (``app-two`` -> ``A2``)."""
    words = _words(repo)
    if not words:
        return (repo.upper()[:4]) or "?"
    if len(words) >= 2:
        return "".join(w[0] for w in words[:4]).upper()
    return words[0][:3].upper()


def nickname_for(repo: str, nmap: Dict[str, str]) -> str:
    return nmap.get(repo.lower()) or derive_nickname(repo)


def strip_prefix(title: str) -> str:
    """Drop a leading ``[token] `` prefix we previously added, if any."""
    return _PREFIX_RE.sub("", title or "", count=1)


def existing_prefix_host(title: str) -> Optional[str]:
    """The host segment of an existing ``[NICK.HOST] ...`` prefix, or None if
    the title has no prefix or its prefix has no host segment. ``[AO.mini] x``
    -> ``"mini"``; ``[AH] x`` -> ``None`` (no second segment, so no host claim);
    ``no prefix here`` -> ``None``.

    A self-heal pass uses this to leave already-claimed titles alone: when a
    session shows ``[AO.mini]`` and *this* host is ``note``, we don't want
    note's pass to overwrite mini's claim and start a ping-pong with mini's
    own pass. The host token is always the *last* segment because the default
    template (and the supported tokens) put ``{nick}`` first."""
    m = _PREFIX_RE.match(title or "")
    if not m:
        return None
    inner = m.group(0).strip()[1:-1].strip()  # strip "[" + "]"
    parts = [p for p in _PREFIX_SEP_RE.split(inner) if p]
    return parts[-1] if len(parts) >= 2 else None


def apply_prefix(title: str, nickname: str) -> str:
    """Re-prefix a title with ``[nickname] `` (idempotent: an existing prefix is
    replaced, not stacked)."""
    return f"[{nickname}] {strip_prefix(title)}".rstrip()


def is_active_session(session: dict) -> bool:
    """True if *session* is in the platform's ``active`` status (the only state
    that can be self-healed). The ``/sessions`` list endpoint returns archived
    sessions too — they outnumber live ones 10:1 — and re-titling them is wasted
    work + visual noise in the ``list`` plan."""
    return (session.get("status") or "active") == "active"


def is_host_local(session: dict, worktree_index: Dict[str, str]) -> bool:
    """True if *session* physically runs on THIS host: a bridge session
    (no authoritative git source URL) whose id is in this host's worktree index.
    A session with a ``config.sources[].url`` is a cloud / CLI session that lives
    on no particular host, so it is never host-local (-> no ``.host`` suffix)."""
    for src in (session.get("config") or {}).get("sources") or []:
        if src.get("url"):
            return False
    return session.get("id") in worktree_index


def short_session_id(sid: str) -> str:
    """A compact id handle for the ``{shortid}`` token: drop a ``cse_`` prefix,
    keep the first 8 chars (``cse_01XtFhCtBTr4...`` -> ``01XtFhCt``)."""
    s = sid[4:] if sid.startswith("cse_") else sid
    return s[:8]


def session_values(
    session: dict,
    repo: str,
    nmap: Dict[str, str],
    *,
    host: str = "",
    host_local: bool = False,
    branch: str = "",
) -> Dict[str, str]:
    """Token values for one session. Host/branch are filled by the caller only
    for host-local sessions; cloud sessions leave them empty so those tokens (and
    their separators) collapse out of the template."""
    sid = session.get("id") or ""
    return {
        "nick": nickname_for(repo, nmap),
        "repo": repo,
        "host": host if host_local else "",
        "branch": branch if host_local else "",
        "id": sid,
        "shortid": short_session_id(sid),
        "engine": str(session.get("engine") or "claude"),
    }


def render_prefix(template: str, values: Dict[str, str]) -> str:
    """The bracketed prefix token for a session: the *template* rendered against
    *values*, falling back to the bare ``{nick}`` if a template renders empty (so
    a repo-known session never gets an empty ``[]`` prefix)."""
    return render_template(template, values) or values.get("nick", "")


def git_branch(path) -> str:
    """The current branch at *path* (``""`` on detached HEAD / not a repo / error).
    The one filesystem touch behind the ``{branch}`` token; injected into the pure
    planners so they stay testable."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    b = r.stdout.strip()
    return b if r.returncode == 0 and b != "HEAD" else ""


def bridge_worktree_path(dev: str, repo: str, sid: str) -> Path:
    """Where a host-local bridge session's worktree lives -- the inverse of
    build_worktree_index's scan -- so ``{branch}`` can be resolved without
    threading the path through the index."""
    return Path(dev) / repo / ".claude" / "worktrees" / f"bridge-{sid}"


def repo_for_session(session: dict, worktree_index: Dict[str, str]) -> Optional[str]:
    """The repo a session belongs to, or None if undeterminable.

    ``config.sources[].url`` wins (authoritative); otherwise fall back to the
    local bridge-worktree index keyed by session id."""
    for src in (session.get("config") or {}).get("sources") or []:
        url = src.get("url")
        if url:
            return repo_basename_from_url(url)
    return worktree_index.get(session.get("id"))


def session_id_from_path(path: str) -> Optional[str]:
    """The bridge session id owning a path (e.g. this process's cwd), read from a
    ``.../worktrees/bridge-<id>`` component, or None if not inside one. A bridge
    session's API id IS its worktree basename minus ``bridge-``."""
    parts = Path(path).parts
    for i, name in enumerate(parts):
        if name.startswith("bridge-") and i and parts[i - 1] == "worktrees":
            return name[len("bridge-"):]
    return None


def repo_from_worktree_path(path: str) -> Optional[str]:
    """The repo owning a bridge-worktree path
    (``<repo>/.claude/worktrees/bridge-<id>``), or None. Mirrors the layout
    build_worktree_index scans, but for one explicit path."""
    parts = Path(path).parts
    for i, name in enumerate(parts):
        if (name.startswith("bridge-") and i >= 3
                and parts[i - 1] == "worktrees" and parts[i - 2] == ".claude"):
            return parts[i - 3]
    return None


class Rename(NamedTuple):
    id: str
    repo: Optional[str]
    nickname: Optional[str]
    old_title: str
    new_title: str

    @property
    def changed(self) -> bool:
        return self.new_title != self.old_title and self.nickname is not None


def plan_renames(
    sessions: List[dict],
    worktree_index: Dict[str, str],
    nmap: Dict[str, str],
    host: str = "",
    template: str = DEFAULT_TITLE_FORMAT,
    branch_for: Optional[Callable[[str, str], str]] = None,
) -> List[Rename]:
    """One Rename per session: derive repo -> token values -> rendered prefix ->
    prefixed title. A session whose repo can't be derived is returned unchanged
    (nickname None). Host-local (bridge) sessions fill the ``{host}``/``{branch}``
    tokens (``[AO.mini]``); cloud sessions leave them empty (``[AO]``).
    *branch_for* ``(sid, repo) -> branch`` is consulted only for host-local
    sessions and only when the template uses ``{branch}``, so non-branch templates
    make no git calls. ``Rename.nickname`` holds the rendered prefix token."""
    want_branch = branch_for is not None and "{branch}" in template
    plan: List[Rename] = []
    for s in sessions:
        sid = s.get("id") or ""
        old = s.get("title") or ""
        repo = repo_for_session(s, worktree_index)
        if repo is None:
            plan.append(Rename(sid, None, None, old, old))
            continue
        # Don't overwrite another host's claim: if the title already carries a
        # ``[NICK.HOST]`` prefix whose HOST is some other host, leave it alone.
        # Otherwise two hosts' self-heal passes would ping-pong the suffix
        # whenever the same session is visible to both (e.g. mini sees its own
        # bridge -> writes ``.mini``; note sees the same sid in its mm-log tail
        # -> would overwrite to ``.note``). A non-claim title (``[AH]`` or no
        # prefix) is still re-prefixed, since adding a fresh claim is fine.
        claimed_by = existing_prefix_host(old)
        if claimed_by and claimed_by != host:
            plan.append(Rename(sid, repo, None, old, old))
            continue
        local = is_host_local(s, worktree_index)
        branch = branch_for(sid, repo) if (local and want_branch) else ""
        vals = session_values(s, repo, nmap, host=host, host_local=local, branch=branch)
        token = render_prefix(template, vals)
        plan.append(Rename(sid, repo, token, old, apply_prefix(old, token)))
    return plan


def fs_branch_resolver(dev: str) -> Callable[[str, str], str]:
    """A ``(sid, repo) -> branch`` resolver that reads the git branch of the
    session's local bridge worktree under *dev*. The I/O backing for ``{branch}``;
    pass it to plan_renames / apply_prefixes when the template uses that token."""
    return lambda sid, repo: git_branch(bridge_worktree_path(dev, repo, sid))


# --------------------------------------------------------------------------- #
# I/O
# --------------------------------------------------------------------------- #
DEFAULT_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
_SESSION_ID_ARG_RE = re.compile(r"--session-id\s+(cse_[A-Za-z0-9]+)")
_JWT_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


def _jwt_payload(jwt: str) -> Optional[dict]:
    """Decode a JWT's middle (payload) segment to a JSON dict, or None on any
    parse failure. Token signature is NOT verified -- we only read its
    self-declared ``session_id`` (the same value we'd get back from the server
    on the next API call), so verification gives us nothing."""
    import base64
    try:
        payload = jwt.split(".")[1]
    except IndexError:
        return None
    pad = "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload + pad))
    except (ValueError, OSError):
        return None


def parse_cmd_session_id(cmd: str) -> Optional[str]:
    """Pull the ``cse_*`` id for a live process from its argv+env blob (the
    output of ``ps eww``), or None. Two sources, in order:

      1. ``--session-id cse_...`` argv slice -- the desktop app passes this
         when spawning its ``--print --sdk-url ...`` CLI worker.
      2. ``CLAUDE_CODE_SESSION_ACCESS_TOKEN=<JWT>`` env var -- the desktop
         app's own process (and any subprocess that inherits its env) carries
         the cse_id only inside this JWT's payload (``session_id`` claim).

    The cmdline-or-env blob is the authoritative cse_id <-> pid binding:
    the local registry (``~/.claude/sessions/<pid>.json``) only records the
    local uuid, not the cloud cse_id, so we have to read the live process to
    bridge the two."""
    text = cmd or ""
    m = _SESSION_ID_ARG_RE.search(text)
    if m:
        return m.group(1)
    for jwt in _JWT_RE.findall(text):
        payload = _jwt_payload(jwt)
        sid = (payload or {}).get("session_id") if payload else None
        if isinstance(sid, str) and sid.startswith("cse_"):
            return sid
    return None


def repo_from_cwd(cwd: str, dev: str) -> Optional[str]:
    """The repo basename a cwd belongs to (``<dev>/<repo>`` or any subdir of
    it), or None if not under *dev*. Catches the layouts the bridge-worktree
    scan misses -- a session launched directly in a repo's main checkout
    (``<dev>/<repo>``) -- so it too can be tagged host-local."""
    try:
        rel = Path(cwd).relative_to(Path(dev))
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def _ps_cmd(pid: int) -> str:
    """``ps eww -p PID -o command=`` -- the cmdline AND the env, space-joined
    (the form parse_cmd_session_id reads to find either ``--session-id`` argv
    OR a JWT-carrying ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` env var). macOS
    ``ps -E`` requires the BSD ``e`` flag without a dash; ``ww`` lifts the
    width cap so a long env doesn't get truncated. Injected so tests stay pure."""
    try:
        r = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _read_session_files(sessions_dir: Path) -> List[dict]:
    """Parse ``<sessions_dir>/*.json`` (the local pid+cwd registry the desktop
    app writes one-per-process). Silently drop unreadable/unparseable files."""
    out: List[dict] = []
    try:
        files = list(Path(sessions_dir).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return out


def live_session_entries(
    sessions_dir: Path,
    dev: str,
    *,
    cmd_for: Optional[Callable[[int], str]] = None,
    read_records: Optional[Callable[[Path], List[dict]]] = None,
) -> Dict[str, str]:
    """Map ``cse_id -> repo`` for currently-running desktop-app sessions on
    this host. Walks ``<sessions_dir>/<pid>.json`` (each holds pid+cwd) and
    reads each live process's ``--session-id cse_*`` arg to bridge the local
    uuid <-> cse_id gap that the registry doesn't record itself. *cmd_for*
    and *read_records* are injected for tests; defaults are a ``ps`` shell-out
    and a JSON glob."""
    cmd_for = cmd_for or _ps_cmd
    read_records = read_records or _read_session_files
    out: Dict[str, str] = {}
    for d in read_records(sessions_dir):
        pid, cwd = d.get("pid"), d.get("cwd")
        if not (isinstance(pid, int) and isinstance(cwd, str)):
            continue
        cse = parse_cmd_session_id(cmd_for(pid))
        if not cse:
            continue
        repo = repo_from_cwd(cwd, dev)
        if repo:
            out.setdefault(cse, repo)
    return out


def build_worktree_index(
    dev_root: Path,
    *,
    sessions_dir: Optional[Path] = None,
    cmd_for: Optional[Callable[[int], str]] = None,
    read_records: Optional[Callable[[Path], List[dict]]] = None,
) -> Dict[str, str]:
    """Map session id -> repo for sessions physically running on this host.
    Two sources:

      1. Bridge worktrees: ``<dev>/<repo>/.claude/worktrees/bridge-<sid>``.
         The historical, fork/resume-friendly layout.
      2. Live desktop-app sessions (``<sessions_dir>``): pid+cwd from the
         local registry joined with the live process's ``--session-id cse_*``.
         Catches sessions launched directly in a repo's main checkout
         (``<dev>/<repo>``) -- those have no ``bridge-<sid>`` dir on disk.

    Bridge entries win when both sources name the same sid (a bridge layout
    is the authoritative one for a fork/resume). *sessions_dir* defaults to
    ``~/.claude/sessions``; pass ``Path('/dev/null')`` (or any nonexistent
    path) to disable the live-session source."""
    index: Dict[str, str] = {}
    sd = DEFAULT_SESSIONS_DIR if sessions_dir is None else sessions_dir
    for sid, repo in live_session_entries(
            sd, str(dev_root), cmd_for=cmd_for, read_records=read_records).items():
        index[sid] = repo
    for wt in Path(dev_root).glob("*/.claude/worktrees/bridge-*"):
        sid = wt.name[len("bridge-"):]
        repo = wt.parents[2].name  # <repo>/.claude/worktrees/<wt>
        index[sid] = repo  # bridge layout wins over a live-session entry
    return index


def encode_dev_prefix(dev_root: str) -> str:
    """The ``-Users-<u>-dev-`` prefix every transcript-dir name carries -- the
    encoded form of *dev_root* (``/`` and ``.`` collapsed to ``-``) plus a
    trailing ``-`` so a startswith() check binds to a full path segment, not a
    leading substring of a similarly-named repo."""
    return dev_root.replace("/", "-").replace(".", "-") + "-"


def repo_sid_from_project_dirname(name: str, dev_prefix: str) -> Optional[Tuple[str, str]]:
    """Parse a ``~/.claude/projects/`` dir name like
    ``-Users-user-dev-<repo>--claude-worktrees-bridge-cse-<sid>`` into
    ``(repo, "cse_" + sid)``, or None if the name isn't a bridge-worktree dir
    or its dev prefix doesn't match *dev_prefix*. The repo segment is taken
    verbatim, so encodings safe under ``/.._`` -> ``-`` (``ai-harness``,
    ``app-two-docker``, ``claude-remote-control``) round-trip; a repo whose
    own name contains ``.`` or ``_`` would be ambiguous here and is unsupported
    on purpose -- the worktree-index source still covers it."""
    idx = name.find(_PROJECT_DIR_SUFFIX)
    if idx < 0:
        return None
    prefix, sid_raw = name[:idx], name[idx + len(_PROJECT_DIR_SUFFIX):]
    if not prefix.startswith(dev_prefix) or not sid_raw:
        return None
    repo = prefix[len(dev_prefix):]
    return (repo, f"cse_{sid_raw}") if repo else None


def build_projects_index(projects_root: Path, dev_root: str) -> Dict[str, str]:
    """Map session id -> repo by scanning Claude Code's per-session transcript
    dirs under *projects_root* (default ``~/.claude/projects``). Unlike the
    worktree-index source, this one survives a deleted bridge environment, so it
    catches the long tail of disconnected sessions whose worktree dir was reaped
    but whose history is still on disk on the host that ran them."""
    index: Dict[str, str] = {}
    dev_prefix = encode_dev_prefix(dev_root)
    try:
        entries = list(Path(projects_root).iterdir())
    except OSError:
        return index
    for d in entries:
        if not d.is_dir():
            continue
        parsed = repo_sid_from_project_dirname(d.name, dev_prefix)
        if parsed:
            repo, sid = parsed
            index.setdefault(sid, repo)
    return index


def build_mm_log_index(logdir: Path) -> Dict[str, str]:
    """Map session id -> repo by scanning ``<logdir>/mm-<repo>.log`` tails for
    the OSC-8 hyperlink the ``claude`` TUI prints when a desktop-app session
    connects via the remote-control server. Each ``mm-<repo>`` server only ever
    serves sessions whose cwd is under that repo, so the repo is just the file's
    ``mm-...log`` basename -- no per-line parsing needed.

    Closes the gap left by worktree+projects indexes: a session launched
    directly in a repo checkout (no bridge worktree, transcript dir is the
    plain ``-Users-<u>-dev[-<repo>]`` form with no ``--claude-worktrees-bridge-
    cse-`` suffix and ``<uuid>.jsonl`` files rather than ``<cse>.jsonl``) leaves
    nothing on disk linking its local uuid to its API ``cse_<id>``. The
    remote-control server's stdout *does* print that ``cse_<id>`` once on
    connect, and the supervisor pipes that stdout to ``mm-<repo>.log``."""
    index: Dict[str, str] = {}
    try:
        files = list(Path(logdir).glob("mm-*.log"))
    except OSError:
        return index
    for f in files:
        repo = f.name[len("mm-"):-len(".log")]
        if not repo:
            continue
        try:
            with f.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - MM_LOG_TAIL_BYTES))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for m in _SESSION_LINK_RE.finditer(tail):
            index.setdefault(f"cse_{m.group(1)}", repo)
    return index


def merged_repo_index(
    dev_root: str,
    projects_root: Optional[str] = None,
    logdir: Optional[str] = None,
) -> Dict[str, str]:
    """Union of three host-local sources, in precedence order:

      1. ``build_worktree_index`` -- live bridge worktrees (authoritative:
         current on-disk state).
      2. ``build_projects_index`` -- surviving bridge-cse transcript dirs (still
         reliably name a repo even after the worktree env is reaped).
      3. ``build_mm_log_index`` -- cse-ids harvested from remote-control server
         logs (catches desktop-app sessions launched directly in a checkout,
         which leave no bridge artifacts).

    Worktree wins because a stale project dir may name a worktree that has
    since been moved to a different repo (rare but possible); the mm-log
    source is last because it is the loosest signal (a session that has
    migrated hosts may still appear in the *old* host's log tail until the
    window slides past)."""
    idx = build_worktree_index(Path(dev_root))
    if projects_root:
        for sid, repo in build_projects_index(Path(projects_root), dev_root).items():
            idx.setdefault(sid, repo)
    if logdir:
        for sid, repo in build_mm_log_index(Path(logdir)).items():
            idx.setdefault(sid, repo)
    return idx


def set_title(cfg: UsageLimitConfig, token: str, sid: str, title: str):
    return monitor.api_request(cfg, "PUT", f"/sessions/{sid}", token, {"title": title})


def _load_nickname_text(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def apply_prefixes(
    cfg: UsageLimitConfig,
    token: str,
    log,
    *,
    dev: str = DEV,
    file: str = NICKNAMES_FILE,
    map_value: str = "",
    only: Optional[str] = None,
    host: Optional[str] = None,
    projects: str = PROJECTS_DIR,
) -> Tuple[int, int]:
    """Headless re-apply of ``[NICK]`` title prefixes for every session whose repo
    is derivable. The programmatic twin of ``main``'s ``apply`` path (no stdout) so
    a daemon (the usage-limit monitor) can self-heal prefixes that the platform's
    auto-titling overwrites mid-session. Returns ``(renamed_ok, failed)``; sessions
    already correct or with an undeterminable repo are skipped. Host-local bridge
    sessions render their ``{host}``/``{branch}`` tokens for *host* (default: this
    machine); see title_format for the template."""
    sessions = monitor.list_sessions(cfg, token, log)
    if sessions is None:
        return (0, 0)
    sessions = [s for s in sessions if is_active_session(s)]
    file_text = _load_nickname_text(file)
    nmap = build_nickname_map(file_text,
                              map_value or os.environ.get("SESSION_TITLE_NICKNAMES", ""))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    index = merged_repo_index(dev, str(Path(projects).expanduser()),
                              logdir=(str(cfg.logdir) if cfg is not None else None))
    plan = plan_renames(sessions, index, nmap, host or host_nickname(),
                        template, fs_branch_resolver(dev))
    if only:
        want = only.lower()
        plan = [r for r in plan if (r.repo or "").lower() == want]
    ok = fail = 0
    for r in (r for r in plan if r.changed):
        code, _ = set_title(cfg, token, r.id, r.new_title)
        if code == 200:
            ok += 1
        else:
            fail += 1
    return (ok, fail)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
USAGE = (
    "usage: python3 -m remote_control titles [list|apply] [--dev DIR] "
    "[--projects-dir DIR] [--nicknames-file PATH] [--map repo=NICK,...] "
    "[--only REPO] [--all]\n"
    "       python3 -m remote_control titles set [--self|--id CSE_ID] \"<description>\"\n"
    "  list  (default): show the planned [NICK] title prefixes (no writes)\n"
    "  apply         : PUT the changed titles\n"
    "  --all         : include archived sessions (default: active only)\n"
    "  set           : set ONE session's title to '[NICK] <description>'\n"
    "                  --self (default): derive id + repo from the current\n"
    "                  bridge worktree; --id targets another session\n"
    "  --projects-dir DIR : Claude Code transcript root (default ~/.claude/\n"
    "    projects). Repo is derived from a session's transcript-dir name when\n"
    "    its bridge worktree has been deleted but the transcript folder remains.\n"
    "  --logdir DIR : remote-control logs dir (default ai-harness/logs). Used\n"
    "    to harvest cse-ids from mm-<repo>.log for desktop-app sessions that\n"
    "    have no bridge worktree (e.g. launched directly in ~/dev).\n"
    "  --map / SESSION_TITLE_NICKNAMES env extend the repo->nickname map\n"
    "  SESSION_TITLE_FORMAT env (or a `format=` line in the nicknames file)\n"
    "    sets the prefix template; default {nick}.{host} -> [AO.mini] / [AO].\n"
    "    tokens: {nick} {repo} {host} {branch} {id} {shortid} {engine}"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"cmd": "list", "dev": DEV, "file": NICKNAMES_FILE,
            "map": "", "only": None, "self": False, "id": None, "desc": "",
            "projects": PROJECTS_DIR, "logdir": LOGDIR, "all": False}
    desc: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("list", "apply", "set"):
            opts["cmd"] = a
        elif a == "--dev":
            i += 1; opts["dev"] = argv[i]
        elif a == "--projects-dir":
            i += 1; opts["projects"] = argv[i]
        elif a == "--logdir":
            i += 1; opts["logdir"] = argv[i]
        elif a == "--nicknames-file":
            i += 1; opts["file"] = argv[i]
        elif a == "--map":
            i += 1; opts["map"] = argv[i]
        elif a == "--only":
            i += 1; opts["only"] = argv[i]
        elif a == "--all":
            opts["all"] = True
        elif a == "--self":
            opts["self"] = True
        elif a == "--id":
            i += 1; opts["id"] = argv[i]
        elif a in ("-h", "--help"):
            opts["cmd"] = "help"
        elif not a.startswith("-"):
            desc.append(a)  # positional words form the `set` <description>
        else:
            raise ValueError(f"unknown arg: {a}")
        i += 1
    opts["desc"] = " ".join(desc)
    return opts


def _run_set(cfg: UsageLimitConfig, token: str, opts: dict, log) -> int:
    """`titles set`: set ONE session's title to ``[NICK] <description>``.

    Resolve the repo (hence the nickname prefix) highest-confidence source first:
    1. the current bridge-worktree cwd (``--self``), 2. the local worktree index
    (``--id`` of a local bridge session), 3. the session's own git source URL via
    the API (``repo_for_session``) -- the authoritative fallback that recovers the
    repo for cloud sessions and worktree layouts the cwd parser misses (e.g. a
    sandbox path without the ``.claude/`` segment, where ``session_id_from_path``
    still yields an id so the rename fires but ``repo_from_worktree_path`` returns
    None). Only when all three miss is the title set without a prefix -- and we say
    so rather than dropping it silently. Used by start-work/close-work to reflect
    the current ticket in this chat's title."""
    desc = (opts["desc"] or "").strip()
    if not desc:
        log("set requires a <description>")
        return 2
    index = merged_repo_index(opts["dev"], str(Path(opts["projects"]).expanduser()),
                              logdir=opts.get("logdir"))
    if opts["id"]:
        sid = opts["id"]
        repo = index.get(sid)
        host_local = sid in index  # a known local bridge worktree
    else:
        cwd = os.getcwd()
        sid = session_id_from_path(cwd)
        if not sid:
            log("--self: not inside a bridge worktree; pass --id CSE_ID")
            return 2
        repo = repo_from_worktree_path(cwd)
        host_local = True  # --self: we ARE running inside it, on this host
    if repo is None:  # authoritative fallback: the session's own git source URL
        s = next((x for x in (monitor.list_sessions(cfg, token, log) or [])
                  if x.get("id") == sid), None)
        repo = repo_for_session(s, index) if s else None
        if not host_local:  # don't downgrade a confirmed --self
            host_local = is_host_local(s, index) if s else False
    file_text = _load_nickname_text(opts["file"])
    nmap = build_nickname_map(file_text,
                              os.environ.get("SESSION_TITLE_NICKNAMES", opts["map"]))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    if repo is None:
        log(f"warning: could not determine repo for {sid}; "
            f"setting title without a [NICK] prefix")
    if repo:
        branch = ""
        if host_local and "{branch}" in template:
            wt = cwd if not opts["id"] else str(bridge_worktree_path(opts["dev"], repo, sid))
            branch = git_branch(wt)
        vals = session_values({"id": sid}, repo, nmap, host=host_nickname(),
                              host_local=host_local, branch=branch)
        title = apply_prefix(desc, render_prefix(template, vals))
    else:
        title = desc
    code, body = set_title(cfg, token, sid, title)
    if code == 200:
        print(f"set {sid} -> {title!r}")
        return 0
    log(f"FAILED {sid} http={code} body={str(body)[:160]}")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        opts = _parse_args(argv)
    except (ValueError, IndexError) as e:
        print(f"{e}\n{USAGE}", file=sys.stderr)
        return 2
    if opts["cmd"] == "help":
        print(USAGE)
        return 0

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731  (API client diagnostics)
    cfg = UsageLimitConfig.from_env()
    token = monitor.get_token(cfg, log)
    if not token:
        log("could not read OAuth token from keychain")
        return 1
    if opts["cmd"] == "set":
        return _run_set(cfg, token, opts, log)
    sessions = monitor.list_sessions(cfg, token, log)
    if sessions is None:
        return 1
    if not opts["all"]:
        sessions = [s for s in sessions if is_active_session(s)]

    file_text = _load_nickname_text(opts["file"])
    nmap = build_nickname_map(file_text,
                              os.environ.get("SESSION_TITLE_NICKNAMES", opts["map"]))
    if opts["map"]:
        nmap.update(parse_nickname_map(opts["map"].replace(",", "\n")))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    index = merged_repo_index(opts["dev"], str(Path(opts["projects"]).expanduser()),
                              logdir=opts.get("logdir"))
    plan = plan_renames(sessions, index, nmap, host_nickname(),
                        template, fs_branch_resolver(opts["dev"]))
    if opts["only"]:
        want = opts["only"].lower()
        plan = [r for r in plan if (r.repo or "").lower() == want]

    changed = [r for r in plan if r.changed]
    unknown = [r for r in plan if r.repo is None]
    for r in plan:
        mark = "~" if r.changed else ("?" if r.repo is None else "=")
        repo = r.repo or "<unknown repo>"
        print(f"{mark} {r.id}  [{repo}]")
        print(f"    {r.old_title!r}")
        if r.changed:
            print(f" -> {r.new_title!r}")
    print(f"\n{len(plan)} sessions; {len(changed)} to rename; "
          f"{len(unknown)} with undeterminable repo.")

    if opts["cmd"] != "apply":
        if changed:
            print("(dry run -- re-run with `apply` to write these titles)")
        return 0

    ok = fail = 0
    for r in changed:
        code, body = set_title(cfg, token, r.id, r.new_title)
        if code == 200:
            ok += 1
            print(f"renamed {r.id} -> {r.new_title!r}")
        else:
            fail += 1
            print(f"FAILED {r.id} http={code} body={str(body)[:120]}", file=sys.stderr)
    print(f"\napplied: {ok} ok, {fail} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

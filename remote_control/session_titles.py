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
  4. a ``logs/<host>-<repo>.log`` tail mentioning ``session_<id>?from=cli``
     -> ``<repo>``. Catches desktop-app sessions launched directly in a checkout
     (no bridge worktree, transcript folder is the plain ``-Users-<u>-dev[-<repo>]``
     with ``<uuid>.jsonl`` files rather than ``<cse>.jsonl``), so (2) and (3)
     both miss them. The cse-id is the only thing claude's TUI prints on
     connect, and the supervisor pipes that stdout into ``<host>-<repo>.log``.

The bracketed prefix is built from a **format template** (``SESSION_TITLE_FORMAT``
env, or a ``format=`` line in ``session-nicknames.txt``; default ``{nick}.{host}``)
whose ``{token}`` placeholders resolve from different sources per session:

  ``{nick}``    repo nickname (AO, CRC; from the nickname map)
  ``{repo}``    full repo basename (AppOne)
  ``{host}``    host nickname -- LOCAL bridge sessions only
  ``{branch}``  the worktree's git branch -- LOCAL bridge sessions only (1 git call)
  ``{id}``      full session id (``cse_01ABC...``)
  ``{shortid}`` compact id handle (``cse_`` stripped, first 8 chars)
  ``{engine}``  agent engine (``claude``)

A session whose id appears in this host's local worktree index (sources (2)-(4))
physically runs on this host, so its host/branch tokens are filled (``[AO.mini]``)
-- this holds even when the session also has a ``config.sources[].url`` (a sandbox
session forked to a local bridge dir here still runs HERE). A session with ONLY a
``config.sources[].url`` and no local bridge artifacts is cloud-only -- those
tokens stay empty and the default renders host-less (``[AO]``). An empty token
collapses together with one adjacent separator, so ``{nick}.{host}`` is ``AO.mini``
when host is set and ``AO`` when it's not. Two hosts that happen to BOTH have a
local bridge for the same shared sid are kept from ping-ponging the suffix by
``existing_prefix_host`` in plan_renames (whichever host writes first wins, and
the other's pass leaves the claim alone on the next cycle).

The pure helpers (no network, no clock, no filesystem) live above ``main`` so the
nickname map, template rendering, repo derivation, and rename planning unit-test
against plain dicts; ``{branch}`` resolution (the one git call) is injected.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import (Callable, Dict, Iterable, List, NamedTuple, Optional,
                    Tuple)

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
# ``logs/<host>-<repo>.log`` by the remote-control supervisor) when a desktop-app
# session connects. The path component is ``session_<id>``, *without* the
# ``cse_`` prefix the API uses, so the extractor re-adds it. This is the only
# on-disk artifact tying a desktop-app session id to its host+repo when the
# session was launched directly in a checkout (no bridge worktree, no
# ``--claude-worktrees-bridge-cse-`` transcript dir).
_SESSION_LINK_RE = re.compile(r"session_([A-Za-z0-9]+)\?from=cli")

# Tail bytes read from each ``<host>-<repo>.log`` for the cse-id extractor. The logs
# grow unbounded (no rotation), and only the recent tail reflects *currently*
# connected sessions -- stale ids from sessions that have since moved to another
# host (or been archived) drop out naturally as the tail window slides past them.
MM_LOG_TAIL_BYTES = 64_000

# The prefix format template. Precedence (low->high): this default < a `format=`
# line in session-nicknames.txt < the SESSION_TITLE_FORMAT env var. See
# title_format and the module docstring for the available {tokens}.
DEFAULT_TITLE_FORMAT = "{nick}.{host}"

# A title prefix we own: ONE OR MORE bracketed tokens (any chars but a closing
# `]`) at the very start, separated by OPTIONAL whitespace, followed by
# whitespace. The format template is user-configurable, so this must strip ANY
# separator it could emit (``.`` ``/`` ``@`` ``:`` ...) or a re-run would stack
# instead of replace. The chained form (``[NICK.host][SUB] desc``) is how
# new-session tags its spawned subsessions with an extra ``[SUB]`` bracket
# while still letting the watcher own the outer ``[NICK.host]``;
# extract_sub_token reads the last leading bracket back so plan_renames can
# preserve it across passes.
#
# The optional inter-bracket whitespace (``\s*``) and the trailing
# ``(?<=\s)`` lookbehind together let the watcher self-heal the
# duplicated-prefix form (``[A] [A][B] body``) that accumulates when the
# platform's auto-titler races our PUT: the watcher's own apply_prefix
# stacks ``[NICK] `` on top of an unstripped ``[NICK][SUB]`` carry-over.
# Before this tolerance, strip_prefix consumed only the first ``[A] ``,
# extract_sub_token saw no adjacent bracket pair, and apply_prefix was
# idempotent on the dup -- so the watcher considered the title "correct"
# and never PUT a fix.
#
# The <=64 cap (per bracket) is the one guard that keeps us from eating
# a long bracketed sentence a human happens to start a title with. The
# ``(?<=\s)`` lookbehind keeps us from stripping ``[A]body`` (no
# separator between prefix and content); without it, ``\s*`` would
# happily match zero whitespace and consume the bracket.
_PREFIX_RE = re.compile(r"^(?:\[[^\]]{1,64}\]\s*)+(?<=\s)")
# Single-bracket form: used where we ONLY care about the outermost bracket
# (e.g. existing_prefix_host, which reads the [NICK.host] claim segment).
_PREFIX_RE_FIRST = re.compile(r"^\[[^\]]{1,64}\]")
# One leading bracket-group (with optional preceding whitespace); used to
# iteratively walk the leading bracket sequence in extract_sub_token.
_LEADING_BRACKET_RE = re.compile(r"\s*\[([^\]]{1,64})\]")

# Separators the title-format template can emit between tokens. Used to split an
# existing ``[NICK.HOST] ...`` prefix back into its segments so a self-heal pass
# can detect that another host's monitor already claimed the session and leave
# it alone (see existing_prefix_host).
_PREFIX_SEP_RE = re.compile(r"[.\s/@:]")

# Keys in the nickname map that carry one of these are treated as GLOB patterns
# by nickname_for; every other key keeps pure exact-match semantics (so no
# existing plain entry changes meaning).
_GLOB_META_RE = re.compile(r"[*?\[]")


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
    """The nickname for *repo*: an exact map entry, else the most specific
    matching GLOB entry, else an auto-derived acronym.

    Glob keys (``divorcio*=DP``) exist because a feature worktree's dir name
    becomes its own repo name -- ``divorcio-73-familyfund-pipeline`` derives
    ``D7FP`` (hyphenated -> initials), which flaps against the base repo's
    ``DP``. Worse, a transcript dir OUTLIVES the worktree and is a repo
    derivation source, so the stale name keeps resolving after the worktree is
    reaped. Without globs every ``<repo>-<ticket>-<slug>`` needs its own hand-
    added line, forever -- fixing instances instead of the mechanism.

    Precedence is most-specific-first so a glob can't shadow a deliberate
    entry: exact beats glob, and among globs the LONGEST pattern wins
    (``divorcio-legacy*`` beats ``divorcio*``), ties broken lexicographically
    so the result never depends on dict ordering. Only keys containing a glob
    metacharacter are treated as patterns, so every existing plain key keeps
    pure exact-match semantics."""
    key = (repo or "").lower()
    exact = nmap.get(key)
    if exact:
        return exact
    best: Optional[Tuple[str, str]] = None
    for pattern, nick in nmap.items():
        if not _GLOB_META_RE.search(pattern):
            continue
        if fnmatch.fnmatchcase(key, pattern):
            if best is None or (len(pattern), pattern) > (len(best[0]), best[0]):
                best = (pattern, nick)
    return best[1] if best else derive_nickname(repo)


_HOST_NOTE_SUFFIX_RE = re.compile(r"(?i)note$")


def normalize_host_segment(host: str) -> str:
    """Drop a trailing ``note`` from a host nickname token so titles render
    ``[AH.m5]`` instead of ``[AH.m5note]``. A MacBook whose hostname falls
    through ``NICKNAME_RULES`` to the dotted-label split yields the literal
    ``...m5note`` -- the canonical fix is ``REMOTE_CONTROL_HOST=m5`` in that
    host's plist, but until every host is set we normalize at render time.
    Strips trailing separators left after the suffix is gone; if the result
    is empty (host was exactly ``note``), returns the input unchanged so the
    title never renders a blank host segment."""
    if not host:
        return host
    stripped = _HOST_NOTE_SUFFIX_RE.sub("", host).rstrip("-_.")
    return stripped or host


def strip_prefix(title: str) -> str:
    """Drop a leading ``[token] `` prefix we previously added, if any. Strips a
    chained ``[N][S] `` form too -- so a title written as ``[NICK.host][SUB] desc``
    round-trips back to ``desc`` on a re-render pass."""
    return _PREFIX_RE.sub("", title or "", count=1)


def _leading_brackets(title: str) -> List[str]:
    """All leading bracketed tokens (in order) before the leading whitespace
    boundary, or ``[]`` if the title has none or no bracket-run ends at
    whitespace. The trailing-whitespace requirement mirrors strip_prefix so the
    two helpers agree on what counts as a prefix."""
    text = title or ""
    brackets: List[str] = []
    pos = 0
    while True:
        m = _LEADING_BRACKET_RE.match(text, pos)
        if not m:
            break
        brackets.append(m.group(1))
        pos = m.end()
    if not brackets:
        return []
    if pos >= len(text) or not text[pos].isspace():
        return []
    return brackets


def nick_base(token: str) -> str:
    """The bare nickname segment of a rendered prefix token: everything before
    the first host/branch separator. ``DEV.m5`` -> ``DEV``; ``DEV`` -> ``DEV``;
    ``CRC.mini.feature-x`` -> ``CRC``.

    A leading bracket is recognized as a (possibly re-rendered) NICK occurrence
    by its base, NOT by exact string match, because the host/branch segment
    FLAPS between watcher passes: host-local detection races (a bridge worktree
    momentarily invisible to the indexer) so one pass renders ``DEV.m5`` and the
    next ``DEV``. Under exact-match those two look like a nick and an unrelated
    sub, so the stale one is preserved and a fresh nick is prepended -- the
    UNBOUNDED stacking (``[DEV.m5]`` -> ``[DEV][DEV.m5]`` -> ``[DEV.m5][DEV]
    [DEV.m5]`` -> ...). Matching on the base collapses every host variant of the
    nick into the single leading slot, so re-apply is a true no-op."""
    parts = [p for p in _PREFIX_SEP_RE.split(token or "") if p]
    return parts[0] if parts else (token or "")


def _has_host_suffix(token: str, host: str) -> bool:
    """True if *token* renders as ``<base>.<host>`` (or any separator variant) --
    i.e. its LAST segment is *host*. Only NICK brackets ever carry the
    ``{host}`` suffix (``apply_prefix`` emits subs BARE), so a host-suffixed
    leading bracket is always a stacked prior-pass nick render, never a real
    sub. Used to collapse a LEGACY/AUTO-DERIVED stale nick (``DIV.m5`` on a repo
    that now auto-derives ``DP``) that ``nick_base(bracket) in known`` misses
    because the stale base is neither the current nick nor a *configured* one."""
    if not host:
        return False
    parts = [p for p in _PREFIX_SEP_RE.split(token or "") if p]
    return len(parts) >= 2 and parts[-1] == host


def extract_sub_tokens(title: str, nick: Optional[str] = None,
                       nicks: Optional[Iterable[str]] = None,
                       host: str = "") -> List[str]:
    """All sub-bracket tokens in a chained ``[NICK][S1][S2] desc`` prefix, in
    order, or ``[]`` if there are none. With *nick* supplied (the canonical
    rendered NICK token for this session), every leading bracket whose
    :func:`nick_base` equals the nick's base is treated as a stacked NICK
    occurrence and dropped -- so every host variant of the nick (``DEV``,
    ``DEV.m5``, ``DEV.mini``) collapses into the single leading slot rather than
    surviving as a bogus sub. The remainder are real subs (``MGR-2``,
    ``MGR2-W1``, ``#123``, ...), whose bases never coincide with a repo nick.

    *nicks* is the set of ALL configured repo nicknames (``nmap.values()``).
    Matching the current *nick* alone is not enough: repo->nick resolution can
    flap between two DIFFERENT nicks for one session (``FE`` one pass, ``FIN``
    the next -- a worktree resolving through a different config source), and
    then ``nick_base(bracket) != nick_base(nick)``, the drop-loop bails at
    index 0, the stale nick is misclassified as a real sub, and apply_prefix
    prepends the fresh nick IN FRONT of it. That accretes ONE BRACKET PER PASS,
    without bound::

        [FE.m5][MGR6-W11][#19] x
        -> [FIN.m5][FE.m5][MGR6-W11][#19] x
        -> [FE.m5][FIN.m5][FE.m5][MGR6-W11][#19] x   ...forever

    Treating every leading bracket whose base is a KNOWN repo nick as a nick
    occurrence collapses the different-nick flap the same way ``nick_base``
    already collapses the host-variant flap: the title keeps exactly one
    leading nick slot, and real subs (which never collide with a repo nick)
    survive intact.

    *host* (the monitor's host nickname) covers the LEGACY / AUTO-DERIVED stale
    nick: a repo whose auto-derived nick CHANGED (``divorce-prep`` once rendered
    ``DIV``, now ``DP``) leaves a ``[DIV.m5]`` bracket whose base is neither the
    current nick nor a *configured* one, so the ``known`` set misses it and it
    survives as a bogus sub -- a pre-existing double the daemon can't self-heal
    (only a manual titles-set would). But ``apply_prefix`` emits subs BARE and
    only ever suffixes the NICK bracket with ``{host}``, so ANY leading bracket
    rendered as ``<base>.<host>`` (last segment == *host*) is provably a stacked
    prior-pass nick, whatever its base. Dropping those lets a fresh stacked
    title self-heal on the next DAEMON cycle. A ``<base>.<other-host>`` bracket
    is left intact (a cross-host claim; ``existing_prefix_host`` owns that flap).

    Without *nick*, falls back to the legacy single-sub heuristic (assume the
    first N-1 leading brackets are stacked NICK dups, return the LAST one) so
    :func:`extract_sub_token` and the existing new-session ``--subname``
    round-trip stay byte-identical."""
    brackets = _leading_brackets(title)
    if len(brackets) < 2:
        return []
    if nick is not None:
        # The current nick is always droppable; the rest of the configured
        # nicks cover the flapped-to-a-different-nick case. *host* covers the
        # LEGACY/AUTO-DERIVED stale nick: a leading bracket rendered with our
        # host suffix (``DIV.m5``) is a stacked prior-pass nick regardless of
        # whether its base is configured, since only nick brackets carry the
        # host suffix -- so it collapses too, letting the daemon self-heal a
        # double a manual titles-set would otherwise be needed for.
        known = {nick_base(n) for n in (nicks or ()) if n}
        known.add(nick_base(nick))
        i = 0
        while i < len(brackets) and (
            nick_base(brackets[i]) in known or _has_host_suffix(brackets[i], host)
        ):
            i += 1
        return brackets[i:]
    return [brackets[-1]]


def extract_sub_token(title: str) -> Optional[str]:
    """Back-compat single-sub form of :func:`extract_sub_tokens`. Returns the
    LAST leading bracket in a chained ``[A][B] desc`` or ``[A] [A][B] desc``
    prefix, or ``None`` if the title has at most one leading bracket."""
    subs = extract_sub_tokens(title)
    return subs[-1] if subs else None


def existing_prefix_host(title: str) -> Optional[str]:
    """The host segment of an existing ``[NICK.HOST] ...`` prefix, or None if
    the title has no prefix or its prefix has no host segment. ``[AO.mini] x``
    -> ``"mini"``; ``[AH] x`` -> ``None`` (no second segment, so no host claim);
    ``no prefix here`` -> ``None``.

    A self-heal pass uses this to leave already-claimed titles alone: when a
    session shows ``[AO.<host-a>]`` and *this* host is ``<host-b>``, we don't
    want host-b's pass to overwrite host-a's claim and start a ping-pong with
    host-a's own pass. The host token is always the *last* segment because the
    default template (and the supported tokens) put ``{nick}`` first. Reads the
    FIRST bracket only, so a chained ``[NICK.host][SUB]`` title still resolves
    to ``host`` (the [SUB] is a sibling, not part of the claim)."""
    m = _PREFIX_RE_FIRST.match(title or "")
    if not m:
        return None
    inner = m.group(0).strip()[1:-1].strip()  # strip "[" + "]"
    parts = [p for p in _PREFIX_SEP_RE.split(inner) if p]
    return parts[-1] if len(parts) >= 2 else None


def apply_prefix(title: str, nickname: str, sub: Optional[str] = None,
                 subs: Optional[List[str]] = None) -> str:
    """Re-prefix a title with ``[nickname] `` (idempotent: an existing prefix is
    replaced, not stacked). With *sub* or *subs*, emit the chained
    ``[nickname][s1][s2] desc`` form -- the subname-tag mechanism new-session
    uses to mark a spawned one-off subsession, and the manage skill extends
    with a second ``[S<k>]`` to identify a worker within its manager. *subs*
    (multi) takes precedence over *sub* (legacy single); empty entries collapse."""
    body = strip_prefix(title)
    chain: List[str] = []
    if subs:
        chain = [s for s in subs if s]
    elif sub:
        chain = [sub]
    if chain:
        tail = "".join(f"[{s}]" for s in chain)
        return f"[{nickname}]{tail} {body}".rstrip()
    return f"[{nickname}] {body}".rstrip()


def is_active_session(session: dict) -> bool:
    """True if *session* is in the platform's ``active`` status (the only state
    that can be self-healed). The ``/sessions`` list endpoint returns archived
    sessions too — they outnumber live ones 10:1 — and re-titling them is wasted
    work + visual noise in the ``list`` plan."""
    return (session.get("status") or "active") == "active"


def is_host_local(session: dict, worktree_index: Dict[str, str]) -> bool:
    """True if *session* physically runs on THIS host: its id is in the local
    worktree index (a bridge worktree dir, or a live desktop-app pid+cwd entry).
    A ``config.sources[].url`` (cloud/sandbox provenance) does NOT disqualify a
    session that ALSO has a local bridge dir here -- a forked-to-local sandbox
    session, or one launched here with a GitHub source attached, physically runs
    on this host and should carry the ``.host`` suffix. Two hosts both seeing
    the same sid (e.g. forked on both) are kept from ping-ponging the suffix by
    the existing_prefix_host guard in plan_renames."""
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
        "host": normalize_host_segment(host) if host_local else "",
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
        # whenever the same session is visible to both (e.g. host-a sees its
        # own bridge -> writes ``.<host-a>``; host-b sees the same sid in its
        # log tail -> would overwrite to ``.<host-b>``). A non-claim title
        # (``[AH]`` or no prefix) is still re-prefixed, since adding a fresh
        # claim is fine.
        claimed_by = existing_prefix_host(old)
        if claimed_by and claimed_by != host:
            plan.append(Rename(sid, repo, None, old, old))
            continue
        local = is_host_local(s, worktree_index)
        # Self-claim is authoritative: if the title already carries OUR host
        # token, treat the session as host-local even when the on-disk indexer
        # doesn't see it (worktree dir lives outside the scanned roots, or the
        # session connected through a path that bypasses the log scanner).
        # Otherwise the next pass would re-render with host_local=False and
        # strip our own claim, which is the exact ping-pong we're trying to
        # avoid for foreign claims -- the symmetry has to hold both ways.
        if claimed_by == host:
            local = True
        branch = branch_for(sid, repo) if (local and want_branch) else ""
        vals = session_values(s, repo, nmap, host=host, host_local=local, branch=branch)
        token = render_prefix(template, vals)
        # Preserve any [SUB] tags set by new-session --subname or by the
        # manage skill's titles-set (e.g. ``[MGR-3][S1]``). The watcher itself
        # has no per-session source for subnames -- the only authoritative
        # source is the title it's looking at right now -- so re-extract every
        # pass and re-emit. Pass the rendered NICK so dup-NICK leading brackets
        # are dropped and any real subs (one OR multiple) survive intact.
        subs = extract_sub_tokens(old, nick=token, nicks=nmap.values(), host=host)
        plan.append(Rename(sid, repo, token, old,
                           apply_prefix(old, token, subs=subs)))
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


def self_session_id_from_env(env: Optional[dict] = None) -> Optional[str]:
    """This session's own ``cse_*`` id, read from the
    ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` JWT the app injects into our env, or
    None if the var is absent/garbage or carries no ``cse_`` session claim.

    The ``--self`` fallback for sessions that are NOT in a bridge worktree: a
    manager or worker anchored in a plain checkout has no
    ``.claude/worktrees/bridge-cse_<id>`` path for ``session_id_from_path`` to
    parse, so cwd-based resolution fails and the caller used to have to look
    its own id up by hand and pass ``--id``. The JWT is the same source
    :func:`parse_cmd_session_id` reads out of a live process's env blob -- here
    we just read our OWN env directly instead of shelling out to ``ps``.
    Signature is not verified (see :func:`_jwt_payload`); the ``cse_`` prefix
    check is what keeps a malformed/foreign claim from being used as an id."""
    raw = (env if env is not None else os.environ).get(
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN") or ""
    payload = _jwt_payload(raw) if raw else None
    sid = (payload or {}).get("session_id") if payload else None
    return sid if isinstance(sid, str) and sid.startswith("cse_") else None


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


def build_mm_log_index(logdir: Path, host: str) -> Dict[str, str]:
    """Map session id -> repo by scanning ``<logdir>/<host>-<repo>.log`` tails
    for the OSC-8 hyperlink the ``claude`` TUI prints when a desktop-app session
    connects via the remote-control server. Each ``<host>-<repo>`` server only
    ever serves sessions whose cwd is under that repo, so the repo is just the
    file's ``<host>-...log`` basename -- no per-line parsing needed.

    Closes the gap left by worktree+projects indexes: a session launched
    directly in a repo checkout (no bridge worktree, transcript dir is the
    plain ``-Users-<u>-dev[-<repo>]`` form with no ``--claude-worktrees-bridge-
    cse-`` suffix and ``<uuid>.jsonl`` files rather than ``<cse>.jsonl``) leaves
    nothing on disk linking its local uuid to its API ``cse_<id>``. The
    remote-control server's stdout *does* print that ``cse_<id>`` once on
    connect, and the supervisor pipes that stdout to ``<host>-<repo>.log``."""
    index: Dict[str, str] = {}
    prefix = f"{host}-"
    try:
        files = list(Path(logdir).glob(f"{prefix}*.log"))
    except OSError:
        return index
    for f in files:
        repo = f.name[len(prefix):-len(".log")]
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
    host: Optional[str] = None,
) -> Dict[str, str]:
    """Union of three host-local sources, in precedence order:

      1. ``build_worktree_index`` -- live bridge worktrees (authoritative:
         current on-disk state).
      2. ``build_projects_index`` -- surviving bridge-cse transcript dirs (still
         reliably name a repo even after the worktree env is reaped).
      3. ``build_mm_log_index`` -- cse-ids harvested from remote-control server
         logs (catches desktop-app sessions launched directly in a checkout,
         which leave no bridge artifacts). Requires *host* (the current host's
         nickname) to glob the right ``<host>-*.log`` files; when not provided,
         the log scan is skipped.

    Worktree wins because a stale project dir may name a worktree that has
    since been moved to a different repo (rare but possible); the log source
    is last because it is the loosest signal (a session that has migrated
    hosts may still appear in the *old* host's log tail until the window
    slides past)."""
    idx = build_worktree_index(Path(dev_root))
    if projects_root:
        for sid, repo in build_projects_index(Path(projects_root), dev_root).items():
            idx.setdefault(sid, repo)
    if logdir and host:
        for sid, repo in build_mm_log_index(Path(logdir), host).items():
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
    host_nick = host or host_nickname()
    index = merged_repo_index(dev, str(Path(projects).expanduser()),
                              logdir=(str(cfg.logdir) if cfg is not None else None),
                              host=host_nick)
    plan = plan_renames(sessions, index, nmap, host_nick,
                        template, fs_branch_resolver(dev))
    if only:
        want = only.lower()
        plan = [r for r in plan if (r.repo or "").lower() == want]
    locked = read_title_locks(cfg)  # `set --raw` sessions: leave title verbatim
    ok = fail = 0
    for r in (r for r in plan if r.changed and r.id not in locked):
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
    "       python3 -m remote_control titles set [--self|--id CSE_ID] [--cwd DIR] [--sub SUB]... [--nick NICK] \"<description>\"\n"
    "       python3 -m remote_control titles watch [--interval SECS]\n"
    "  list  (default): show the planned [NICK] title prefixes (no writes)\n"
    "  apply         : PUT the changed titles\n"
    "  watch         : run as a daemon; re-apply prefixes every --interval\n"
    "                  (or SESSION_TITLE_APPLY_SECS env, default 600s). Logs\n"
    "                  to logs/titles-monitor.log; single-instance lockfile.\n"
    "                  This is the long-running counterweight to the app's\n"
    "                  auto-titler that strips our [NICK] prefix mid-session.\n"
    "  --all         : include archived sessions (default: active only)\n"
    "  set           : set ONE session's title to '[NICK] <description>'\n"
    "                  --self (default): derive id + repo from the current\n"
    "                  bridge worktree; --id targets another session\n"
    "                  --force-host: assert this host owns the session for\n"
    "                  the prefix template (so '{nick}.{host}' produces\n"
    "                  '[AO.<host>]'), even when the on-disk indexer doesn't\n"
    "                  see it as a local bridge -- the escape hatch for\n"
    "                  sessions whose worktree lives outside the scanned\n"
    "                  dev roots\n"
    "                  --sub SUB: emit the chained '[NICK][SUB] desc' form\n"
    "                  so the watcher's extract_sub_tokens preserves SUB on\n"
    "                  subsequent re-renders. Repeatable; manager sessions\n"
    "                  use two ('--sub MGR-3 --sub S1') to tag a worker with\n"
    "                  its manager id and its ordinal within that manager.\n"
    "                  --cwd DIR: override repo derivation from DIR (the\n"
    "                  manage skill passes the manager's own cwd so a session\n"
    "                  outside any indexed bridge layout still gets [NICK]).\n"
    "                  --nick NICK: use NICK verbatim as the bracket prefix,\n"
    "                  skipping repo derivation entirely -- the escape hatch\n"
    "                  for other-host bridge sessions with no local worktree/\n"
    "                  transcript/log for the repo resolver to key off.\n"
    "  --projects-dir DIR : Claude Code transcript root (default ~/.claude/\n"
    "    projects). Repo is derived from a session's transcript-dir name when\n"
    "    its bridge worktree has been deleted but the transcript folder remains.\n"
    "  --logdir DIR : remote-control logs dir (default ai-harness/logs). Used\n"
    "    to harvest cse-ids from <host>-<repo>.log for desktop-app sessions that\n"
    "    have no bridge worktree (e.g. launched directly in ~/dev).\n"
    "  --map / SESSION_TITLE_NICKNAMES env extend the repo->nickname map\n"
    "  SESSION_TITLE_FORMAT env (or a `format=` line in the nicknames file)\n"
    "    sets the prefix template; default {nick}.{host} -> [AO.mini] / [AO].\n"
    "    tokens: {nick} {repo} {host} {branch} {id} {shortid} {engine}"
)


def _parse_args(argv: List[str]) -> dict:
    opts = {"cmd": "list", "dev": DEV, "file": NICKNAMES_FILE,
            "map": "", "only": None, "self": False, "id": None, "desc": "",
            "projects": PROJECTS_DIR, "logdir": LOGDIR, "all": False,
            "force_host": False, "sub": None, "subs": [], "cwd": None,
            "nick": None, "interval": 0, "raw": False}
    desc: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("list", "apply", "set", "watch"):
            opts["cmd"] = a
        elif a == "--interval":
            i += 1; opts["interval"] = int(argv[i])
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
        elif a == "--force-host":
            opts["force_host"] = True
        elif a in ("--raw", "--exact"):
            # set the title verbatim: no [NICK] prefix, no bracket stripping
            opts["raw"] = True
        elif a == "--sub":
            i += 1
            # Repeatable: each --sub appends one bracket. Legacy callers that
            # passed a single --sub still work (read via opts["subs"][0]).
            opts["subs"].append(argv[i])
            opts["sub"] = argv[i]
        elif a == "--cwd":
            i += 1; opts["cwd"] = argv[i]
        elif a == "--nick":
            i += 1; opts["nick"] = argv[i]
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
                              logdir=opts.get("logdir"), host=host_nickname())
    if opts["id"]:
        sid = opts["id"]
        repo = index.get(sid)
        host_local = sid in index  # a known local bridge worktree
    else:
        cwd = os.getcwd()
        sid = session_id_from_path(cwd)
        if sid:
            repo = repo_from_worktree_path(cwd)
            host_local = True  # --self: we ARE running inside it, on this host
        else:
            # Not in a bridge worktree (a manager/worker anchored in a plain
            # checkout). Fall back to our OWN id from the app-injected JWT --
            # there is no local worktree to derive from then, so resolve
            # repo/host_local exactly the way the --id path does and let
            # repo_for_session recover the nick from the session's own source.
            sid = self_session_id_from_env()
            if not sid:
                log("--self: not inside a bridge worktree and no cse_id in "
                    "CLAUDE_CODE_SESSION_ACCESS_TOKEN; pass --id CSE_ID")
                return 2
            repo = index.get(sid)
            host_local = sid in index
    # ``--raw``/``--exact``: force the title to the given text verbatim -- no
    # [NICK] prefix, no bracket stripping, no repo/host derivation. For callers
    # who want an exact title (e.g. preserving a manager label the prefix
    # machinery would otherwise strip). NOTE: the ``titles watch`` daemon will
    # re-apply the [NICK] prefix on its next pass unless it is also stopped.
    if opts.get("raw"):
        _set_title_lock(cfg, sid, True)  # persist against the watcher/apply passes
        code, body = set_title(cfg, token, sid, desc)
        if code == 200:
            print(f"set {sid} -> {desc!r} (raw, locked)")
            return 0
        log(f"FAILED {sid} http={code} body={str(body)[:160]}")
        return 1
    _set_title_lock(cfg, sid, False)  # a normal set hands control back to the watcher
    # ``--nick NICK`` is an explicit escape hatch: skip all repo/host
    # derivation entirely and use NICK verbatim as the bracket prefix. Exists
    # because repo resolution (config.sources[].url / worktree index / cse
    # log tail) has no signal at all for an other-host bridge session --
    # there is no local worktree, no local transcript dir, and no local log
    # to scan -- so `set --id <other-host-sid>` would otherwise always fall
    # back to a bare, unprefixed title.
    if opts.get("nick"):
        subs = list(opts.get("subs") or ([] if not opts.get("sub") else [opts["sub"]]))
        title = apply_prefix(desc, opts["nick"], subs=subs)
        code, body = set_title(cfg, token, sid, title)
        if code == 200:
            print(f"set {sid} -> {title!r}")
            return 0
        log(f"FAILED {sid} http={code} body={str(body)[:160]}")
        return 1
    # ``--cwd PATH`` is the explicit override the manage skill uses: a manager
    # session running outside any indexed bridge layout still gets a project
    # nickname by passing the dir it conceptually belongs to (typically the
    # manager's own cwd). The override beats both the index lookup AND the
    # cloud-source fallback because the caller asserts they know better, and
    # it implies host-local (the caller IS running here).
    if opts.get("cwd"):
        cwd_override = opts["cwd"]
        cwd_repo = (repo_from_worktree_path(cwd_override)
                    or repo_from_cwd(cwd_override, opts["dev"]))
        if cwd_repo:
            repo = cwd_repo
            host_local = True
    if repo is None:  # authoritative fallback: the session's own git source URL
        s = next((x for x in (monitor.list_sessions(cfg, token, log) or [])
                  if x.get("id") == sid), None)
        repo = repo_for_session(s, index) if s else None
        if not host_local:  # don't downgrade a confirmed --self
            host_local = is_host_local(s, index) if s else False
    # `--force-host`: assert this host owns the session even when the on-disk
    # indexer can't prove it (worktree dir outside the scanned dev roots, or
    # a session connected via a path that bypasses the per-server log
    # scanner). Without this, ``set --id <sid>`` on an indexer-blind session
    # emits `[NICK]` (no host suffix) -- and even if you then re-PUT
    # ``[NICK.host]`` by hand, the monitor's title-pass would have stripped
    # it back to `[NICK]` before the fix landed in plan_renames. With the
    # plan_renames fix AND this flag, a single CLI call claims the session
    # for this host AND survives subsequent passes.
    if opts.get("force_host"):
        host_local = True
    file_text = _load_nickname_text(opts["file"])
    nmap = build_nickname_map(file_text,
                              os.environ.get("SESSION_TITLE_NICKNAMES", opts["map"]))
    template = title_format(file_text, os.environ.get("SESSION_TITLE_FORMAT", ""))
    if repo is None:
        log(f"warning: could not determine repo for {sid}; "
            f"setting title without a [NICK] prefix")
    subs = list(opts.get("subs") or ([] if not opts.get("sub") else [opts["sub"]]))
    if repo:
        branch = ""
        if host_local and "{branch}" in template:
            if opts.get("cwd"):
                wt = opts["cwd"]
            elif not opts["id"]:
                wt = cwd
            else:
                wt = str(bridge_worktree_path(opts["dev"], repo, sid))
            branch = git_branch(wt)
        vals = session_values({"id": sid}, repo, nmap, host=host_nickname(),
                              host_local=host_local, branch=branch)
        title = apply_prefix(desc, render_prefix(template, vals), subs=subs)
    else:
        # No repo -> no [NICK]; preserve any --sub bracket chain inline so a
        # manager's [MGR-N][S1] markers survive even when nickname resolution
        # failed (the title is still useful in the picker).
        chain = "".join(f"[{s}]" for s in subs if s)
        title = f"{chain} {desc}".strip() if chain else desc
    code, body = set_title(cfg, token, sid, title)
    if code == 200:
        print(f"set {sid} -> {title!r}")
        return 0
    log(f"FAILED {sid} http={code} body={str(body)[:160]}")
    return 1


def _titles_watcher_lock_file(cfg: UsageLimitConfig) -> Path:
    """Lockfile path for the ``titles watch`` daemon. Distinct from the
    usage-limit monitor's lockfile so the two services run side by side."""
    return cfg.logdir / "titles-monitor.lock"


def _titles_watcher_log_file(cfg: UsageLimitConfig) -> Path:
    """Log file for the ``titles watch`` daemon."""
    return cfg.logdir / "titles-monitor.log"


def _title_locks_file(cfg: Optional[UsageLimitConfig]) -> Path:
    """Session ids whose title is force-set (``set --raw``) and must be left
    verbatim -- the watcher/apply passes skip these so the exact title sticks
    instead of being re-prefixed. One ``cse_`` id per line."""
    return cfg.logdir / "title-locks.txt"


def read_title_locks(cfg: Optional[UsageLimitConfig]) -> set:
    """Set of session ids currently locked to a verbatim title (empty if none)."""
    if cfg is None:
        return set()
    try:
        text = _title_locks_file(cfg).read_text()
    except (FileNotFoundError, OSError):
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip()
            and not ln.strip().startswith("#")}


def _set_title_lock(cfg: Optional[UsageLimitConfig], sid: str,
                    locked: bool) -> None:
    """Add (``locked=True``) or remove (``locked=False``) ``sid`` from the lock
    list. Best-effort; never raises into the caller.

    ``cfg`` is ``None`` when there is no resolved config (notably under test,
    and for callers that drive ``_run_set`` without one). There is nowhere to
    persist a lock then, so this is a no-op -- matching ``read_title_locks``,
    which reports "no locks" for the same case."""
    if cfg is None:
        return
    try:
        ids = read_title_locks(cfg)
        if locked:
            ids.add(sid)
        else:
            ids.discard(sid)
        f = _title_locks_file(cfg)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(f"{i}\n" for i in sorted(ids)))
    except OSError:
        pass


def _run_watch(cfg: UsageLimitConfig, log, interval: int,
               *, sleep=__import__("time").sleep,
               clock=__import__("time").time) -> int:
    """The ``titles watch`` daemon loop. Periodically re-applies the
    ``[NICK.host]`` prefix to every active session that needs it -- the
    counterweight to the platform's auto-titler, which strips our prefix
    mid-session. Single-instance via lockfile. Clean SIGTERM shutdown.

    Mirrors ``usage_limit.monitor.main``'s daemon pattern (lockfile +
    signal handler + 1s tick) so the two services have the same operational
    feel. Separate from usage-limit detection because (a) different
    cadences (titles ~ 10min vs detect ~ 60s), (b) different failure modes
    (title-pass touches every session; detect is read-only mostly), and
    (c) operators want to disable / restart them independently."""
    import signal  # local import: this function is only called as a daemon
    state = {"running": True}

    def handler(signum, _frame):
        log(f"received signal {signum}; shutting down")
        state["running"] = False

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    lock = _titles_watcher_lock_file(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Single-instance check: refuse to start if another live pid holds the lock.
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        else:
            log(f"another instance running pid={pid}; exiting")
            return 0
    lock.write_text(str(os.getpid()))

    log(f"titles watcher up pid={os.getpid()} interval={interval}s")
    try:
        last = 0.0
        while state["running"]:
            now = clock()
            if now - last >= interval:
                last = now
                token = monitor.get_token(cfg, log)
                if token:
                    try:
                        ok, fail = apply_prefixes(cfg, token, log)
                        if ok or fail:
                            log(f"titles: re-applied {ok} prefix(es), {fail} failed")
                    except Exception as e:  # noqa: BLE001 (daemon must stay up)
                        log(f"titles: apply pass failed: {e}")
            # 1s tick bounds SIGTERM latency: time.sleep returns after the
            # handler rather than aborting, so a long sleep would delay the
            # shutdown by up to its full duration.
            for _ in range(int(interval)):
                if not state["running"]:
                    break
                sleep(1)
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
    log("titles watcher exiting")
    return 0


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

    cfg = UsageLimitConfig.from_env()
    if opts["cmd"] == "watch":
        # Daemon mode: log to file (rotates with the rest of the runtime logs),
        # not stderr. Interval precedence: --interval > SESSION_TITLE_APPLY_SECS
        # env > 600s default.
        from .logging_util import make_logger
        cfg.logdir.mkdir(parents=True, exist_ok=True)
        log = make_logger(_titles_watcher_log_file(cfg), utc=True)
        interval = (opts["interval"] or
                    int(os.environ.get("SESSION_TITLE_APPLY_SECS", "600")))
        return _run_watch(cfg, log, interval)

    log = lambda m: print(m, file=sys.stderr)  # noqa: E731  (API client diagnostics)
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
    host_nick = host_nickname()
    index = merged_repo_index(opts["dev"], str(Path(opts["projects"]).expanduser()),
                              logdir=opts.get("logdir"), host=host_nick)
    plan = plan_renames(sessions, index, nmap, host_nick,
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

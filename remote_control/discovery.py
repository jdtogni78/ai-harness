"""Pure dir-discovery + active-dirs.txt allowlist parsing.

No I/O: the caller supplies the listed subdirs and a ``git_probe`` callable, so
the spawn-mode rules are fully unit-testable without a real filesystem or git.

Entries may be **host-scoped** so one shared ``active-dirs.txt`` (it lives in a
git repo synced across machines) can spawn different servers on different hosts.
Each host has a short *nickname* (``cfg.host``; see ``config``). An entry of the
form ``basename@nick`` only spawns on the host whose nickname is ``nick``;
``basename@nick1,nick2`` allows several; a bare ``basename`` (no ``@``) spawns
everywhere (the original, backward-compatible behavior).
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import (
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Tuple,
)

# Friendly nickname rules: ordered (glob, nickname) pairs matched (first wins)
# against the lowercased hostname, so machines of the same kind share one short
# nickname regardless of their full hostname. An unmatched host falls back to
# its first dotted label. Globs are case-insensitive (the hostname is lowered).
NICKNAME_RULES: Tuple[Tuple[str, str], ...] = (
    ("*macbook*", "note"),
    ("*macmini*", "mini"),
)


class Server(NamedTuple):
    name: str          # e.g. "mm-AppOne"
    directory: Path
    spawn_mode: str    # "same-dir" | "worktree"


# Per-basename host scope: None = any host, else the set of allowed nicknames.
HostScope = Optional[FrozenSet[str]]
# Parsed active-dirs.txt: basename -> host scope.
Allowlist = Dict[str, HostScope]


def server_name(basename: str) -> str:
    return f"mm-{basename}"


def nickname_from_hostname(hostname: str) -> str:
    """Default host nickname derived from a hostname.

    A ``NICKNAME_RULES`` glob match wins (so ``Daniels-MacBook-Pro-2.local`` ->
    ``note`` and ``macmini.local`` -> ``mini``); otherwise the first
    dotted label is used (``Some-Server.lan`` -> ``some-server``). The result is
    lowercased so matching is stable and case-insensitive. An explicit
    ``REMOTE_CONTROL_HOST`` overrides this (see ``config``); this is just the
    fallback when none is set.
    """
    h = "".join(hostname.split()).lower()
    for pattern, nick in NICKNAME_RULES:
        if fnmatch.fnmatch(h, pattern):
            return nick
    return h.split(".", 1)[0]


def normalize_host(name: str) -> str:
    """Canonical form of a host nickname (used for both sides of the match)."""
    return "".join(name.split()).lower()


def load_allowlist(text: str) -> Allowlist:
    """Parse active-dirs.txt content into a ``{basename: host-scope}`` map.

    Strips inline ``#`` comments and surrounding whitespace; ignores blank
    lines. ``basename`` -> None (any host); ``basename@a,b`` -> ``{"a", "b"}``.
    Repeats merge: a bare ``basename`` anywhere wins (any host); otherwise the
    nickname sets union. A missing file is the caller's concern -- here an empty
    string yields an empty map, which fail-closed means "nothing allowed" (see
    the supervisor).
    """
    allowed: Allowlist = {}
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()   # strip inline comment + edges
        if not entry:
            continue
        base, sep, hostspec = entry.partition("@")
        base = "".join(base.split())             # no internal whitespace in a basename
        if not base:
            continue
        hosts = {normalize_host(h) for h in hostspec.split(",") if h.strip()}
        if not sep or not hosts:
            # bare basename (or "basename@" with empty spec) -> any host.
            allowed[base] = None
            continue
        prev = allowed.get(base, frozenset())
        if prev is None:
            continue                              # already any-host; keep it
        allowed[base] = prev | hosts
    return allowed


def host_allows(scope: HostScope, host: str) -> bool:
    """True if *host* may run an entry with this scope (None = any host)."""
    return scope is None or normalize_host(host) in scope


def discover(
    dev_root: Path,
    allowlist: Allowlist,
    subdirs: Iterable[Path],
    git_probe: Callable[[Path], bool],
    host: str,
) -> List[Server]:
    """Servers to run on *host* for every allowlisted dir present on disk.

    An entry is eligible only if its basename is allowlisted AND its host scope
    permits *host*. The dev root (basename of *dev_root*, e.g. "dev" -> mm-dev)
    is same-dir. Subdirs are worktree when *git_probe* reports a usable repo
    (inside a work tree AND HEAD resolves -- a fresh ``git init`` with no commit
    can't be branched off, so it falls back to same-dir), else same-dir.
    """
    dev_root = Path(dev_root)
    servers: List[Server] = []
    root_base = dev_root.name
    if root_base in allowlist and host_allows(allowlist[root_base], host):
        servers.append(Server(server_name(root_base), dev_root, "same-dir"))
    for d in subdirs:
        d = Path(d)
        base = d.name
        if base not in allowlist or not host_allows(allowlist[base], host):
            continue
        if base == root_base:
            continue  # don't double-emit if a subdir collides with the root
        mode = "worktree" if git_probe(d) else "same-dir"
        servers.append(Server(server_name(base), d, mode))
    return servers

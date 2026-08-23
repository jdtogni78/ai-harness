#!/usr/bin/env python3
"""fs-inventory — deterministic (no-AI) filesystem + git-state scanner.

Phase 1 of the `organizer` (alias `dream`) skill: read-only scan of one or
more repo roots. Emits a single JSON document describing the subfolder tree,
per-file size/mtime/class/sha256, and the git loss-risk signals
(uncommitted, untracked, stashes, worktree list). Mutates NOTHING — the only
process launched beyond `os.walk` is `git`, and only its read subcommands.

Modeled on ~/.claude/skills/manage/scripts/session-inventory.py: no AI, no
network, JSON out, stable keys so the downstream tools (dup-detect.py,
folder-rater.py) can join on them.

Usage:
  fs-inventory.py                       # scan ai-harness + its worktrees
  fs-inventory.py --root ~/dev/foo      # scan an explicit root (repeatable)
  fs-inventory.py --no-worktrees        # just the given/default root, no wt expand
  fs-inventory.py --no-hash             # skip content hashing (faster)
  fs-inventory.py --max-hash-mb 5       # skip hashing files larger than N MB
  fs-inventory.py -o inventory.json     # write to file instead of stdout

Default root: ~/dev/ai-harness (+ every path from `git worktree list`).
"""
import argparse, hashlib, json, os, subprocess, sys
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
DEFAULT_ROOT = os.path.join(HOME, "dev", "ai-harness")

# Directories we never recurse into (recorded as pruned signals instead).
PRUNE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".next", ".turbo",
    "dist", "build", ".idea", ".gradle", "target",
}

# File classification by extension / basename. Order: junk wins, then the rest.
JUNK_NAMES = {".DS_Store", "Thumbs.db", ".localized"}
JUNK_EXTS = {".tmp", ".bak", ".swp", ".swo", ".orig", ".pyc", ".pyo", ".log"}
DOC_EXTS = {".md", ".rst", ".txt", ".pdf", ".adoc"}
DOC_NAMES = {"README", "LICENSE", "COPYING", "CHANGELOG", "NOTICE", "AUTHORS"}
SCRIPT_EXTS = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".tsx",
               ".rb", ".go", ".rs", ".pl", ".php", ".lua", ".sql"}
DATA_EXTS = {".json", ".yaml", ".yml", ".csv", ".tsv", ".toml", ".ini",
             ".cfg", ".xml", ".env", ".jsonl", ".ndjson"}
ARTIFACT_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".whl",
                 ".egg", ".jar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                 ".mp4", ".mov", ".mp3", ".wav", ".so", ".dylib", ".o", ".a",
                 ".class", ".bin", ".dat", ".db", ".sqlite", ".sqlite3"}

# Cryptic / low-signal basenames that hurt searchability.
LOW_SIGNAL_STEMS = {
    "tmp", "temp", "test", "test2", "foo", "bar", "baz", "new", "old",
    "copy", "untitled", "asdf", "misc", "stuff", "a", "b", "x", "1", "2",
    "final", "final2", "draft", "wip", "scratch", "backup",
}


def now_utc():
    return datetime.now(timezone.utc)


def git(root, *args):
    """Run a read-only git subcommand in `root`; return stdout ('' on error)."""
    try:
        r = subprocess.run(["git", "-C", root, *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def classify(name):
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    base_no_ext = stem  # e.g. README (from README.md), or the whole name
    if name in JUNK_NAMES or ext in JUNK_EXTS or name.endswith("~"):
        return "junk"
    if ext in DOC_EXTS or name in DOC_NAMES or base_no_ext.upper() in DOC_NAMES:
        return "doc"
    if ext in SCRIPT_EXTS:
        return "script"
    if ext in DATA_EXTS:
        return "data"
    if ext in ARTIFACT_EXTS:
        return "artifact"
    return "other"


def sha256_of(path, max_bytes):
    try:
        if max_bytes and os.path.getsize(path) > max_bytes:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def discover_worktrees(root):
    """Absolute paths from `git worktree list --porcelain` (empty if not a repo)."""
    out = git(root, "worktree", "list", "--porcelain")
    paths = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
    return paths


def git_state(root):
    """Loss-risk git signals for a root. All read-only."""
    is_repo = git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    if not is_repo:
        return {"is_repo": False}
    porcelain = git(root, "status", "--porcelain")
    uncommitted, untracked = [], []
    for line in porcelain.splitlines():
        if not line:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            untracked.append(path)
        else:
            uncommitted.append({"status": code.strip(), "path": path})
    stashes = [s for s in git(root, "stash", "list").splitlines() if s]
    worktrees = discover_worktrees(root)
    head = git(root, "rev-parse", "HEAD").strip()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return {
        "is_repo": True,
        "head": head,
        "branch": branch,
        "uncommitted": uncommitted,
        "untracked": untracked,
        "stashes": stashes,
        "worktrees": worktrees,
        "n_uncommitted": len(uncommitted),
        "n_untracked": len(untracked),
        "n_stashes": len(stashes),
    }


def scan_root(root, label, do_hash, max_hash_bytes, files_out, folders_out,
              pruned_out):
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune noisy dirs; record what we skipped.
        keep = []
        for d in dirnames:
            if d in PRUNE_DIRS:
                pruned_out.append(os.path.relpath(os.path.join(dirpath, d), root))
            else:
                keep.append(d)
        dirnames[:] = keep

        rel = os.path.relpath(dirpath, root)
        fkey = label if rel == "." else f"{label}/{rel}"
        classes = {}
        n_files = 0
        total = 0
        newest = None
        oldest = None
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not os.path.isfile(fp) or os.path.islink(fp):
                continue
            cls = classify(fn)
            classes[cls] = classes.get(cls, 0) + 1
            n_files += 1
            total += st.st_size
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
            rec = {
                "path": os.path.relpath(fp, root) if rel != "." else fn,
                "folder": fkey,
                "root": label,
                "name": fn,
                "size": st.st_size,
                "mtime": round(st.st_mtime, 1),
                "class": cls,
            }
            if do_hash and cls != "junk":
                rec["sha256"] = sha256_of(fp, max_hash_bytes)
            files_out.append(rec)
        if n_files or fkey not in folders_out:
            folders_out[fkey] = {
                "root": label,
                "n_files": n_files,
                "total_bytes": total,
                "newest_mtime": round(newest, 1) if newest else None,
                "oldest_mtime": round(oldest, 1) if oldest else None,
                "classes": classes,
            }


def main():
    ap = argparse.ArgumentParser(description="read-only fs + git-state scanner")
    ap.add_argument("--root", action="append", default=[],
                   help="repo root to scan (repeatable; default ~/dev/ai-harness)")
    ap.add_argument("--no-worktrees", action="store_true",
                   help="do NOT expand each root's `git worktree list`")
    ap.add_argument("--no-hash", action="store_true", help="skip content hashing")
    ap.add_argument("--max-hash-mb", type=float, default=5.0,
                   help="skip hashing files larger than N MB (default 5)")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    a = ap.parse_args()

    roots = a.root or [DEFAULT_ROOT]
    roots = [os.path.abspath(os.path.expanduser(r)) for r in roots]

    # Expand worktrees unless suppressed.
    expanded = list(roots)
    if not a.no_worktrees:
        for r in roots:
            for wt in discover_worktrees(r):
                if wt not in expanded:
                    expanded.append(wt)
    # De-dupe while preserving order.
    seen = set()
    scan_list = [r for r in expanded if not (r in seen or seen.add(r))]

    files_out, folders_out, pruned_out, git_out = [], {}, [], {}
    max_hash_bytes = int(a.max_hash_mb * 1024 * 1024) if a.max_hash_mb else 0

    for r in scan_list:
        label = os.path.basename(r.rstrip("/")) or r
        git_out[label] = git_state(r)
        git_out[label]["path"] = r
        scan_root(r, label, not a.no_hash, max_hash_bytes,
                  files_out, folders_out, pruned_out)

    doc = {
        "generated_at": now_utc().isoformat(),
        "tool": "fs-inventory",
        "roots": scan_list,
        "hashed": not a.no_hash,
        "git": git_out,
        "folders": folders_out,
        "files": files_out,
        "pruned_dirs": sorted(set(pruned_out)),
        "summary": {
            "n_roots": len(scan_list),
            "n_folders": len(folders_out),
            "n_files": len(files_out),
            "total_bytes": sum(f["size"] for f in files_out),
        },
    }
    text = json.dumps(doc, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
        print(f"fs-inventory: wrote {a.out} "
              f"({doc['summary']['n_files']} files, "
              f"{doc['summary']['n_folders']} folders, "
              f"{len(scan_list)} roots)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()

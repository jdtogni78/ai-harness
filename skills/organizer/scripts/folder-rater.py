#!/usr/bin/env python3
"""folder-rater — deterministic (no-AI) folder organization rubric.

Phase 2 of the `organizer` (alias `dream`) skill. Consumes fs-inventory JSON
(required) plus dup-detect JSON (optional) and scores every folder on the six
rubric axes, 0-5 each, then a composite A-F grade:

  duplication · naming consistency · objective signal · loss risk
  (uncommitted/orphan worktrees/untracked/stashes) · searchability · reduction

Read-only: it only reads the two JSON inputs, never the filesystem or git.
Emits per-folder grade JSON (--json) and/or a human-readable rated report
(default), worst-first so the eye lands on what needs help.

Usage:
  fs-inventory.py | folder-rater.py                 # inventory on stdin, text report
  folder-rater.py -i inventory.json -d dups.json    # join with dup analysis
  folder-rater.py -i inventory.json --json          # machine-readable grades
"""
import argparse, json, os, re, sys
from collections import defaultdict

# Searchability: cryptic / low-signal basename stems.
LOW_SIGNAL_STEMS = {
    "tmp", "temp", "test", "test2", "foo", "bar", "baz", "new", "old",
    "copy", "untitled", "asdf", "misc", "stuff", "a", "b", "x", "1", "2",
    "final", "final2", "draft", "wip", "scratch", "backup", "z",
}
INDEX_NAMES = {"readme", "index", "__init__", "skill", "manifest", "makefile",
               "package", "pyproject", "setup", "main", "mod", "cargo"}

SNAKE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)+$")
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
CAMEL = re.compile(r"^[a-z]+([A-Z][a-z0-9]*)+$")
PASCAL = re.compile(r"^([A-Z][a-z0-9]*)+$")


def name_style(stem):
    if " " in stem:
        return "space"
    # Idiomatic dunder / leading-underscore names (__init__, _private) read as
    # snake_case once the wrapping underscores are stripped — don't flag them.
    core = stem.strip("_")
    if core and (SNAKE.match(core) or (core.isalnum() and core.islower())):
        return "snake"
    if SNAKE.match(stem):
        return "snake"
    if KEBAB.match(stem):
        return "kebab"
    if CAMEL.match(stem):
        return "camel"
    if PASCAL.match(stem):
        return "pascal"
    if stem.islower() or stem.isupper():
        return "flat"
    return "mixed"


def grade_letter(score):
    if score >= 4.5: return "A"
    if score >= 3.7: return "B"
    if score >= 2.9: return "C"
    if score >= 2.1: return "D"
    if score >= 1.3: return "E"
    return "F"


def load(path, required):
    if not path:
        return None
    if path == "-":
        return json.load(sys.stdin)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        if required:
            sys.exit(f"folder-rater: input not found: {path}")
        return None


def score_duplication(folder_bytes, dup_bytes):
    """Fewer duplicated bytes in the folder → higher. 0 dup → 5."""
    if not folder_bytes:
        return 5.0
    frac = min(1.0, dup_bytes / folder_bytes)
    return round(5.0 * (1.0 - frac), 2)


def score_naming(files):
    """Consistency of the dominant basename style across the folder's files."""
    if len(files) < 2:
        return 5.0
    styles = defaultdict(int)
    for f in files:
        stem = os.path.splitext(f["name"])[0]
        styles[name_style(stem)] += 1
    dominant = max(styles.values())
    share = dominant / len(files)
    return round(5.0 * share, 2)


def score_objective(files, n_files):
    """Presence of an index/manifest/doc that explains the folder."""
    if n_files == 0:
        return 2.5
    has_index = any(
        os.path.splitext(f["name"])[0].lower() in INDEX_NAMES for f in files
    )
    has_doc = any(f["class"] == "doc" for f in files)
    if has_index:
        return 5.0
    if has_doc:
        return 4.0
    # A small, single-purpose folder needs less signage than a big grab-bag.
    return 3.0 if n_files <= 3 else 1.5


def score_loss_risk(files, git_root_state):
    """Inverse of uncommitted/untracked/stash exposure touching this folder."""
    if not git_root_state or not git_root_state.get("is_repo"):
        return 3.0  # unknown git state — neutral, not clean
    folder_files = {f["path"] for f in files}
    risky = 0
    for u in git_root_state.get("uncommitted", []):
        if u["path"] in folder_files or _under(u["path"], files):
            risky += 1
    for p in git_root_state.get("untracked", []):
        if p in folder_files or _under(p, files):
            risky += 1
    # Repo-wide signals apply a small flat penalty to every folder in the root.
    repo_penalty = 0.0
    if git_root_state.get("n_stashes"):
        repo_penalty += 0.5
    n = max(1, len(files))
    local_frac = min(1.0, risky / n)
    return round(max(0.0, 5.0 * (1.0 - local_frac) - repo_penalty), 2)


def _under(path, files):
    """True if a git-reported path lives under any of this folder's files' dir."""
    dirs = {os.path.dirname(f["path"]) for f in files}
    return os.path.dirname(path) in dirs


def score_searchability(files):
    """Descriptive, non-cryptic basenames → higher."""
    if not files:
        return 3.0
    good = 0
    for f in files:
        stem = os.path.splitext(f["name"])[0].lower()
        cryptic = stem in LOW_SIGNAL_STEMS or len(stem) <= 2
        if not cryptic:
            good += 1
    return round(5.0 * good / len(files), 2)


def score_reduction(classes, n_files):
    """Low junk+artifact ratio → higher (folder isn't a dumping ground)."""
    if n_files == 0:
        return 5.0
    waste = classes.get("junk", 0) + classes.get("artifact", 0)
    frac = waste / n_files
    return round(5.0 * (1.0 - min(1.0, frac)), 2)


def build(inv, dup):
    files_by_folder = defaultdict(list)
    for f in inv.get("files", []):
        files_by_folder[f["folder"]].append(f)

    dup_by_folder = (dup or {}).get("duplicated_bytes_by_folder", {})
    git_by_root = inv.get("git", {})

    rows = []
    for fkey, meta in inv.get("folders", {}).items():
        files = files_by_folder.get(fkey, [])
        n = meta.get("n_files", 0)
        root_label = meta.get("root")
        git_state = git_by_root.get(root_label)

        axes = {
            "duplication": score_duplication(meta.get("total_bytes", 0),
                                             dup_by_folder.get(fkey, 0)),
            "naming": score_naming(files),
            "objective": score_objective(files, n),
            "loss_risk": score_loss_risk(files, git_state),
            "searchability": score_searchability(files),
            "reduction": score_reduction(meta.get("classes", {}), n),
        }
        composite = round(sum(axes.values()) / len(axes), 2)
        rows.append({
            "folder": fkey,
            "root": root_label,
            "n_files": n,
            "total_bytes": meta.get("total_bytes", 0),
            "classes": meta.get("classes", {}),
            "axes": axes,
            "composite": composite,
            "grade": grade_letter(composite),
        })

    rows.sort(key=lambda r: (r["composite"], r["folder"]))
    return rows


def _relpath(folder, root):
    """Strip the leading root label from a folder key → its in-repo relpath."""
    if folder == root:
        return "."
    prefix = root + "/"
    return folder[len(prefix):] if folder.startswith(prefix) else folder


def collapse_worktrees(rows):
    """Collapse folders that exist at the same relpath across roots into one
    logical row (A4). Identical grades → a single tidy row; divergent grades →
    one row flagged with the per-root spread so real drift stays visible."""
    groups = defaultdict(list)
    for r in rows:
        groups[_relpath(r["folder"], r["root"])].append(r)
    out = []
    for rel, members in groups.items():
        grades = {m["grade"] for m in members}
        roots = sorted(m["root"] for m in members)
        rep = min(members, key=lambda m: m["composite"])  # worst-case wins
        mean_comp = round(sum(m["composite"] for m in members) / len(members), 2)
        out.append({
            "relpath": rel,
            "n_worktrees": len(members),
            "roots": roots,
            "identical": len(grades) == 1,
            "grade": rep["grade"] if len(grades) == 1 else "/".join(sorted(grades)),
            "composite": mean_comp,
            "axes": rep["axes"],
            "n_files": rep["n_files"],
            "classes": rep["classes"],
            "per_root": {m["root"]: {"grade": m["grade"], "composite": m["composite"]}
                         for m in members} if len(grades) > 1 else None,
        })
    out.sort(key=lambda r: (r["composite"], r["relpath"]))
    return out


AX_ORDER = ["duplication", "naming", "objective", "loss_risk",
            "searchability", "reduction"]
AX_SHORT = {"duplication": "dup", "naming": "name", "objective": "obj",
            "loss_risk": "loss", "searchability": "srch", "reduction": "redu"}


def print_report(inv, dup, rows):
    print(f"===== ORGANIZER RATED REPORT  ({len(rows)} folders) =====")
    print(f"generated_at: {inv.get('generated_at')}")
    roots = inv.get("roots", [])
    print(f"roots: {len(roots)}")
    for r in roots:
        g = None
        for lbl, gs in inv.get("git", {}).items():
            if gs.get("path") == r:
                g = gs
                break
        if g and g.get("is_repo"):
            print(f"  {r}  [{g.get('branch')}]  "
                  f"uncommitted={g.get('n_uncommitted')} "
                  f"untracked={g.get('n_untracked')} "
                  f"stashes={g.get('n_stashes')} "
                  f"worktrees={len(g.get('worktrees', []))}")
        else:
            print(f"  {r}  (not a git repo)")
    if dup:
        s = dup.get("summary", {})
        print(f"duplication: {s.get('n_within_root_groups', 0)} within-root groups, "
              f"{s.get('wasted_bytes_within_root', s.get('wasted_bytes', 0)):,} "
              f"actionable bytes  |  {s.get('n_cross_root_groups', 0)} "
              f"cross-worktree groups, {s.get('wasted_bytes_cross_root', 0):,} "
              f"expected bytes  ({s.get('n_near_groups', 0)} near-dup)")

    hdr = f"\n{'grade':5} {'score':>5}  " + "  ".join(
        f"{AX_SHORT[a]:>4}" for a in AX_ORDER) + f"  {'files':>5}  folder"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        axes = "  ".join(f"{r['axes'][a]:>4.1f}" for a in AX_ORDER)
        print(f"{r['grade']:5} {r['composite']:>5.2f}  {axes}  "
              f"{r['n_files']:>5}  {r['folder']}")

    # Worst-offender call-outs the plan phase acts on.
    dcount = defaultdict(int)
    for r in rows:
        dcount[r["grade"]] += 1
    dist = " ".join(f"{g}:{dcount.get(g,0)}" for g in "ABCDEF")
    print(f"\ngrade distribution: {dist}")
    worst = [r for r in rows if r["grade"] in ("D", "E", "F")]
    if worst:
        print(f"\nlowest-graded folders (plan-phase candidates):")
        for r in worst[:15]:
            weak = sorted(r["axes"].items(), key=lambda kv: kv[1])[:2]
            weak_s = ", ".join(f"{AX_SHORT[k]}={v}" for k, v in weak)
            print(f"  {r['grade']}  {r['folder']}  (weakest: {weak_s})")


def print_collapsed_report(inv, dup, collapsed):
    print(f"===== ORGANIZER RATED REPORT — collapsed worktrees "
          f"({len(collapsed)} logical folders) =====")
    print(f"generated_at: {inv.get('generated_at')}")
    roots = inv.get("roots", [])
    print(f"roots collapsed: {len(roots)}")
    if dup:
        s = dup.get("summary", {})
        print(f"duplication: {s.get('n_within_root_groups', 0)} within-root groups, "
              f"{s.get('wasted_bytes_within_root', 0):,} actionable bytes  "
              f"(cross-worktree copies excluded)")
    hdr = f"\n{'grade':7} {'score':>5}  " + "  ".join(
        f"{AX_SHORT[a]:>4}" for a in AX_ORDER) + f"  {'wt':>2} {'files':>5}  folder"
    print(hdr)
    print("-" * len(hdr))
    for r in collapsed:
        axes = "  ".join(f"{r['axes'][a]:>4.1f}" for a in AX_ORDER)
        flag = "" if r["identical"] else " *DIVERGENT*"
        print(f"{r['grade']:7} {r['composite']:>5.2f}  {axes}  "
              f"{r['n_worktrees']:>2} {r['n_files']:>5}  {r['relpath']}{flag}")
    div = [r for r in collapsed if not r["identical"]]
    print(f"\nlogical folders: {len(collapsed)}   divergent across worktrees: {len(div)}")
    for r in div:
        spread = ", ".join(f"{root}={pr['grade']}({pr['composite']})"
                           for root, pr in r["per_root"].items())
        print(f"  {r['relpath']}: {spread}")


def main():
    ap = argparse.ArgumentParser(description="folder organization rubric (A-F)")
    ap.add_argument("-i", "--in", dest="inp", default="-",
                   help="fs-inventory JSON (default: stdin)")
    ap.add_argument("-d", "--dups", help="dup-detect JSON (optional)")
    ap.add_argument("--json", action="store_true",
                   help="emit grade JSON instead of the text report")
    ap.add_argument("--collapse-worktrees", action="store_true",
                   help="collapse same-relpath folders across roots into one "
                        "logical row (A4); flags per-root grade divergence")
    ap.add_argument("-o", "--out", help="write output here instead of stdout")
    a = ap.parse_args()

    inv = load(a.inp, required=True)
    dup = load(a.dups, required=False) if a.dups else None
    rows = build(inv, dup)
    if a.collapse_worktrees:
        collapsed = collapse_worktrees(rows)

    if a.json:
        if a.collapse_worktrees:
            grade_of = lambda r: r["grade"][0]  # first letter for divergent rows
            doc = {
                "generated_at": inv.get("generated_at"),
                "tool": "folder-rater",
                "view": "collapsed-worktrees",
                "folders": collapsed,
                "summary": {
                    "n_folders": len(collapsed),
                    "n_divergent": sum(1 for r in collapsed if not r["identical"]),
                    "mean_composite": round(
                        sum(r["composite"] for r in collapsed) / len(collapsed), 2)
                        if collapsed else 0,
                },
            }
            text = json.dumps(doc, indent=2)
            if a.out:
                with open(a.out, "w") as f:
                    f.write(text + "\n")
                print(f"folder-rater: wrote {a.out} ({len(collapsed)} collapsed folders)",
                      file=sys.stderr)
            else:
                print(text)
            return
        doc = {
            "generated_at": inv.get("generated_at"),
            "tool": "folder-rater",
            "folders": rows,
            "summary": {
                "n_folders": len(rows),
                "distribution": {g: sum(1 for r in rows if r["grade"] == g)
                                 for g in "ABCDEF"},
                "mean_composite": round(
                    sum(r["composite"] for r in rows) / len(rows), 2) if rows else 0,
            },
        }
        text = json.dumps(doc, indent=2)
        if a.out:
            with open(a.out, "w") as f:
                f.write(text + "\n")
            print(f"folder-rater: wrote {a.out} ({len(rows)} folders)", file=sys.stderr)
        else:
            print(text)
        return

    printer = ((lambda: print_collapsed_report(inv, dup, collapsed))
               if a.collapse_worktrees
               else (lambda: print_report(inv, dup, rows)))
    if a.out:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            printer()
        with open(a.out, "w") as f:
            f.write(buf.getvalue())
        print(f"folder-rater: wrote {a.out}", file=sys.stderr)
    else:
        printer()


if __name__ == "__main__":
    main()

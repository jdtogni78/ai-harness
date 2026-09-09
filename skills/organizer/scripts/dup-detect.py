#!/usr/bin/env python3
"""dup-detect — deterministic (no-AI) content-hash duplicate finder.

Phase 1 companion to fs-inventory.py. Consumes an fs-inventory JSON document
(from stdin or a file) and reports:

  - exact duplicates: 2+ files sharing an identical sha256 (byte-identical),
  - near-duplicates: files sharing an identical (name, size) pair but for
    which we have no hash agreement — a cheap heuristic for "probably the
    same file copied around" when hashing was skipped or size-capped.

CROSS-ROOT vs WITHIN-ROOT (the worktree correction). When the scan spans a
repo AND its git worktrees, the SAME file exists at the SAME relpath in every
worktree — that is not waste, it's how worktrees check out. Reporting it as
"duplication" over-counted by ~24x on this repo (22.0 MB cross-worktree vs
0.9 MB genuinely within-root). So each group is classified:
  - within-root : 2+ copies inside ONE root  → ACTIONABLE (real redundancy)
  - cross-root  : copies spread across roots  → EXPECTED for worktrees
The headline wasted-bytes and the per-folder tally count WITHIN-ROOT only, and
pure cross-root groups are dropped from the emitted list by default when more
than one root was scanned (override with --all-groups).

Read-only: it only reads the inventory JSON, never the filesystem. Emits JSON
with a per-folder duplicated-byte tally that folder-rater.py joins on.

Usage:
  fs-inventory.py | dup-detect.py
  dup-detect.py -i inventory.json
  dup-detect.py -i inventory.json -o dups.json
  dup-detect.py -i inventory.json --all-groups   # keep cross-worktree groups too
"""
import argparse, json, sys
from collections import defaultdict


def load(path):
    if path and path != "-":
        with open(path) as f:
            return json.load(f)
    return json.load(sys.stdin)


def main():
    ap = argparse.ArgumentParser(description="content-hash duplicate finder")
    ap.add_argument("-i", "--in", dest="inp", default="-",
                   help="fs-inventory JSON (default: stdin)")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    ap.add_argument("--min-bytes", type=int, default=1,
                   help="ignore files smaller than N bytes (default 1: skip empties)")
    ap.add_argument("--all-groups", action="store_true",
                   help="keep pure cross-root (worktree) groups in the output "
                        "(default: drop them when >1 root was scanned)")
    a = ap.parse_args()

    inv = load(a.inp)
    files = inv.get("files", [])
    n_roots = len(inv.get("roots", []) or [])
    drop_cross = (n_roots > 1) and not a.all_groups

    by_hash = defaultdict(list)
    by_namesize = defaultdict(list)
    for f in files:
        if f.get("size", 0) < a.min_bytes:
            continue
        h = f.get("sha256")
        if h:
            by_hash[h].append(f)
        by_namesize[(f.get("name"), f.get("size"))].append(f)

    # Exact duplicate groups (same content hash, 2+ members).
    exact = []
    dup_bytes_by_folder = defaultdict(int)
    hashed_dup_paths = set()
    wasted_within = 0          # actionable: redundant copies inside one root
    wasted_cross = 0           # expected: same file across worktrees
    n_within_groups = 0
    n_cross_groups = 0
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: (x["root"], x["path"]))
        size = group_sorted[0]["size"]
        for g in group_sorted:
            hashed_dup_paths.add((g["root"], g["path"]))

        # Split the group by root. Extra copies WITHIN a root are the real
        # redundancy; copies that only differ by root are worktree checkouts.
        by_root = defaultdict(list)
        for g in group_sorted:
            by_root[g["root"]].append(g)
        within_extra = sum(len(v) - 1 for v in by_root.values())
        cross_extra = (len(group_sorted) - 1) - within_extra
        w_within = size * within_extra
        w_cross = size * cross_extra
        wasted_within += w_within
        wasted_cross += w_cross

        # Per-folder tally: only the 2nd+ copy INSIDE a root counts.
        for root, gs in by_root.items():
            for g in gs[1:]:
                dup_bytes_by_folder[g["folder"]] += size

        has_within = within_extra > 0
        scope = "within-root" if has_within else "cross-root"
        if has_within:
            n_within_groups += 1
        else:
            n_cross_groups += 1

        if drop_cross and not has_within:
            continue  # pure worktree checkout — not reported by default

        exact.append({
            "sha256": h,
            "size": size,
            "count": len(group_sorted),
            "scope": scope,
            "within_root_wasted": w_within,
            "cross_root_wasted": w_cross,
            "wasted_bytes": w_within,  # headline = actionable bytes
            "roots": sorted(by_root.keys()),
            "paths": [{"root": g["root"], "path": g["path"], "folder": g["folder"]}
                      for g in group_sorted],
        })

    # Near-duplicate groups: same (name,size), not already an exact-dup group,
    # 2+ members (cheap heuristic when hashes are absent / size-capped).
    near = []
    for (name, size), group in by_namesize.items():
        if len(group) < 2:
            continue
        # Skip if this set is fully explained by an exact-hash match.
        keyset = {(g["root"], g["path"]) for g in group}
        if keyset <= hashed_dup_paths:
            continue
        near.append({
            "name": name,
            "size": size,
            "count": len(group),
            "paths": [{"root": g["root"], "path": g["path"], "folder": g["folder"]}
                      for g in sorted(group, key=lambda x: x["path"])],
        })

    # Actionable first (within-root wasted), then by total size.
    exact.sort(key=lambda x: (-x["within_root_wasted"], -x["size"]))
    near.sort(key=lambda x: -(x["size"] * x["count"]))

    doc = {
        "generated_at": inv.get("generated_at"),
        "tool": "dup-detect",
        "source_roots": inv.get("roots"),
        "n_roots": n_roots,
        "cross_root_dropped": drop_cross,
        "exact_duplicates": exact,
        "near_duplicates": near,
        "duplicated_bytes_by_folder": dict(dup_bytes_by_folder),
        "summary": {
            "n_exact_groups": len(exact),
            "n_within_root_groups": n_within_groups,
            "n_cross_root_groups": n_cross_groups,
            "n_near_groups": len(near),
            "wasted_bytes": wasted_within,          # headline = actionable
            "wasted_bytes_within_root": wasted_within,
            "wasted_bytes_cross_root": wasted_cross,
            "hashed": inv.get("hashed", False),
        },
    }
    text = json.dumps(doc, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
        print(f"dup-detect: wrote {a.out} "
              f"({n_within_groups} within-root groups, {wasted_within:,} actionable "
              f"bytes; {n_cross_groups} cross-worktree groups, {wasted_cross:,} "
              f"expected bytes)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()

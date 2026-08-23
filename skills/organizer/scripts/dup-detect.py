#!/usr/bin/env python3
"""dup-detect — deterministic (no-AI) content-hash duplicate finder.

Phase 1 companion to fs-inventory.py. Consumes an fs-inventory JSON document
(from stdin or a file) and reports:

  - exact duplicates: 2+ files sharing an identical sha256 (byte-identical),
  - near-duplicates: files sharing an identical (name, size) pair but for
    which we have no hash agreement — a cheap heuristic for "probably the
    same file copied around" when hashing was skipped or size-capped.

Read-only: it only reads the inventory JSON, never the filesystem. Emits JSON
with a per-folder duplicated-byte tally that folder-rater.py joins on.

Usage:
  fs-inventory.py | dup-detect.py
  dup-detect.py -i inventory.json
  dup-detect.py -i inventory.json -o dups.json
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
    a = ap.parse_args()

    inv = load(a.inp)
    files = inv.get("files", [])

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
    wasted = 0
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda x: x["path"])
        size = group_sorted[0]["size"]
        # Wasted = every copy beyond the first.
        wasted += size * (len(group_sorted) - 1)
        exact.append({
            "sha256": h,
            "size": size,
            "count": len(group_sorted),
            "wasted_bytes": size * (len(group_sorted) - 1),
            "paths": [{"root": g["root"], "path": g["path"], "folder": g["folder"]}
                      for g in group_sorted],
        })
        for i, g in enumerate(group_sorted):
            hashed_dup_paths.add((g["root"], g["path"]))
            if i > 0:  # redundant copies count against their folder
                dup_bytes_by_folder[g["folder"]] += size

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

    exact.sort(key=lambda x: -x["wasted_bytes"])
    near.sort(key=lambda x: -(x["size"] * x["count"]))

    doc = {
        "generated_at": inv.get("generated_at"),
        "tool": "dup-detect",
        "source_roots": inv.get("roots"),
        "exact_duplicates": exact,
        "near_duplicates": near,
        "duplicated_bytes_by_folder": dict(dup_bytes_by_folder),
        "summary": {
            "n_exact_groups": len(exact),
            "n_near_groups": len(near),
            "wasted_bytes": wasted,
            "hashed": inv.get("hashed", False),
        },
    }
    text = json.dumps(doc, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")
        print(f"dup-detect: wrote {a.out} "
              f"({len(exact)} exact groups, {wasted} wasted bytes)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()

---
name: dream
description: >-
  Alias for the `organizer` skill — a loss-safe repo/worktree organization
  auditor that rates folders A-F and proposes reversible reorg, read-only until
  a boss-gated apply. Use when the user says "/dream", "dream-organize this
  repo", or any organizer trigger ("organize this repo", "rate my folders",
  "audit my worktrees", "clean up without losing anything"). This is a thin
  pointer: it hands off to `organizer`.
---

# /dream → organizer

`dream` is the accepted **alias** for the `organizer` skill (not a separate
tool). Everything — the scan → rate → plan → discuss → backup → apply → report
phase model, the six-axis rubric, the four deterministic tools, and the
backup-before-change GATE — lives in the `organizer` skill.

**Do this now:** invoke the `organizer` skill (`Skill(skill="organizer")`) and
follow it, or read `../organizer/SKILL.md` and proceed from there. The tools are
in `../organizer/scripts/` (`fs-inventory.py`, `dup-detect.py`,
`folder-rater.py`, `safe-backup.sh`).

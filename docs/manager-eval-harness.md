# Manager guideline eval harness

Regression-test harness for the session-manager's guideline prompt
(`docs/session-manager-cases.md`). Lets us A/B two guideline variants against a
corpus of real recorded situations and score which produced better
recommendations, so we can iterate on the guidelines with evidence instead of
vibes.

## Why

The manager's per-thread `claude -p` analyzer reads `session-manager-cases.md`
into every investigator prompt (`investigator_prompt_advise` in
`remote_control/manager.py:287`). When we edit that doc we have no way to tell
whether the change actually improved recommendations or quietly regressed the
common path. This harness closes that loop.

## What a test case is

Self-contained JSON at `tests/manager_cases/<case_id>.json`. The case carries
its own frozen input snapshot so it stays reproducible after the underlying
session is archived or its transcript JSONL rotates.

```jsonc
{
  "id": "cse_abc...-q7f3",          // action_sig at capture time
  "captured_at": "2026-05-26T...",
  "captured_by": "manager-ui",
  "tags": ["A-waiting-q", "ANSWER", "managed"],
  "notes": "user free-text",

  "input": {                         // everything analyze_action() needs
    "action": { /* Action._asdict() */ },     // kind, reason, repo, title,
                                              // fresh, managed, question, ...
    "questions": [ { /* RequiredAction.questions item */ } ],
    "transcript_tail": [                      // last_messages() output, frozen
      {"role": "assistant", "text": "..."},
      {"role": "user",      "text": "..."}
    ],
    "session_meta": {                          // "now-relative" facts, frozen
      "idle_secs": 1834,
      "status": "...",
      "limit_text": "..."
    }
  },

  "actual_at_capture": {             // what the manager produced when saved
    "rec_manager": "...",
    "rec_session": "...",
    "analysis":    "..."
  },

  "expected": {                      // editable in UI, prefilled from actual
    "rec_manager": "...",
    "rec_session": "..."
  }
}
```

Storage lives in-repo (`tests/manager_cases/`) so cases are reviewable in PRs
and diffable. ~5-10 KB per case; 200 cases ≈ 1-2 MB.

## Phases

### Phase 1 — Refactor `analyze_action` to accept injected transcript_tail

Small enabling refactor that lets the eval runner feed a frozen transcript into
the analyzer instead of re-reading disk.

- Add `transcript_tail: Optional[List[Tuple[str, str]]] = None` to
  `analyze_action()` (`remote_control/manager.py:407`) and to the prompt
  builder it calls. When `None`, behavior is unchanged (read from disk via the
  current path); when supplied, use it verbatim.
- Unit test in `tests/test_manager.py` covering both branches.
- No behavior change for the live manager.

### Phase 2 — Capture flow in the UI

Lets a reviewer save the situation in front of them as a test case.

- **Backend** (`remote_control/manager_ui.py`): new `POST /api/test_case` next
  to `api_feedback` (~line 215). Payload:
  `{session_id, expected_manager, expected_session, tags, notes}`. Rebuilds the
  snapshot from the cached analysis in `state["sessions"][sid]` plus a fresh
  `last_messages()` freeze, writes `tests/manager_cases/<case_id>.json`.
- **Frontend** (inline JS in the same file, ~line 659): "Save as test case"
  button next to the existing feedback button. Opens an inline editor with
  expected_manager / expected_session prefilled from the current rec, a
  case-kind tag selector, and a notes field. Default flow = accept current rec
  in one click; corrections supported in the editor.
- **Seed corpus**: end of this phase, hand-craft 5-10 "boring" cases
  (idle → defer, running → skip, fresh-question → defer, …) so a guideline
  tweak that breaks the common path can't pass.

Pause after Phase 2 and collect ~20 real cases over 1-2 weeks before building
the runner. If the corpus stays thin, the runner is wasted.

### Phase 3 — Runner with LLM judge (primary scorer)

The LLM judge is the headline scorer, not opt-in. Cheap structural checks may
ride along as sanity signals but don't drive the verdict.

- New module `remote_control/eval.py` + subcommand
  `python -m remote_control eval run [--guidelines PATH] [--out DIR]`.
- For each case in `tests/manager_cases/`:
  1. Reconstruct the `Action` from `input.action`.
  2. Call `analyze_action(action, guidelines=..., transcript_tail=input.transcript_tail)`.
  3. Persist actual rec to `out/eval/<guidelines_hash>/<case_id>.json`.
  4. Score via LLM judge.
- **Judge contract**: a `claude -p` call sees `(input, expected, actual)` and
  returns JSON `{verdict: equivalent|better|worse, score: 1-5, reason: "..."}`.
  Sample **n=3** per case; report mean + stdev. Cache by
  `(case_id, guidelines_hash, sha256(actual))`.
- `<guidelines_hash>` = sha256 of the guidelines file, so runs are
  content-addressed and re-runs of the same guidelines on the same corpus are
  free.
- Run cases in a thread pool (default n=4 workers).

### Phase 4 — A/B compare command + report

- `python -m remote_control eval compare --baseline <hash-A> --candidate <hash-B>`.
- Markdown report: per-case verdict diff, per case-kind aggregates, overall
  mean score delta. Flag any case that flipped equivalent → worse as a
  regression.
- `python -m remote_control eval show <case_id>` for drilling into a single
  case (input, expected, baseline actual, candidate actual, judge reasons).

### Phase 5 — CI wiring (optional)

- `Makefile` targets: `make eval-baseline`, `make eval-pr`.
- GH Action that posts the diff as a PR comment when
  `docs/session-manager-cases.md` or the manager prompts change.

## Open questions deferred

- **Executor-action correctness.** v1 scores recommendation text only; whether
  the parsed manager_rec verb (fork/archive/resume/…) matches expected is a
  follow-up because it needs faking the live API.
- **Corpus selection bias.** Phase 2 seeds 5-10 happy-path cases to mitigate.
  Revisit once we have ~20 real cases and can see actual coverage.

## Tracking

Plan + phase issues on **Remote Control board #2** (linked from the parent
tracking issue).

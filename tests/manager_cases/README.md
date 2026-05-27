# Manager guideline eval corpus

Self-contained `(input, expected)` snapshots used by the guideline
regression-test harness. See `docs/manager-eval-harness.md` for the why.

## How they get here

- **From the running UI** — open `manager-ui`, click *Save as test case* on an
  analyzed row, accept the prefilled expected rec (or edit it), Save. The
  snapshot lands here.
- **Hand-seeded** — files named `seed-*.json` are the "boring path" baseline
  (idle→defer, running→skip, …) so a guideline tweak can't quietly regress the
  common cases.

## File format

```jsonc
{
  "schema": 1,
  "id": "answer-cse_01abc-f3a2b1",   // file-safe slug of "sig"
  "sig": "ANSWER:cse_01abc:f3a2b1",  // manager.action_sig(action)
  "captured_at": "2026-05-26T12:34:56+00:00",
  "captured_by": "manager-ui",       // or "seed", "hand"
  "tags": ["A-waiting-q", "ANSWER"],
  "notes": "user's free-text",

  "input": {
    // Frozen Action (manager.Action._asdict() with the nested RequiredAction
    // also unpacked to a plain dict). The eval runner reconstructs the Action
    // via remote_control.eval_cases.action_from_jsonable().
    "action": { /* session_id, repo, kind, reason, run_dir, question,
                   command, api, managed, required, fresh, title */ },

    // Last messages of the source session at capture time, frozen so the
    // runner doesn't need the on-disk transcript. Same shape as
    // manager.last_messages() returns.
    "transcript_tail": [
      {"role": "assistant", "text": "..."},
      {"role": "user",      "text": "..."}
    ],

    // Optional now-relative facts not already on Action (idle_secs, status,
    // limit_text, ...). Empty object today; future fields are additive.
    "session_meta": {}
  },

  "actual_at_capture": {
    "rec_manager": "...",
    "rec_session": "...",
    "analysis":    "..."
  },

  "expected": {
    "rec_manager": "...",
    "rec_session": "..."
  }
}
```

`id` is derived from `sig` (`kind:session:qhash` -> `kind-session-qhash`); the
filename is `<id>.json`. Re-saving the same situation upserts.

## Schema versioning

`CASE_SCHEMA_VERSION` is defined in `remote_control/eval_cases.py`. Bumped on
any incompatible change to the shape above. The runner refuses to score a
case carrying an unknown version (so old runs aren't silently misread); add a
migration if the change is backward-incompatible.

## Editing by hand

Yes. The format is plain JSON. The most common reason to edit is to refine
`expected` after the live UI captured a wrong rec — that's the whole point of
the corpus.

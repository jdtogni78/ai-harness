# perm_gate_lab — Plan

Captured corpus of permission decisions (static rules, AI tier, Anthropic Auto Mode if
observable, human overrides) plus a UI to review them, label them, run new judge
configurations against them, and score the results with an LLM-as-judge — so guidelines
can be iterated on with regression-test discipline.

Related: ai-harness#23 (perm_gate, closed), ai-harness#25 (follow-ups).

## Decisions locked in

- **Lives in** `ai-harness/perm_gate_lab/` (new module, CLI `pgctl`).
- **UI** is a new tab inside `manager-ui` on `:8765` (shared process, shared auth).
- **Storage** is SQLite at `~/.perm-gate-lab/lab.db`.
- **Capture sources**: hook (static + AI tier), Auto Mode observations (if the recon
  spike finds anything visible), human overrides, synthetic adversarial cases.
- **Scoring**: LLM-as-judge. Scorer model must differ from judge model to reduce
  shared blind-spots. Aggressive caching keyed by `(case_sha, verdict_sha, scorer_sha)`.
- Cross-cutting note in root `DECISIONS.md` as a new `GD-NNNN`.

## Architecture (one paragraph)

The `PreToolUse` hook writes one **case** row per evaluation (tool, args, redacted
context, tier-used, rule-fired, AI rationale if any, final verdict). Out-of-band
importers add **Auto Mode** observations and **synthetic adversarial** cases checked
into the repo. A **judge** is a (guideline text + model + system-prompt) bundle,
versioned in-repo. A **run** is judge × case-set → one **verdict** row per (case, run).
A **scorer** is an LLM-as-judge call that reads (case, verdict) and emits `{score
0–100, risk_tier, agrees_with_human_label?, critique}`. The UI browses cases, lets you
label them, and diffs runs against each other and against labels.

## Storage schema (SQLite)

```
case(id, ts, source, tool, args_json, ctx_json, redacted, sha256)
            -- source ∈ {hook_static, hook_ai, auto_mode_obs, human_override, synthetic}
verdict(id, case_id, run_id, verdict, risk_tier, rationale, model, latency_ms, cost_usd)
            -- verdict ∈ {allow, prompt, deny}
label(case_id, ideal_verdict, ideal_risk_tier, labeler, ts, notes)
run(id, judge_id, case_set, ts, git_sha, notes)
judge(id, name, version, guideline_path, system_prompt, model)
score(verdict_id, scorer_id, score, agrees_with_label, critique, ts)
scorer(id, name, model, system_prompt, version)
```

Dedupe cases by `sha256(tool + canonical_args + minimal_ctx)`; re-runs append verdicts.

## Phases (each shippable on its own)

1. **Capture & storage [DONE].** `pgctl init/import/list/stats` over SQLite. Imports
   directly from the existing `~/dev/ai-harness/logs/perm-gate-decisions.jsonl` — the
   production hook already writes the fields we need, so the originally-planned hook
   patch was dropped (revisit only if phase 4 needs richer ctx for Edit/Write content).
   Redaction runs at ingest; idempotent on `sha256(tool + subject + cwd)`.
2. **Read-only browse UI** — manager-ui tab: case list (filters: source, verdict, tier,
   date), case detail (full ctx, all verdicts). Confirms capture is sound.
3. **Auto Mode recon spike (timebox 2h)** — inspect what Auto Mode exposes to hooks /
   transcripts / CLI on Opus 4.7. Outcome: (a) capture classifier verdict directly,
   (b) capture only user-prompt + answer as proxy, or (c) nothing observable → drop
   that source. Decision recorded as a `GD` in `DECISIONS.md`.
4. **Judge runner** — `pgctl run --judge configs/judges/v1.yaml --cases all` writes one
   `run` row + N `verdict` rows.
5. **LLM-as-judge scorer** — `pgctl score --run <id> [--scorer configs/scorers/strict.yaml]`.
   Different model from the judge. Cache by `(case_sha, verdict_sha, scorer_sha)`.
6. **Labelling + diff UI** — human label form in case detail; run-vs-run diff page
   (rows where verdicts disagree, scorer critique inline); leaderboard (mean score per
   run, false-allow rate on red cases).
7. **CI gate** — pytest target runs the current production judge against the frozen
   `synthetic_adversarial/` corpus + a sampled snapshot of real cases; **hard fail** on
   any false-allow of a red-tier synthetic case. PRs to `configs/judges/` must include
   the diff report in the body.

## Touch list

- `perm_gate_lab/{__init__,db,models,redact,judge,scorer,cli}.py`
- `perm_gate_lab/configs/judges/*.yaml`
- `perm_gate_lab/configs/scorers/*.yaml`
- `perm_gate_lab/corpus/synthetic_adversarial/*.yaml`
- `manager-ui/routes/perm_gate.py` + `templates/perm_gate/*.html`
- existing `perm_gate` hook — add structured JSONL emit behind capture flag
- root `DECISIONS.md` — new `GD-NNNN` entry
- new tracking issue on board #2, linked to follow-up #25

## Open risks

- **Auto Mode observability is unknown.** Phase 3 spike resolves; do not design
  assuming we get the classifier verdict.
- **PII/secrets in captured args.** Redaction must run before disk and again before any
  LLM call. Redactor tests are mandatory in phase 1.
- **Judge/scorer model collusion** — both being Claude shares biases. Different model
  tiers; consider an ensemble scorer for red-tier cases.
- **Cost** — full scoring of a large corpus is real money. Cache aggressively; default
  `pgctl run` to a sampled subset; full-corpus runs explicit.
- **Coexistence with Anthropic Auto Mode.** Hook and Auto Mode could double-prompt.
  Config knob `PERM_GATE_MODE ∈ {primary, second_opinion, off}` decides whether the
  hook gates the call or just logs alongside Anthropic.
- **Labelling burden.** Even with LLM-as-judge picked, the scorer needs an anchor on
  hard cases. Plan to label ~50 seed cases manually before phase 5 is useful.

## Resume pointers

- Current state: planning only, no code.
- First implementation step: phase 1 — schema + hook patch + `pgctl import`.
- Before phase 4: complete the phase-3 Auto Mode recon spike.
- Before phase 5: label ~50 seed cases.

# PromptForge Eval Rubric — FROZEN 2026-10-05

sha256 of this file is recorded in `eval/config.json`. Editing this file
invalidates the freeze: judge scores would no longer be comparable across
arms. Any change requires a new freeze commit.

## Protocol

- Judge: `claude-sonnet-5-5`, temperature 0, **one output per call**.
- Arm identity, spec name, generator model, and all provenance are stripped
  before the call. The judge sees exactly two things:
  1. the TASK text (`tasks[].text`, verbatim),
  2. the CANDIDATE OUTPUT (the response produced under one arm).
- The prompt that produced the candidate is never shown to the judge.
- Response format: a single JSON object, no prose, no markdown fence:
  `{"scores": {"constraint_adherence": 0-10, "structure": 0-10,
  "actionability": 0-10, "correctness": 0-10, "brevity": 0-10},
  "total": 0-50, "notes": "<=30 words"}`.
- Malformed JSON or out-of-range scores => retry that cell once, then mark
  the cell `judge_error` and exclude it from aggregates (count and list them
  in the results).

## Dimensions (each 0-10)

1. **constraint_adherence** — did the output honor the task's stated
   requirements, formats, and explicit exclusions? Unstated requirements
   must NOT be invented; inventing scope loses points.
2. **structure** — does the organization fit THIS task (a plan scores as a
   plan, a script as a script, a name shortlist as a list)? Structure for
   its own sake neither helps nor hurts.
3. **actionability** — could the requester act without asking follow-up
   questions? Concrete specifics over generic advice.
4. **correctness** — technical claims are right; no fabricated facts,
   endpoints, APIs, measurements, or CVEs.
5. **brevity** — nothing outside the request; no filler, no
   meta-commentary, no restating the task.

## Anti-bias rules (binding on the judge)

- Do not reward length, markdown density, or heading count per se.
  `structure` scores fit-to-task only.
- Do not penalize a direct short answer to a small question (task 9, task 7
  are legitimately short).
- Do not reward references to the prompt-engineering framing ("Unstated",
  "Quality checklist", spec vocabulary) — judge the response the requester
  receives, not the prompt that produced it.
- Ties (totals within 1 point) are ties, not wins.

## Deriving arm win-rates

- For each `(eval task, run)`: all four arm outputs are scored
  independently (absolute scoring, one judge call each).
- Pairwise win = higher `total` with difference > 1; otherwise tie.
- Primary report: per-arm-pair win-rate over non-bait eval tasks
  (tasks 1-9), with ties shown.
- Bait tasks (10, 11) are reported separately as routing probes, never in
  the primary win-rate.
- Human review (chef + one other, random 20%, arms shuffled) happens AFTER
  automated judging and is reported side by side with judge win rates —
  never merged into a single number.

# PromptForge

Free, offline, private prompt engineering workbench. Pure Python standard
library — no accounts, no cloud, no telemetry, ₹0.

## Context

PromptForge exists because most prompt tooling wants your prompts on someone
else's server. This one never sees them: everything runs on localhost from a
single folder.

History:

- **v1** (`e044820`, `230e123`) — a 12-file CLI: field specs for cyber and
  ML/LLM work, prompt generation, structural scoring, fixing, comparing,
  merging, ensembling, and a parser that turns raw chat text into finished
  prompts.
- **v2** (`c757aca`) — added the brain (local SQLite learning from your edits),
  the context parser, a stdlib HTTP server with a single-page UI, and a full
  test suite. No LLM anywhere: every rule is deterministic and auditable.
- **This round** — determinism hardening: `as_of` day-pinning, `brain_hash`
  provenance metadata, and the `use_brain` chokepoint so every caller states
  whether the brain may run. A README so the repo explains itself.

Built as a local-first tool for people who write a lot of prompts and want
structure, scoring, and learned preferences without shipping anything.

## What it does

| Command | Purpose |
|---|---|
| `forge gen` | build a field-specific prompt from slot values |
| `forge compile` | turn raw chat text into a finished prompt (parse + build + brain) |
| `forge parse` | read chat text: detect template, slots, constraints |
| `forge check` | score a prompt against the field's structural checklist |
| `forge fix` | patch gaps: graft missing structure, keep your wording |
| `forge compare` | score two prompts side by side |
| `forge merge` | union the strengths of two prompts |
| `forge ensemble` | mutate + score search; returns top candidates |
| `forge learn` | diff generated vs corrected prompt and learn from it |
| `forge brain` | show / forget / reset / export learned patterns |
| `forge lint` / `list` / `show` / `new` | spec maintenance |

Nine field specs ship (`specs/*.json`) — seven cyber, two AI/ML — plus a
universal fallback so `compile` works on any text, any field.

## Web app

```
python website.py          # opens http://127.0.0.1:8765/
python website.py --no-open
```

or double-click `Open Website.bat`. The UI mirrors the CLI exactly — write,
score, improve, compare, variants, library, brain — over the same engine.

## The brain (local learning)

Edits you teach (`forge learn` or the Teach button) are stored in a local
`brain.db` (SQLite, WAL). Weights are deterministic: `count × recency decay`,
30-day half-life. At effective weight ≥ 3.0 a rule starts applying itself:

- learned boilerplate gets suppressed from new prompts,
- learned constraints get appended under `## Your preferences`,
- learned slot preferences fill absent slots (your explicit text always wins).

Nothing is learned until you teach it. Everything stays on this machine.

## Determinism

Given identical input, configuration, brain state, seed, and `as_of`,
every artifact is byte-identical. `--no-brain` is fully deterministic.

Mechanics:

- **`--no-brain`** (also the API's `use_brain: false`) — the brain is never
  read at all; output does not depend on brain state in any way. The old
  `--no-learned` spelling stays as an alias.
- **`--as-of YYYY-MM-DD`** — pins decay to one calendar day so tests and
  evaluations reproduce across runs instead of drifting with the wall clock.
- **`brain_hash`** — sha256 of the effective auto-tier rule payload plus the
  `as_of` day. It appears in reports, JSON output, and stderr footers as
  provenance metadata — never inside the prompt text itself. Sub-threshold
  rules cannot change output, so they don't churn the hash.

```
brain: hash=<64-hex> as_of=2026-10-04 use_brain=True
```

## Tests

```
python -m unittest discover -s tests
python forge.py lint
```

All tests run against a temp brain database; `tests/test_forge.py` isolates
subprocess runs via the `PROMPTFORGE_BRAIN_DB` environment variable.

## Scope

- offline by design: no network calls, stdlib only, Python 3.10+
- the score is structural (checklist coverage), not a model's opinion —
  verify against a real model output before trusting it
- `brain.db`, outputs, and `my.txt` are git-ignored / untracked local state

# PromptForge

Free, offline, private **next-message engine** with a prompt-builder
workbench behind it. Pure Python standard library — no accounts, no cloud,
no telemetry, ₹0.

## The job

Paste a full chat transcript. Get the **next message you send to the same
agent** — plus why, the constraints carried forward, and the alternatives:

```
python nextmsg.py chat.txt            # card for a transcript file
python nextmsg.py -                   # ... or stdin
python nextmsg.py chat.txt --json     # machine-readable card
python nextmsg.py --score fixtures    # score against the pass bar
```

Web: `python website.py` → **http://127.0.0.1:8765/next** — one paste box,
one result card.

## The invariant

**Infer action. Never invent decisions.**

If the transcript leaves an A/B unresolved, the card says *your call* and
picks nothing — that is a hard gate, not a score. The pass bar:

| condition | bar |
|---|---|
| state classification (intent) | ≥ 7 / 10 fixtures |
| constraints carried | 10 / 10 fixtures |
| invented decisions | **0 — absolute blocker** |
| beats the `Continue.` baseline | strictly better |

Fixtures live in `fixtures/` (git-ignored — real chats carry secrets;
`SCHEMA.md` and `PASSBAR.md` are the public format and bar). The bar is
written down *before* the run; fixtures are never tuned after a run starts.
A failed run is a result, not an inconvenience.

Latest: intent 10/10, constraints 10/10, invented 0, violations 10/10
exact, `Continue.` scores 1/10. 156 tests.

## How it reads a chat

1. **Split** — `user:` / `assistant:` markers (bracketed too); code fences
   are opaque, so colons inside code never start a turn. No markers? The
   paste is one user turn.
2. **Classify** — compound priority: a new task the user just redirected
   to, then *unresolved error* > *unverified work* > *open question* >
   *stated next step*, else *continue*.
3. **Extract** — standing rules (constraints) with the turn they were set
   on; posed decisions (options + which user turn resolved them, recency:
   a later "actually B" beats an earlier "A").
4. **Check** — violations: an ask the agent's last reply didn't cover, and
   a negated rule whose object the reply touched. Fresh-error and
   pending-agent suppression keep the false-positive rate honest.
5. **Render** — message, why (each claim cites its turn), constraints
   carried, alternatives.

## Honest limits

Offline and rule-based — deterministic, auditable, and dumb in a known
way:

- **good at**: "fix the error", "answer the question", "continue the
  plan", carrying explicit rules forward, flagging a broken rule or a
  ignored ask.
- **poor at**: strategy calls, judging whether a plan is any good,
  anything needing world knowledge the transcript doesn't contain.
- **not yet**: an optional smart mode (a one-shot analyst prompt you can
  point at any local or remote model) for when the rule engine knows it's
  out of its depth. Planned, not implemented.

## Prompt builder (secondary)

When the next step is a *big new task*, the older builder takes over —
field specs, generation, scoring, fixing, comparing, merging, ensembling,
and a parser that turns raw chat text into finished prompts:

| Command | Purpose |
|---|---|
| `forge gen` | build a field-specific prompt from slot values |
| `forge compile` | turn raw chat text into a finished prompt |
| `forge parse` | read chat text: detect template, slots, constraints |
| `forge check` | score a prompt against the field's checklist |
| `forge fix` | patch gaps: graft missing structure, keep your wording |
| `forge compare` / `merge` / `ensemble` | score, union, mutate+search |
| `forge learn` / `brain` | teach the local brain; show/forget/reset |
| `forge lint` / `list` / `show` / `new` | spec maintenance |

Nine field specs ship (`specs/*.json`) plus a universal fallback.

## Web app

```
python website.py          # opens http://127.0.0.1:8765/
python website.py --no-open
```

or double-click `Open Website.bat`. `/` is the builder UI, `/next` is the
next-message card. Same engine as the CLI, same localhost-only guard
(DNS-rebinding Host allowlist, JSON-only POSTs, cross-origin POST
rejection).

## The brain (local learning)

Edits you teach (`forge learn` or the Teach button) are stored in a local
`brain.db` (SQLite, WAL). Weights are deterministic: `count × recency
decay`, 30-day half-life. At effective weight ≥ 3.0 a rule starts applying
itself: learned boilerplate gets suppressed, learned constraints get
appended, learned slot preferences fill absent slots (your explicit text
always wins). Nothing is learned until you teach it.

## Determinism

Given identical input, configuration, brain state, seed, and `as_of`,
every artifact is byte-identical. `--no-brain` never reads the brain;
`--as-of YYYY-MM-DD` pins decay to one calendar day; `brain_hash` (sha256
of the effective rule payload + day) rides reports and JSON as provenance
metadata — never inside the prompt text. Freeze artifacts for evaluation
are pinned via `.gitattributes` (`eval/* -text`) so EOL conversion can
never shift a hash.

## Tests

```
python -m unittest discover -s tests
python forge.py lint
python nextmsg.py --score fixtures
```

All tests run against a temp brain database; `tests/test_nextmsg.py`
re-scores the full fixture bar every run.

## History

- **v1** (`e044820`, `230e123`) — 12-file CLI: field specs, prompt
  generation, scoring, fixing, comparing, merging, ensembling, chat
  parser.
- **v2** (`c757aca`) — the brain (local SQLite learning), context parser,
  stdlib HTTP server + single-page UI, full test suite. No LLM anywhere.
- **Determinism** (`f41e816`) — `as_of` day-pinning, `brain_hash`
  provenance, the `use_brain` chokepoint.
- **This round** — pivot to the next-message engine: `nextmsg.py`,
  fixtures + pass bar, `/next` page, 156 tests. The builder is now the
  secondary surface; Gate-4 evaluation artifacts are frozen and shelved.

## Scope

- offline by design: no network calls, stdlib only, Python 3.10+
- builder scores are structural (checklist coverage), not a model's
  opinion — verify against a real model output before trusting it
- `brain.db`, `fixtures/*` (real chats), outputs, and `my.txt` are
  git-ignored / untracked local state

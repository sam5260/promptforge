# Fixture schema (v1)

A fixture is one real agent-session cutoff: the transcript up to the moment
you were about to type the next message, plus the ground truth for what the
engine must produce.

Fixtures live in this directory and are **gitignored** except this file and
`PASSBAR.md` — real chats may contain secrets, paths, and names. Redact
before saving. Replace the synthetic seeds with real chats using this exact
shape; the engine never looks at `source`.

```json
{
  "schema": 1,
  "id": "fx01",
  "source": "synthetic-seed | real",
  "redacted": true,
  "transcript": "full chat text up to the cutoff, roles prefixed 'user:' / 'assistant:'",
  "cut_rule": "end",
  "expected_state": "unresolved_error | unverified_work | open_question | stated_next_step | big_new_task | continue",
  "expected_message": "the exact next message you actually typed (or would type)",
  "expected_constraints": ["verbatim rule clauses from your earlier turns that must be carried"],
  "decision": {
    "available": true,
    "resolved_by_turn": 3,
    "choice": "Memcached"
  } | {
    "available": false,
    "resolved_by_turn": null,
    "choice": null
  },
  "violations_expected": [
    {"kind": "ignored_ask | broke_rule", "detail": "..."}
  ],
  "why": "one line: why this is the correct next action",
  "supporting_turn": 2
}
```

Four things every fixture preserves (non-negotiable):

1. what the conversation contained at the cutoff — `transcript` + `cut_rule`
2. what state PromptForge should have reconstructed — `expected_state`
3. what you actually sent next — `expected_message`
4. why that was correct, with the supporting turn — `why` + `supporting_turn`
   (plus `decision.available` with `resolved_by_turn` when a choice existed)

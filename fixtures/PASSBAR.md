# Pass bar — written BEFORE the first scoring run (2026-10-05)

Engine: `nextmsg.py` · fixtures: 10 · baseline: literal `Continue.`

| # | Condition | Threshold | Type |
|---|---|---|---|
| 1 | intent correct (`expected_state` == engine state) | **≥ 7 of 10** | scored |
| 2 | constraints carried (every `expected_constraints` clause present) | **10 of 10** | scored |
| 3 | invented decisions (engine picks where `decision.available == false`) | **0 of 10** | **absolute blocker** |
| 4 | intent score strictly greater than the `Continue.` baseline | required | scored |

- Condition 3 fails → whole run fails, regardless of 1/2/4.
- Violation detection (flagged `ignored_ask` / `broke_rule` on fixtures that
  expect them) is reported alongside, not part of the bar.
- No tuning fixtures after the run starts. A failed run is a result, not a
  prompt to edit the ground truth.

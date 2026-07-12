# PROPOSAL: kill-switch extension for model decay and regime mismatch

**Status: FLAGGED FOR HUMAN REVIEW - NOT IMPLEMENTED.**

`app/risk/kill_switch.py` is on the AGENTS.md protected list, and the
upgrade task (item 34) asks for its check set to be *extended* with two
new flags. Per AGENTS.md, this file was not edited; this document is the
flag.

## Proposed change

Extend `KillSwitchState` with two optional fields, both defaulting to the
safe value:

```python
model_decay_flagged: bool = False       # from app/discovery/decay.py DecayReport.drifted
regime_mismatch_flagged: bool = False   # current regime absent from the candidate's
                                        # positive-expectancy regimes (discovery gates)
```

And extend `evaluate_kill_switch` with, before the existing limit checks:

```python
if state.model_decay_flagged:
    return KillSwitchDecision(flatten_now=True, reason="Model decay: rolling performance drifted below validation baseline.")
if state.regime_mismatch_flagged:
    return KillSwitchDecision(flatten_now=True, reason="Regime mismatch: current regime is outside the candidate's validated set.")
```

## Wiring (already built, waiting on approval)

- `app/discovery/decay.py` produces `DecayReport.drifted`.
- `app/discovery/gates.py` `regime_breakdown()` provides the validated
  positive-expectancy regime set to compare the live regime against.

## Item 35 note (reconciliation)

The protected `app/execution/reconciliation.py` was likewise not touched.
Nothing in this upgrade weakens it; the new
`app/execution/reconciliation_report.py` is a read-only daily reporting
layer beside it. Reinforcement of the pre-order broker-state check
belongs to the future, separately-scoped Tradovate task.

## To apply

Approve this proposal explicitly (create `.protected-change-approved` at
the repo root or say so in a task prompt), and the edit above can be made
with pass/fail tests per the existing risk-module pattern.

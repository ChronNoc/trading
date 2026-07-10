# Project: MNQ Order-Flow Trading Assistant

## What this project is
An explainable strategy-cloning system for one MNQ setup learned from trading videos.
It is NOT an autonomous learning trader. Every decision must be traceable to an explicit,
inspectable rule or a versioned model — never a black-box output that directly places size.

## Non-negotiable rules for every task
- Python 3.11+, full type hints, docstrings on every public function.
- Every module ships with pytest tests. No task is complete without passing tests.
- Use Decimal for all money and price calculations. Never float for P&L, risk, or price.
- No hidden state: functions take explicit inputs and return explicit outputs where possible.
- No network calls, no order submission, and no broker credentials in any module unless
  the task explicitly says "execution" or "broker" — see protected files below.
- The system starts in OBSERVE mode by default everywhere. LIVE mode requires an explicit,
  separate config flag that defaults to false and is never set to true by generated code.
- Log every decision (see decision log schema in strategy/logging.py) — accepted or
  rejected setups both get logged, with the specific reasons.

## Protected files — do not modify without being told explicitly in the task prompt
- risk/limits.py
- risk/sizing.py
- risk/kill_switch.py
- execution/live_execution.py
- execution/reconciliation.py
- config/production_config.yaml

If a task would require touching one of these files and the task prompt didn't say so,
stop and flag it instead of editing it.

## Style
- Prefer small, composable functions over large ones.
- Prefer dataclasses/pydantic models over dicts for any structured state.
- Every "rule" in the strategy engine must be individually testable and individually
  explainable (return which sub-conditions passed/failed, not just true/false).

## Testing
- pytest for everything.
- Any module involving randomness or timing must be deterministic in tests (inject a
  fixed clock / seeded RNG, don't rely on wall-clock time).

## Out of scope unless a task says otherwise
- Anything that submits a real order to Tradovate's live environment.
- Anything that disables or bypasses the risk engine.
- Anything that auto-retrains a live model.

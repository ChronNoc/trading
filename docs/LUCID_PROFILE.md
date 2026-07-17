# Lucid Flex 25K Evaluation Profile

`app/risk/account_profile.py`. The default PAPER account profile. All values are
typed (`Decimal` / `int` / enum), so a rule can never silently become a float.

## Values

| Field | Value |
|---|---|
| Starting balance | $25,000 |
| Profit target | $1,250 |
| Max loss / drawdown room | $1,000 |
| Drawdown method | EOD trailing, then lock |
| Trailing threshold at start | $24,000 (25,000 − 1,000) |
| Trailing threshold at +$1,100 highest close | $25,100 (trails, then locks) |
| Max micro contracts | 20 |
| Effective `max_contracts` (this project trades MNQ micros) | 20 |

The trailing max-loss line follows the highest *closing* balance up to a lock
point and then stops trailing — `trailing_threshold(highest_closing_balance)`
implements exactly this and is unit-tested.

## MNQ economics (used by the paper executor)

| Quantity | Value |
|---|---|
| Tick size | 0.25 |
| Tick value | $0.50 |
| Point value | $2.00 / contract |
| Commission | $1.24 / contract round turn |

## Enforcement in PAPER

The paper executor's `gate()` and `size_intent()` enforce, before any simulated
order:

- risk sizing through `app.risk.sizing.calculate_position_size` (the same engine
  the offline ledger uses), capped at `max_contracts`
- one open position at a time; no duplicate setup occurrence; setup cooldown
- daily entry and daily loss limits; drawdown lockout
- no entry on a stale/crossed book or an unresolved contract

Every rejection carries a stable reason code and human-readable text, shown in
the GUI and recorded in the daily report.

## LIVE vs PAPER

`blocks_broker_arming` is driven by whether the prop-firm rules are fully
resolved. The 12 required Lucid rule fields in `config/prop_rules_lucid.yaml`
remain **unresolved** (web sources were unavailable to verify them), so the LIVE
gate lists this as a failure and LIVE cannot arm. This does **not** affect PAPER:
unresolved LIVE-only rules never disable paper evaluation or simulation.

Verified profile values are printed by `python -m tools.profitability_meter`
context and asserted in `tests/test_paper_account.py`.

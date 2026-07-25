# Daily market learning - 2026-07-11

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 1
- Valid sessions: 0
- Delayed/free-data sessions: 0
- Depth updates: 1861
- Trades: 0

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 0.00 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Blockers

- 1 session(s) were not analysis-clean.
- No trade prints were recorded, so CVD and bubble behavior cannot be learned.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-11/session_20260711T143450Z | live | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; no trades recorded (depth-only) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

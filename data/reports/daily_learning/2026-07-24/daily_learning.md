# Daily market learning - 2026-07-24

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 2
- Valid sessions: 0
- Delayed/free-data sessions: 2
- Depth updates: 4390885
- Trades: 33209

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 0.00 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Blockers

- 2 session(s) were not analysis-clean.
- 2 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-24/session_20260724T172137Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 13478 malformed messages; trade sequence indicates 330637 missing events; 9946 trade-sequence gap(s) |
| 2026-07-24/session_20260724T173131Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: bounded queue overflow; (bridge lifetime drop total: 3258); receiver rejected 109958 malformed messages; trade sequence indicates 2387693 missing events; 83386 trade-sequence gap(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

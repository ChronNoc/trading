# Daily market learning - 2026-07-22

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 2
- Valid sessions: 0
- Delayed/free-data sessions: 2
- Depth updates: 14324975
- Trades: 98800

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 22.50 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Recurring patterns

- Price and CVD aligned in 1 session(s).
- Possible short absorption appeared in 1 session(s).
- Reload behavior appeared in 1 session(s).

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
| 2026-07-22/session_20260722T112312Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 22523 malformed messages |
| 2026-07-22/session_20260722T121843Z | delayed | no | up | up | yes | 291 | 308 | delayed data: review only; active session: closed parts only, partial coverage; not analysis-clean; reload behavior observed; trade sequence indicates 1899640 missing event(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

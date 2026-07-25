# Daily market learning - 2026-07-23

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 1
- Valid sessions: 0
- Delayed/free-data sessions: 1
- Depth updates: 23787693
- Trades: 123241

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 20.00 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Recurring patterns

- Possible short absorption appeared in 1 session(s).
- Reload behavior appeared in 1 session(s).

## Blockers

- 1 session(s) were not analysis-clean.
- 1 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-23/session_20260723T134503Z | delayed | no | down | up | no | 136 | 160 | delayed data: review only; not analysis-clean; price/CVD divergence; reload behavior observed; trade sequence indicates 2744995 missing event(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

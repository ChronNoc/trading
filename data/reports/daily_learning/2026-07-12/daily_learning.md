# Daily market learning - 2026-07-12

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 8
- Valid sessions: 1
- Delayed/free-data sessions: 8
- Depth updates: 3722
- Trades: 0

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 7.50 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Recurring patterns

- Price and CVD aligned in 1 session(s).

## Blockers

- 7 session(s) were not analysis-clean.
- 8 delayed/free-data session(s) cannot be used for live decisions.
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
| 2026-07-12/session_20260712T165200Z | delayed | yes | flat | flat | yes | 0 | 0 | delayed data: review only; no trade prints |
| 2026-07-12/session_20260712T165225Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165225Z_0001 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: WebSocket connection was not acknowledged; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165246Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165246Z_0001 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165246Z_0002 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165246Z_0003 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-12/session_20260712T165246Z_0004 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

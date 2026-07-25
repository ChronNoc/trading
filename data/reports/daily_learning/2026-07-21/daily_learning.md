# Daily market learning - 2026-07-21

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 7
- Valid sessions: 0
- Delayed/free-data sessions: 7
- Depth updates: 11304037
- Trades: 147100

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 2.86 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Recurring patterns

- Possible long absorption appeared in 1 session(s).
- Reload behavior appeared in 1 session(s).

## Blockers

- 7 session(s) were not analysis-clean.
- 7 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-21/session_20260721T012556Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: bounded queue overflow; bridge dropped 2378 messages during this session; receiver rejected 120796 malformed messages; trade sequence indicates 1540522 missing events; 104775 trade-sequence gap(s) |
| 2026-07-21/session_20260721T131608Z | delayed | no | up | down | no | 13 | 25 | delayed data: review only; not analysis-clean; price/CVD divergence; reload behavior observed; trade sequence indicates 165274 missing event(s) |
| 2026-07-21/session_20260721T141935Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; no depth updates; no trades recorded (depth-only) |
| 2026-07-21/session_20260721T142216Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; no depth updates; no trades recorded (depth-only) |
| 2026-07-21/session_20260721T142256Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; no depth updates; no trades recorded (depth-only) |
| 2026-07-21/session_20260721T150211Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 38329 malformed messages; trade sequence indicates 925369 missing events; 26602 trade-sequence gap(s) |
| 2026-07-21/session_20260721T152357Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: bounded queue overflow; bridge dropped 5093 messages during this session; receiver rejected 190707 malformed messages; trade sequence indicates 3286333 missing events; 147904 trade-sequence gap(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

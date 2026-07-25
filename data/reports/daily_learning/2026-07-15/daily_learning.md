# Daily market learning - 2026-07-15

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 12
- Valid sessions: 0
- Delayed/free-data sessions: 12
- Depth updates: 27715171
- Trades: 1239383

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 7.08 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Recurring patterns

- Price and CVD aligned in 1 session(s).
- Possible short absorption appeared in 1 session(s).
- Reload behavior appeared in 2 session(s).

## Blockers

- 12 session(s) were not analysis-clean.
- 12 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-15/session_20260715T002231Z | delayed | no | up | down | no | 0 | 0 | delayed data: review only; not analysis-clean; price/CVD divergence; sellers controlled tape; trade sequence indicates 196 missing event(s) |
| 2026-07-15/session_20260715T002303Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; bridge dropped 10765099 messages during this session |
| 2026-07-15/session_20260715T102920Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: ConnectionClosedError: no close frame received or sent; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T102920Z_0001 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: ConnectionClosedError: no close frame received or sent; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T161759Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; bridge dropped 59739 messages during this session |
| 2026-07-15/session_20260715T175441Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T175613Z | delayed | no | up | down | no | 0 | 2 | delayed data: review only; active session: closed parts only, partial coverage; not analysis-clean; price/CVD divergence; reload behavior observed; trade sequence indicates 7515 missing event(s) |
| 2026-07-15/session_20260715T175857Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: WebSocket connection was not acknowledged; bridge dropped 86211 messages during this session; receiver rejected 1051 malformed messages; trade sequence indicates 1175 missing events; 1050 trade-sequence gap(s) |
| 2026-07-15/session_20260715T180010Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T180010Z_0001 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T180010Z_0002 | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; no depth updates; no trades recorded (depth-only) |
| 2026-07-15/session_20260715T180010Z_0003 | delayed | no | up | up | yes | 0 | 8 | delayed data: review only; active session: closed parts only, partial coverage; not analysis-clean; reload behavior observed; trade sequence indicates 26126 missing event(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

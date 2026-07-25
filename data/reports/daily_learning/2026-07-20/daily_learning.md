# Daily market learning - 2026-07-20

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 6
- Valid sessions: 0
- Delayed/free-data sessions: 6
- Depth updates: 13535211
- Trades: 73071

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 0.00 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Blockers

- 6 session(s) were not analysis-clean.
- 6 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-20/session_20260720T132154Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 284546 malformed messages; trade sequence indicates 7760395 missing events; 183867 trade-sequence gap(s) |
| 2026-07-20/session_20260720T151456Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; bridge dropped 6186 messages during this session; receiver rejected 13002 malformed messages; trade sequence indicates 367947 missing events; 8425 trade-sequence gap(s) |
| 2026-07-20/session_20260720T151944Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: bounded queue overflow; bridge dropped 14903 messages during this session; receiver rejected 108234 malformed messages; trade sequence indicates 2636416 missing events; 77055 trade-sequence gap(s) |
| 2026-07-20/session_20260720T230204Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 147 malformed messages; trade sequence indicates 1495 missing events; 114 trade-sequence gap(s) |
| 2026-07-20/session_20260720T230308Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; legacy active session has no safely closed parts; active recording: skip until finalized; continuity: bounded queue overflow; bridge dropped 152 messages during this session; receiver rejected 5 malformed messages |
| 2026-07-20/session_20260720T231349Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; receiver rejected 74331 malformed messages; trade sequence indicates 1196979 missing events; 60500 trade-sequence gap(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

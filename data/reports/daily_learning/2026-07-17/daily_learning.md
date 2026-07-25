# Daily market learning - 2026-07-17

> Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.

## Data quality and coverage

- Sessions analyzed: 10
- Valid sessions: 0
- Delayed/free-data sessions: 10
- Depth updates: 892991
- Trades: 38583

## Market observations (descriptive, not strategy performance)

- Observation coverage score: 0.00 / 100
- This score describes recording/market features; it is not win rate, expectancy, or profitability.

## Blockers

- 10 session(s) were not analysis-clean.
- 10 delayed/free-data session(s) cannot be used for live decisions.
- No completed quality-gated real strategy outcomes exist for this trading day.

## Strategy performance

- Status: not_available_no_completed_real_outcomes
- Completed real outcomes: 0
- Ledger-eligible outcomes: 0
- No profitability claim is available from this daily observation report.

## Session table

| Session | Source | Valid | Direction | CVD | Alignment | Bid reloads | Ask reloads | Notes |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| 2026-07-17/session_20260717T000846Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T000846Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T000846Z\\session_manifest.json'; receiver rejected 109 malformed messages; trade sequence indicates 109 missing events; 109 trade-sequence gap(s) |
| 2026-07-17/session_20260717T000909Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 5] Access is denied: 'data\\raw\\2026-07-17\\session_20260717T000909Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T000909Z\\session_manifest.json'; receiver rejected 469 malformed messages; trade sequence indicates 471 missing events; 469 trade-sequence gap(s) |
| 2026-07-17/session_20260717T001029Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T001029Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T001029Z\\session_manifest.json'; receiver rejected 558 malformed messages; trade sequence indicates 564 missing events; 558 trade-sequence gap(s) |
| 2026-07-17/session_20260717T001213Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T001213Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T001213Z\\session_manifest.json'; receiver rejected 1506 malformed messages; trade sequence indicates 1558 missing events; 1506 trade-sequence gap(s) |
| 2026-07-17/session_20260717T001517Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T001517Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T001517Z\\session_manifest.json'; bridge dropped 4550 messages during this session; receiver rejected 2673 malformed messages; trade sequence indicates 3241 missing events; 2673 trade-sequence gap(s) |
| 2026-07-17/session_20260717T001751Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; continuity: java.io.IOException: closed output; bridge dropped 4550 messages during this session; receiver rejected 430 malformed messages; trade sequence indicates 584 missing events; 430 trade-sequence gap(s) |
| 2026-07-17/session_20260717T105636Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T105636Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T105636Z\\session_manifest.json'; receiver rejected 87 malformed messages; trade sequence indicates 85 missing events; 85 trade-sequence gap(s) |
| 2026-07-17/session_20260717T105645Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: receiver_error: PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'data\\raw\\2026-07-17\\session_20260717T105645Z\\session_manifest.json.tmp' -> 'data\\raw\\2026-07-17\\session_20260717T105645Z\\session_manifest.json'; receiver rejected 3004 malformed messages; trade sequence indicates 3038 missing events; 3005 trade-sequence gap(s) |
| 2026-07-17/session_20260717T110224Z | delayed | no | down | up | no | 0 | 0 | delayed data: review only; not analysis-clean; price/CVD divergence; trade sequence indicates 720 missing event(s) |
| 2026-07-17/session_20260717T170347Z | delayed | no | unknown | flat | no | 0 | 0 | manifest-only coverage; raw replay skipped because session is invalid; unclean shutdown; continuity: websocket_closed; bridge dropped 57127 messages during this session; receiver rejected 12882 malformed messages; trade sequence indicates 17352 missing events; 12879 trade-sequence gap(s) |

## Safety

- Observe-only learning report. It records data quality and market behavior, but it does not retrain a live model or authorize trading.
- Bookmap delayed/free data is valid for review and threshold research only.
- Manual labels or replay outcomes are required before any supervised model training.

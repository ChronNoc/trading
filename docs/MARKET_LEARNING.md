# Observe-Only Market Learning

The assistant now learns from recorded Bookmap sessions by building causal setup episodes,
outcome labels, and daily observation reports from raw depth and trade data. This is
research/observe-only learning. It does
not auto-retrain a live model, does not submit orders, and does not make delayed-data
sessions live-decision-ready.

## What Gets Learned

Each day report summarizes:

- total depth updates and trades recorded
- source mode, including delayed/free Bookmap mode
- whether the recording was analysis-clean
- aggressive buy/sell volume and CVD direction
- price direction and price/CVD alignment
- large bid/ask blocks
- bid/ask reload behavior
- possible long/short absorption candidates
- recurring patterns and blockers across sessions
- a descriptive observation-coverage score based on clean data, price/CVD alignment,
  reload behavior, and one-sided control; this is explicitly not a performance metric
- a separate strategy-performance section populated only by completed, quality-gated
  real outcomes

## Build Real Episodes

After a Bookmap session is finalized, run:

```powershell
.\.venv\Scripts\python.exe -m tools.build_real_episodes
```

The builder streams closed Parquet files in bounded batches. New recordings replay by
the receiver-local `receive_sequence`; old recordings fall back to timestamp order and
report any same-timestamp ambiguity. It writes:

- `data/processed/{session_id}.decisions.jsonl` for every accepted/rejected checklist
- `data/processed/{session_id}.episodes.jsonl` for accepted setups and outcomes
- `data/processed/{session_id}.build.json` for counts, quality, hashes, and rejection tallies
- `data/labels/{session_id}.labels.jsonl` for target-first/stop-first supervised labels

Empty episode/label files are an honest result when no setup passes. Thresholds and
levels are never loosened merely to populate the GUI.

New receiver sessions flush readable closed parts under `depth_parts/` and
`trade_parts/` while recording. On clean finalization they also produce the compatible
`depth.parquet` and `trades.parquet` files. The WebSocket event schema is unchanged;
`receive_sequence` is local disk provenance added by Python after validation.

## Automatic Reports

When `start_mnq_assistant.bat` is running, each completed Bookmap connection writes:

- `data/reports/{date}/{session_id}/summary.json`
- `data/reports/{date}/{session_id}/decisions.jsonl`
- `data/reports/daily_learning/{date}/daily_learning.json`
- `data/reports/daily_learning/{date}/daily_learning.md`

The daily report is refreshed after every completed session for that UTC recording date.

## Manual Report Command

Run this from the repo root:

```powershell
.\.venv\Scripts\python.exe -m tools.daily_learning_summary --date 2026-07-12
```

Custom roots:

```powershell
.\.venv\Scripts\python.exe -m tools.daily_learning_summary `
  --raw-root data/raw `
  --report-root data/reports `
  --processed-root data/processed `
  --date 2026-07-12
```

## Safety Limits

- Delayed/free Bookmap data is review-only.
- Daily market observations are not supervised labels or strategy performance.
- Ambiguous ordering, malformed events, sequence gaps, and unfinished outcomes are
  excluded from the fixed-account paper ledger.
- A setup is not considered proven just because it appears in a report.
- Manual labels or replay outcomes are still required before supervised training.
- Live Tradovate behavior remains blocked unless a separate reviewed live-execution task
  explicitly changes the protected execution path.

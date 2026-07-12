# Observe-Only Market Learning

The assistant now learns from recorded Bookmap sessions by building daily consistency
reports from raw depth and trade data. This is research/observe-only learning. It does
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
- a consistency score based on clean data, price/CVD alignment, reload behavior, and
  one-sided control

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
  --date 2026-07-12
```

## Safety Limits

- Delayed/free Bookmap data is review-only.
- Daily learning reports are not supervised labels.
- A setup is not considered proven just because it appears in a report.
- Manual labels or replay outcomes are still required before supervised training.
- Live Tradovate behavior remains blocked unless a separate reviewed live-execution task
  explicitly changes the protected execution path.

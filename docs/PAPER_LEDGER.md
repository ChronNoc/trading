# Paper Ledger

`app/paper/ledger.py`. The durable record of every closed simulated trade.

## Format

Append-only JSONL at `data/paper/paper_trades.jsonl` (gitignored — user-owned
trading records). One closed trade per line, produced by `PaperTrade.to_record()`
plus a `schema_version`. Money and prices are **strings**, never JSON floats, so
a value round-trips exactly into `Decimal`.

Each row carries full lineage — `session_id`, `setup_id`, `strategy_version`,
`contract`, `decision_event_index`, `opened_event_index`, `closed_event_index` —
so any trade can be traced back to the exact events that produced it.

## Durability

- **Append-only.** A closed trade is a historical fact; nothing rewrites or
  reorders an existing line.
- **Never silently overwritten.** Opening a ledger reads what is already there
  and continues it. An existing file is user data, not scratch space.
- **Crash-tolerant.** `fsync` on each append. A process killed mid-write can
  leave one torn final line; `recover()` keeps every intact record and reports
  `damaged_tail` rather than discarding the file.

## Recovery

`PaperLedger.recover(path)` returns a `LedgerRecovery`:

- `records` — every intact row.
- `real_records` — rows **not** flagged `is_synthetic_fixture` (what real
  statistics may use).
- `realized_pnl` — summed net P&L of real rows, as `Decimal`.
- `last_balance(default)` — balance after the last real trade, else `default`.

On restart the engine can reconstruct account state from `last_balance` without
reprocessing events, and it never reopens a closed trade or duplicates a row.

## Synthetic quarantine

Deterministic test fixtures are stamped `is_synthetic_fixture: true`, rendered
`[FIXTURE]` in the GUI, and excluded from `real_records`, `realized_pnl`, and the
daily report's statistics. **A fixture trade is never evidence of profitability.**

## Separation of ledgers

| Ledger | Path | Purpose |
|---|---|---|
| Canonical paper | `data/paper/paper_trades.jsonl` | real simulated trades from the live delayed stream |
| Deterministic test | pytest `tmp_path` | fixtures; never touches the canonical file |
| Research/experimental | `data/research_state/ledgers/` | offline candidate evaluation, kept separate from canonical |

## Reporting

`app/paper/daily_report.py` reads the ledger via `recover()` and computes the
daily digest from `real_records` only (win rate, expectancy, peak-to-trough max
drawdown, worst consecutive-loss streak, per-close-reason breakdown). It is
written automatically on session finalize and on demand via
`python -m tools.paper_daily_report`.

Tested in `tests/test_paper_ledger.py` and `tests/test_paper_daily_report.py`.

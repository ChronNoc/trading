# Strategy Discovery Pipeline

One command runs the whole search and ends at the validation gate:

    .venv\Scripts\python.exe -m tools.run_discovery

## What happens

1. Episodes load (SYNTHETIC generator until real Stage C data exists -
   every artifact and report is marked synthetic).
2. The most recent 20% of dates is sealed in a holdout vault that can be
   read exactly once; a second read raises.
3. The rest is split into rolling walk-forward train/validate windows.
4. A deduplicated parameter grid runs on parallel workers (capped count,
   capped time, deterministic per-worker seeds, full per-worker traces
   under data/discovery/worker_traces/).
5. Every candidate faces the gates, each with an explicit reason:
   - label leakage (features must be decision-time only)
   - minimum sample size (default 100 trades)
   - regime/session robustness (no narrow-bucket performance)
   - Monte Carlo resampled drawdown / risk-of-ruin
   - parameter sensitivity (fragile optima rejected)
   - worst-case cost pass (3x slippage+commission)
6. Accepted AND rejected candidates land in data/discovery/candidates.jsonl
   with reasons - same philosophy as the trade decision log.
7. The leader is scored once on the holdout, compared against any previous
   best (shadow comparison), and a recommendation.md is written.
8. **THE SYSTEM STOPS.** No flag flips, no broker connection, ever.
   Going live means a human edits config/production_config.yaml and
   approves it: `python -m app.runtime.config_guard approve`.

## Supporting pieces

- `app/discovery/decay.py` - decay monitor for a deployed-in-sim leader.
- `app/discovery/supervisor.py` - read-only OBSERVE/LIVE view for workers.
- `app/execution/dry_run.py` - intended-order journal; no transport exists.
- `app/execution/circuit_breaker.py` - latency circuit breaker, manual reset.
- `app/execution/reconciliation_report.py` - daily sim-vs-actual fill report.
- `app/runtime/fresh_sizing.py` - sizing recomputed at every decision.
- `app/runtime/config_guard.py` - production config change alert/approval.
- `tools/check_protected_files.py` - CI enforcement of AGENTS.md protected list.
- `docs/PROPOSAL_kill_switch_extension.md` - flagged (not edited) kill-switch
  extension for model decay + regime mismatch.

## GUI

- Unmissable SIM/LIVE banner + "LIVE ARMED: NO/YES" readout (display-only;
  the Arm LIVE button is permanently disabled with instructions).
- Decision log tab: filter by accepted/rejected and regime; click any row
  for the full why-trace.
- Leaderboard tab: every composite component per candidate (never just a
  win-percent column); click a row for gates and rejection reasons.

## Rate limiting

Full searches are rate-limited (default 1 run/hour, persisted in
data/discovery/.last_search_run). `--skip-rate-limit` exists for tests.

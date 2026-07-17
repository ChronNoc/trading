# Implementation state — final automation/GUI/Lucid task

Durable checkpoint. Base commit `0d4e33e`; work landed in `0e6d78e` and follow-ups.

## DONE (verified by tests, not by assertion)

1. **Repo hygiene** — 426 tracked `pytest-clean-*` files untracked (kept on disk),
   pattern ignored, `tests/test_repo_hygiene.py` guards temp trees, `.env`, and a
   repo-relative pytest basetemp.
2. **`BoundedIntakeBuffer`** — overflow now displaces exactly ONE frame; gap
   markers and the close sentinel are out-of-band so they can neither consume
   capacity nor be miscounted as lost market data. `delivered + lost == sent`.
3. **`RecorderPipeline`** — per-event bounded retry instead of discarding a whole
   batch on one exception; unwritable events counted as real loss, pushed to the
   session manifest, and `finalize()` **fails closed** (a segment that lost
   anything can never be clean).
4. **`session_catalog`** — order-flow eligibility requires clean shutdown,
   continuous data, depth AND trades, and zero session-drops/overflow/malformed/
   rejected/missing/sequence-gaps. Java **lifetime** drop total is distinguished
   from per-session loss; a missing per-session figure fails closed. Verified on
   real data: all 80 finalized sessions with >1k drops (incl. 1.03M and 10.7M)
   are ineligible; the one clean session still qualifies.
5. **Typed Lucid Flex 25K profile** (`app/risk/account_profile.py`) — Decimal/int/
   enum/tz-aware fields, EOD trailing→lock mechanics, consistency, contract cap,
   provenance (URL + retrieval/effective date + excerpt hash). Project safety
   limits always bind as the stricter rule.
6. **Hardcoded $100,000 removed** — paper ledger, service ledgers, and the GUI
   banner all use the selected profile; sizing capped at the profile's limit.

## NOT DONE — outstanding, do not claim these

- **GUI redesign** (sidebar nav, 8 screens, status bar, 1366x768 fit, model/view
  tables, themes, screenshots at 3 resolutions). `app/gui/main_window.py` is still
  the ~2.9k-line 13-tab window. This is the largest remaining item.
- **GUI off-thread work** — `refresh_live_dashboard()` still runs on a 1 s timer
  and uses `findChild` tree walks per label.
- **Process isolation** — receiver/recorder still share the GUI process (research
  already runs in child processes; the 2 s starvation loop was fixed in `0d4e33e`).
- **Lifecycle supervisor** — staged startup/heartbeats/coordinated shutdown.
- **Session rotation** while connected; delayed paper engine; catch-up jobs.
- **Stress test ≥1650 ev/s** with GUI+research load; soak command.
- **Crash diagnostics** — faulthandler + structured rotating logs.
- **Daily summary** automation.

Next actor: start with the GUI redesign as a NEW window class alongside the
existing one (additive, so a partial rewrite can never break the working app),
then move `refresh_live_dashboard` to snapshot-only updates.

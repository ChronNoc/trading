# Implementation state — final automation/GUI/Lucid task

Durable checkpoint. Update as work lands. Base commit: `0d4e33e`.

## Order of work (priority)

1. **[in progress] Repo hygiene** — untrack 426 `pytest-clean-*` artifacts, ignore them,
   add a regression guard so tests can never write temp trees into the repo.
2. **[in progress] Typed Lucid Flex 25K profile** — replace prose fields with typed
   Decimal/int/enum/tz fields; make it the selected default; remove the hardcoded
   `$100,000` canonical account so paper/risk/GUI all use the selected profile.
3. **[todo] Named defects**
   - `BoundedIntakeBuffer`: full-queue path can evict >1 item per frame and can
     count control markers as lost data.
   - `RecorderPipeline`: batch write exception loses the whole batch (counter only)
     — needs retry/fail-closed + manifest propagation.
   - `session_catalog`: require clean shutdown, continuous, zero current-session
     drops, zero overflow, zero malformed/rejected/missing, depth AND trades.
4. **[todo] GUI off-thread** — `refresh_live_dashboard()` (1 s) must not do disk I/O,
   automation, replay rescans, or full table rebuilds. Snapshots only + model/view.
5. **[todo] GUI redesign** — sidebar nav (Overview / Live Order Flow / Paper Trading /
   Sessions & Replay / Research & Model Health / Risk & Lucid Account / Execution /
   Diagnostics & Settings), status bar, 1366x768 fit, screenshots.
6. **[todo] Process isolation** — receiver+recorder in a supervised child process;
   bounded IPC with coalesced snapshots; lifecycle supervisor with graceful drain.
7. **[todo] Automatic workflow** — session rotation (America/New_York, 15-min delay),
   delayed paper engine, catch-up jobs on restart, daily report.
8. **[todo] Stress test** ≥1650 ev/s with GUI+research load; screenshots; Java suite.

## Honest status

Nothing in sections 3–8 is claimed until its tests pass. See the final response for
what is verified vs. outstanding — do not infer completion from this file.

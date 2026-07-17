# Implementation state

Base `0d4e33e`. Landed: `0e6d78e`, `8d032af`, `5e5f0e2`, `300d4fa`, and the
forwarder-shutdown work below.

## DONE (verified by tests)

- **Repo hygiene** — 426 tracked `pytest-clean-*` files untracked; guard test.
- **BoundedIntakeBuffer** — one overflow displaces exactly one frame; gap markers
  and the close sentinel are out-of-band; `delivered + lost == sent`.
- **RecorderPipeline** — per-event bounded retry; lost events reach the manifest;
  `finalize()` fails closed.
- **session_catalog** — eligibility requires clean shutdown, continuity, depth AND
  trades, zero session-drops/overflow/malformed/rejected/missing/gaps. Lifetime vs
  per-session drops separated; missing figure fails closed. All 80 real sessions
  with >1k drops (incl. 1.03M, 10.7M) are ineligible.
- **Typed Lucid Flex 25K** (`app/risk/account_profile.py`) — Decimal/int/enum/tz
  fields, EOD trail→lock, provenance + excerpt hash. Fabricated $100k removed
  everywhere (ledger, service, GUI banner); sizing capped at the profile limit.
- **Bookmap semantics verified** (`docs/bookmap_api_verification.md`) —
  `isBidAggressor==true` ⇒ BUY (proved from `Bar.addTrade` bytecode); simplified
  `onTrade` price is pip-denominated (no `dmul` on the dispatch path). CVD is not
  inverted; the single `pips` multiply is correct. Pinned by tests.
- **GUI redesign (Phase 1)** — `app/gui/{view_models,screens,app_window,
  snapshot_source}.py`: 8-screen sidebar window, snapshot-only rendering, retained
  widgets, plain-language status bar, themes/scale/reset, honest capability
  labelling. **It is now the launched GUI** (`tools/start_assistant.py`); a test
  asserts the legacy 13-tab window is no longer constructed. 23 offscreen tests
  incl. a no-file-I/O-on-refresh guard and a no-dead-space budget.
- **Forwarder shutdown (Phase 5, partial)** — `ForwarderRuntime.close()` used to
  flip `running=false`, enqueue `session_ended` into an undrained queue, then
  `shutdownNow()`: the terminal marker was never delivered. Now: bounded graceful
  drain, terminal marker actually delivered, **unclean** reported when the drain
  or the marker send fails, idempotent. 3 new Java tests (22 total).

## NOT DONE — outstanding

- Process isolation / lifecycle supervisor (Phase 2): receiver still runs as a
  daemon thread in the GUI process.
- Streaming delayed-paper engine (Phase 3): mode is still recording-only.
- Session rotation + restart catch-up (Phase 4).
- Forwarder protocol version / stream id / capability handshake / batching
  (rest of Phase 5).
- Daily report automation (Phase 7); crash diagnostics.
- Stress test ≥1650 ev/s with GUI+research load; 30-min soak.
- `docs/BOT_SURVEY_50.md`, `docs/PRIORITIZED_IMPROVEMENTS_200.md` (Phase 9).

## Environment limits

- Offscreen QPA reports **zero font families**, so test screenshots render tofu
  glyphs. Geometry/clipping/dead-space are still verified; use
  `python -m tools.capture_gui_screenshots` on a machine with fonts for readable
  images.

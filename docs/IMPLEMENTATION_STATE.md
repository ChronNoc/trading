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

- **Streaming delayed-paper engine** (`app/paper/streaming_engine.py`) — FIXED the
  regression where delayed data disabled all evaluation. Causal, auto-started by
  the launcher, shares the offline strategy, bounded visible warm-up, full
  lineage, no-broker proof, capability gating. Session id/contract no longer
  "unknown". See `docs/AUTOMATIC_PAPER_PIPELINE.md`. 12 tests + acceptance check
  `delayed_paper_evaluates_live_stream`.

- **Paper ORDER simulation** (`app/paper/{models,execution,ledger}.py`, `7795d8b`)
  — the full causal lifecycle: accepted setup → candidate → risk → simulated
  order → causal fill → managed position → stop/target/time-stop exit → P&L and
  costs → append-only ledger → GUI. An order is fillable only from
  `event_index + 1`; stop+target in one event books at the STOP as `AMBIGUOUS`;
  a gap through the stop fills at the gapped price. Fills take the opposing side
  of the book and snap to the 0.25 grid, because mid+slippage produced prices
  MNQ cannot trade (`29500.875`). See `docs/PAPER_EXECUTION_MODEL.md`.
- **Clean shutdown** (`app/runtime/shutdown.py`, `2e8456b`) — the receiver was a
  `daemon=True` thread awaiting `asyncio.Future()`, so process exit killed it and
  its drain `finally` never ran. The GUI now requests a stop, capture drains, and
  an incomplete drain is reported. Verified against the real receiver.
- **MNQ rollover computed** (`57c4b96`) — third Friday of Mar/Jun/Sep/Dec, front
  contract's last day = expiry − 8 days. Reproduces all 5 hand-verified table
  periods exactly. Previously raised `ValueError` past 2027-03-11.
- **Profitability meter in the GUI** (`6506c06`) — computed on the research
  thread via `ProgressCache` and only *read* by Qt; a test forbids the GUI from
  importing the disk-reading loader (that call on the Qt timer was the freeze).
  Plus `tools/profitability_meter.py`.
- **Recorder atomic writes** (`7cd57af`, `6b0fcef`) — a FIXED `.tmp` name let
  writers collide (destroyed 31 real sessions); temp paths are now unique per
  writer and renames retry through Windows' transient handle contention.
- **Crash/hang diagnostics** (`app/runtime/diagnostics.py`, `6b0fcef`) —
  faulthandler, main/worker/asyncio excepthooks, rotating logs, and a
  `StallWatchdog` that dumps every thread's stack when the GUI stops heartbeating.
- **Stress ≥1650 ev/s** — measured **26,160 ev/s**, 0 drops (15.9× headroom);
  `tests/test_recorder_load.py` asserts a 5,000 ev/s floor.
- **Test/user-data isolation** (`1346e3f`) — the suite was appending ~291 rows
  per run to the tracked `data/prototype/automation/decisions_log.csv`; an
  autouse `tests/conftest.py` guard now sandboxes GUI output roots.

## NOT DONE — outstanding

- Process isolation / lifecycle supervisor (Phase 2): the receiver still runs as
  a thread in the GUI process. It is no longer killed at exit (`2e8456b`), but it
  is not a separate process.
- Session rotation + restart catch-up (Phase 4).
- Forwarder protocol version / stream id / capability handshake / batching
  (rest of Phase 5).
- Typed feed-capability model (currently a `dict[str, bool]` passed to the
  paper engine).
- Daily report automation (Phase 7).
- 30-minute soak under combined GUI + research load (throughput is proven; a
  long-duration soak is not).
- Tradovate DEMO wiring (LIVE stays locked; `live_enabled` false; 12 Lucid
  prop-rule fields unresolved).
- `docs/BOT_SURVEY_50.md`, `docs/PRIORITIZED_IMPROVEMENTS_200.md` (Phase 9).

## Measured bottleneck to profitability evidence

Of **200 finalized sessions only 1 is order-flow-eligible**: 192× unclean
shutdown, 145× continuity `receiver_error` (31 of them the temp-file race), 71×
depth-only, 11× no depth. The two dominant causes are fixed, so new recordings
should convert far better — but that is a **prediction, not proven**: it needs
new Bookmap sessions. The meter is honestly **6.7%**, `profitable_claim_supported
= False`, **0 completed setups on real data**. See
`reports/FINAL_EVIDENCE_PACK.md`.

## Environment limits

- Offscreen QPA reports **zero font families**, so test screenshots render tofu
  glyphs. Geometry/clipping/dead-space are still verified; use
  `python -m tools.capture_gui_screenshots` on a machine with fonts for readable
  images.

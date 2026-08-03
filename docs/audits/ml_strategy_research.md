# ML / Strategy / Risk Research Audit (Phase 3)

Status: read-only audit. No repository code was changed to produce this document.
Author: `ml-strategy-researcher` subagent (dispatched by the lead agent), findings
independently re-verified by the lead agent via Serena and direct source reads
before being accepted into this document. HEAD at audit time: `bf65824c` (working
tree has extensive uncommitted GUI/ML changes not reflected in the graphify graph
used for scoping).

## Objective

Determine whether — and how — an autonomous ML/strategy/risk research subsystem
can be added on top of the existing pipeline while preserving the mission's
non-negotiable boundary: dataset → walk-forward → challenger → registry →
explicit human approval → loader, with no path that can alter safety limits or
reach live execution. See `docs/GOAL_C_AUTONOMOUS_MISSION.md:52-63`.

## 1. Current implementation (high confidence, independently re-verified)

- Dataset construction is deterministic and content-addressed, restricted to
  finalized model-eligible sessions, and aborts if source files change
  (`build_challenger_dataset`, `app/machine_learning/challenger_pipeline.py:89-212`;
  `classify_manifest`, `app/research/session_catalog.py:57-223`).
- Rows use causal features, sample long/short every 30s after warm-up, resolve
  triple-barrier outcomes only from later observations, record
  `label_resolved_timestamp_ns`, and drop unresolved end-of-session outcomes
  (`build_session_training_rows`, `app/machine_learning/session_training.py:93-174`;
  `resolve_barrier_detail`, `app/machine_learning/triple_barrier.py:54-87`).
- Walk-forward fitting uses only prior trading days and purges labels unresolved
  before the test day (`walk_forward_evaluate`,
  `app/machine_learning/pooled_training.py:95-193`).
- Passing gates create an immutable full-data logistic-regression challenger and
  registry record but do **not** approve it (`build_validated_challenger`,
  `app/machine_learning/challenger_pipeline.py:215-398`; `register_challenger`,
  `app/machine_learning/registry.py:209-220`).
  - **Independently re-verified**: `find_referencing_symbols` on
    `build_validated_challenger` shows its only production caller is
    `tools/train_pooled.py:24,48` — never called from `ResearchService` or any
    autonomous path. Its other references are all tests. Confirms this is a
    manually-invoked training script today, not something continuous research
    can trigger without new wiring.
- Exact artifact ID plus SHA approval is enforced at
  `app/machine_learning/registry.py:252-293`; the loader fails closed on
  invalid, stale, or corrupt evidence
  (`ObserveOnlyModelLoader.refresh`, `app/machine_learning/shadow_predictor.py:254-351`).

## 2. Primary scientific finding (high confidence)

Offline validation does not measure deployed paper-policy utility.

- Walk-forward evaluation applies a fixed **0.5** probability threshold to every
  regularly-sampled long/short row and compares against "take every row" using
  fixed target/stop/cost expectancy
  (`app/machine_learning/pooled_training.py:145-193`).
- Runtime instead applies an explicitly non-backtested **0.35** threshold, only
  as a veto on heuristic-accepted setups
  (`ML_VETO_PROBABILITY_THRESHOLD = 0.35`, `app/paper/streaming_engine.py:73`,
  independently confirmed by direct read; docstring at lines 68-72 states this
  value is "documented here, not derived from any backtest"; applied in
  `DelayedPaperEngine._apply_ml_policy`, `app/paper/streaming_engine.py:624-726`).

**Consequence**: `beats_baseline=True` from walk-forward evaluation is
classifier/filter evidence only. It is not evidence that the actual deployed
heuristic-plus-0.35-veto paper strategy improves net P&L, fill rate, drawdown,
or safety. Any autonomous system that treats walk-forward pass/fail as "the
strategy works" would be measuring the wrong thing.

## 3. Statistical validity findings

- Folds are causal and same-trading-day sessions stay together, but there is no
  untouched final chronological test vault or nested-selection protocol — a
  repeated autonomous search would reuse the same walk-forward history as a de
  facto validation set, enabling silent overfitting to it.
- 30s samples with 300s horizons overlap; both directions are emitted at the
  same timestamp; predictions are aggregated per fold. 50 OOS rows are **not**
  50 independent opportunities — busy days dominate the sample.
- No uncertainty intervals, day/session/side concentration limits, effective-
  sample-size correction, regime floors, or multiple-testing adjustment exist.
- Aggregate class presence is gated, but class balance by fold/regime is not.
- `calibration_check` computes Brier score/ECE
  (`app/machine_learning/validation.py:173-220`) but — **independently
  re-verified** via `find_referencing_symbols` — its only reference anywhere in
  the repository is `tests/test_machine_learning.py:130`. It is not called by
  `build_validated_challenger` or any promotion gate. Calibration is measured
  in tests only, never enforced in production promotion.

## 4. Trading-utility and regime findings

- Walk-forward expectancy assumes a fixed 12-tick target, 8-tick stop, 2-tick
  cost, and never invokes paper fill simulation, queue position, latency,
  dynamic stop handling, entry/loss caps, or heuristic acceptance/rejection.
- `EpisodeConfig` in `app/research/episode_builder.py:50-177` carries richer,
  separate assumptions (1-tick slippage, $1.24 round-turn commission, zero
  queue fraction, 900s timeout) used for strategy replay — this is a distinct
  outcome contract from the pooled ML labels, and the two are not reconciled.
- No required evaluation exists by volatility/liquidity regime, RTH block,
  long/short side, capture provenance, or rolling age of the training data.
- Strict quality filtering reduces corrupted-data exposure but may select
  unusually clean sessions; contract-roll and broader survivorship effects were
  not verified in this pass.

## 5. Continuous-operation / restart-safety findings (high confidence, independently re-verified)

- `ResearchService` is a sound precedent for receiver-priority throttling and
  atomic claims, but it performs autonomous **strategy replay** only. Serena
  confirms no call path from `ResearchService` to `build_validated_challenger`.
- **Confirmed restart-safety defect** (independently re-read at
  `app/research/research_service.py:453-465`): `run_batch` calls
  `checkpoint.save(self.checkpoint_path)` (line 463) — which marks jobs
  complete — **before** `self._persist_result_setups(results)` (line 465) writes
  the actual evidence to disk. A crash between these two calls leaves a job
  permanently marked complete in the checkpoint with no corresponding evidence
  file ever written, and it will never be retried. This is a real defect
  confirmed by direct source read, not a subagent-only claim.
- Additional fragility, confirmed present but not exhaustively stress-tested
  this pass: a single shared checkpoint `.tmp` path
  (`ResearchCheckpoint.save`, `app/research/auto_research.py:450-455`);
  heartbeats recorded only after futures complete
  (`run_batch`, `research_service.py:355-475`); fixed result/error temp paths;
  eager process-pool submission with blocking shutdown; a 10-second thread join
  on stop (`ResearchService.stop`, `research_service.py:774-779`); an ambiguous
  `"unsigned"` source-signature fallback
  (`ResearchService._default_signature`, `research_service.py:269-294`); and no
  verified compute/memory/deadline/disk/retention/artifact-count/candidate-
  search budget enforcement.

## 6. Falsifiable pre-registered evaluation protocol (proposed, not implemented)

Before any autonomous candidate can be treated as informative:

1. Run cheap baselines first: constant-prior, rolling-prior, direction/time-only
   logistic, unchanged heuristic, and the previous approved artifact.
2. Evaluate the **exact runtime policy** — heuristic-accepted setups only, 0.35
   veto, the same feature correlation window, through the paper execution
   simulator — not the pooled 0.5-threshold offline metric.
3. Reserve an untouched chronological test period *before* looking at results.
4. Require ≥20 independent trading days and ≥100 deduplicated heuristic-accepted
   setups, no single day contributing >20% of the sample.
5. Require a positive lower-95%-CI day-block-bootstrap bound on incremental net
   expectancy after stressed costs, positive Brier skill vs. constant prior,
   preregistered ECE/calibration limits, and no material drawdown/side/regime
   degradation.
6. Reject on sign reversal under +1/+2 tick slippage stress, concentration in
   one day/side/regime, unstable calibration, or failure to beat the unchanged
   heuristic.

Operational (crash/duplication) acceptance criteria: inject crashes between
evidence publication and checkpointing, run jobs past the stale-lease interval,
attempt duplicate service starts, fill disk, interrupt process pools. Accept
only if every successful checkpoint has integrity-valid evidence, no job
publishes twice, retries are bounded, and receiver-health throttling remains
effective throughout.

## 7. Evidence inventory

Files read (subagent + lead-agent independent re-reads):
`graphify-out/GRAPH_REPORT.md`, `docs/GOAL_C_AUTONOMOUS_MISSION.md`,
`docs/AUTONOMOUS_HANDOFF.md`, `docs/audits/runtime_architecture.md`,
`app/machine_learning/challenger_pipeline.py`, `session_training.py`,
`feature_contract.py`, `pooled_training.py`, `validation.py`, `registry.py`,
`shadow_predictor.py`, `train.py`, `triple_barrier.py`,
`app/research/episode_builder.py`, `session_catalog.py`, `auto_research.py`,
`research_service.py`, `profitability_progress.py`, `app/gui/snapshot_source.py`,
`app/paper/streaming_engine.py`, `tools/start_assistant.py`, plus
`tests/test_pooled_training.py`, `test_challenger_pipeline.py`,
`test_model_registry.py`, `test_research_service.py`, `test_ml_safety_audit.py`,
`test_end_to_end_ml_integration.py`, `test_machine_learning.py`,
`test_gui_pipeline.py`, `test_order_lifecycle_and_fills.py`,
`test_session_lifecycle.py`.

Serena verification performed by the lead agent (this segment):
`find_referencing_symbols(build_validated_challenger)` →
`tools/train_pooled.py` + tests only, no `ResearchService` caller;
`find_referencing_symbols(calibration_check)` → `test_machine_learning.py` only.
Direct reads of `research_service.py:355-580` and `streaming_engine.py:60-139`
confirmed the checkpoint/persist ordering defect and the 0.35 veto-only
threshold verbatim.

Graphify queries run this segment (post-notification, before writing this
document): `"ResearchService run_batch checkpoint persist result setups order"`,
`"ML_VETO_PROBABILITY_THRESHOLD usage streaming_engine apply ml policy"`.

## 8. Blockers / open questions

Zero real model-training-eligible sessions and zero real training rows exist
under the current contract at audit time; there is no real challenger,
approval, or loaded model in production. No profitability or edge claim can be
made or is made by this document. External web search failed during the
subagent's run (tool-provider/HTTP error) and was not relied upon — all
findings above are sourced from repository code and tests only.

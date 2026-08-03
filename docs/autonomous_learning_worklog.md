# Autonomous Learning Worklog

Branch: `feature/automatic-runtime`. Continuous log, updated during execution (not reconstructed after).

## Task Board

| ID | Team | Status | Owner |
|---|---|---|---|
| 1 | Architecture & integration | VERIFIED_COMPLETE | coordinator |
| 2 | Historical data & dataset | VERIFIED_COMPLETE | team2-dataset |
| 3 | Training & model lifecycle | FAILED_REQUIRES_REWORK | team3-training |
| 4 | Runtime inference | VERIFIED_COMPLETE | team4-runtime |
| 5 | Safety & fallback | VERIFIED_COMPLETE | team5-safety |
| 6 | Evaluation & reporting | VERIFIED_COMPLETE | team6-reporting |
| 7 | Testing & verification | VERIFIED_COMPLETE | team7-verification |

## Phase 0 — Orientation (coordinator)

**Time:** session start, 2026-07-26.

**Serena calls made:**
- `get_symbols_overview` on `app/runtime/controller.py`, `app/machine_learning/{outcome_journal,triple_barrier,shadow_predictor,session_training,registry,train,feature_contract,predict,challenger_pipeline,pooled_training,daily_learning,validation}.py`
- `find_symbol` (include_body) on `AutomaticRuntimeController.{record_setup_decision,snapshot,handle_market_event,handle_market_state_snapshot}`, `RuntimeSnapshot`, `ObserveOnlyModelLoader.{__init__,score}`, `ObserveOnlyFeatureSink`, `ShadowPrediction`, `PredictionJournal`, `predict_with_model`, `PredictionResult`, `DelayedPaperEngine.ingest`
- `find_referencing_symbols` on `record_setup_decision` — only test callers and `tools/start_prototype.py::_record_setup_if_window_complete` reference it. **No caller in the real headless assistant path (`tools/start_assistant.py::run_headless_assistant`) invokes `record_setup_decision` at all.**

**Graphify calls made:**
- `graphify query "how does market data flow from Bookmap through the receiver into AutomaticRuntimeController and into machine learning training and inference"`
- `graphify query "machine_learning module: session_training, shadow_predictor, feature_contract, model_registry, outcome_journal, triple_barrier relationships"`
- `graphify query "MarketSessionRecorder output directory and session_catalog discovery of finalized sessions"`
- `graphify query "record_setup_decision callers"` (context_filter=call) — confirms only `.would_submit()` is called from inside; no external caller edge found in the call-context slice either.

**Key architecture findings (Team 1, in progress):**

1. **`record_setup_decision` is never called by the real runtime.** `tools/start_assistant.py::run_headless_assistant` wires `controller.handle_control_event`, `controller.handle_prebuilt_state`, `controller.health.*`, and `controller.finalize_session_report`, but nothing calls `evaluate_day_trading_plan(...)` and feeds the result into `controller.record_setup_decision(...)`. Only `tools/start_prototype.py` (synthetic scenarios) and tests call it. This means the "shadow decision" path documented in `docs/CURRENT_STATE_TRUTH.md` as connected to `AutomaticRuntimeController` is **not actually reached by the live/headless assistant** — setups are evaluated in `app/paper/streaming_engine.py::DelayedPaperEngine` and `app/research/episode_builder.py` (both call `evaluate_day_trading_plan` directly) but neither feeds `AutomaticRuntimeController.record_setup_decision`.
2. `record_setup_decision` already has a `model_version` field in its decision dict, hardcoded to `None` (`app/runtime/controller.py:326`). No `confidence`, `raw model output`, `decision_source`, `fallback_reason`, or `policy_version` fields exist yet — required by Team 4/task spec.
3. The ML shadow-scoring path (`ObserveOnlyFeatureSink` -> `ObserveOnlyModelLoader.score` -> `PredictionJournal`) is real, wired into `tools/start_assistant.py::run_headless_assistant`, and (as of the uncommitted diff already in the working tree before this session) now also feeds `PendingOutcomeTracker` (`app/machine_learning/outcome_journal.py`) for causal outcome resolution — this is genuine progress already present in the working tree (uncommitted). But this path is **entirely parallel to and disconnected from** `AutomaticRuntimeController`'s decision recording. The model produces predictions into a journal; it does not influence `record_setup_decision`, `DelayedPaperEngine`, or any accept/reject decision.
4. **This is the critical gap the task requires closing**: "A saved or loaded model that does not influence decisions does not count" / "Prove model output changes or can change shadow decisions under validated conditions." Team 4's job is to make `AutomaticRuntimeController.record_setup_decision` (or an equivalent real call site reached by `run_headless_assistant`) consult the `ObserveOnlyModelLoader`'s latest scored prediction and record `decision_source ∈ {ML, HEURISTIC, BLENDED, FALLBACK}`.
5. Real recorded sessions exist: `data/raw/<date>/session_*/session_manifest.json` — 250 session directories across 2026-07-11 through later dates. `app/research/session_catalog.py::build_catalog` scans these and classifies eligibility (`eligible_for_model_training`, etc.) — this is the real discovery mechanism (not synthetic).
6. Existing per-session model artifacts: `data/models/per_session/session_*/report.json` (132 dirs) — these are **descriptive/manual per-session reports**, per `docs/CURRENT_STATE_TRUTH.md`: "Per-session training remains descriptive... per-session models are not registry candidates." No registry.json / challenger artifact exists yet on disk (`data/models` has only `per_session/`) — pooled/challenger training has not actually been run to produce a registered artifact in this checkout.
7. `config/model_approval.yaml` is at the safe default: no artifact approved, runtime_loading_enabled=false, shadow_scoring_enabled=false. This conflicts with the working-tree diff wiring `ObserveOnlyModelLoader`/`outcome_tracker` into `start_assistant.py` — that wiring is present but currently inert (no approved artifact => loader stays `NOT_APPROVED`, never scores) until Team 3 trains+registers a challenger and a human-equivalent (autonomous, per this task's authority) approval step sets `model_approval.yaml`.
8. `app/machine_learning/predict.py::predict_with_model` is confirmed **present but disconnected** exactly as `docs/CURRENT_STATE_TRUTH.md` states — no caller outside `app/machine_learning/predict.py` itself and its test.

**Decision:** Proceed team-by-team as scoped. Team 2/3 first (need a real registered artifact + approval before Team 4's wiring can be end-to-end verified with actual scoring, not just plumbing). Team 1 continues architecture documentation in parallel via this log. Running full test suite in background to get a true baseline before any edits (task b2dhm9bn9).

**Next action:** await baseline pytest result; then dispatch Team 2 (dataset audit against real `data/raw` sessions) and Team 3 (run pooled training to produce a real versioned artifact) concurrently as subagents.

## Phase 1 — Team 2 & Team 3 dispatched (coordinator)

**Time:** continuing same session, post-compaction resume, 2026-07-26.

**Actions taken:**
- Wrote `docs/AUTONOMOUS_HANDOFF.md` per the mandatory pre-compaction-resilience requirement.
- Confirmed `tools/train_pooled.py` is a real CLI wrapping `app/machine_learning/challenger_pipeline.py::build_validated_challenger` (full body read via Serena `find_symbol`, include_body=true): builds dataset → `walk_forward_evaluate` → 5 objective gates (`has_rows`, `both_label_classes`, `minimum_oos_predictions`, `minimum_evaluated_days`, `positive_incremental_expectancy`) → on PASS, fits `LogisticRegression`, writes immutable joblib bundle to `data/models/challengers/<artifact_id>/`, writes `validation.json`+`bundle_manifest.json`, registers via `register_challenger`. On REJECT, writes an attempt report under `data/models/attempts/` and returns without publishing. This is genuine, not a stub.
- `TaskUpdate`: Task #3 (Team 3) → in_progress, owner `team3-training`.
- Dispatched **Team 2** (`Agent` tool, general-purpose, background) — dataset pipeline audit/hardening against real 250 sessions in `data/raw/`: session discovery correctness, leakage audit of `triple_barrier.py`/`session_training.py`/`feature_contract.py`, schema validation, determinism/fingerprint proof, walk-forward split date-separation check, missing-value handling. Told to use a distinct `--basetemp=.pytest-tmp-team2` to avoid clashing with Team 3's concurrent pytest run. Agent ID `a2200235f06011968`.
- Dispatched **Team 3** (`Agent` tool, general-purpose, background) — run `python -m tools.train_pooled` for real against `data/raw`, diagnose/honestly-tune if gates reject, produce (or honestly fail to produce) a real registered challenger artifact with complete metadata, prove unseen-date evaluation via `walk_forward_evaluate`'s real splitting mechanism. Explicitly forbidden from weakening gates or fabricating results. Told to use distinct `--basetemp=.pytest-tmp-team3`. Agent ID `afd16ea5d9e70e601`.
- Both agents run concurrently per the "real subagents, not fictional team labels" + "run independent teams concurrently when safe" mandate. Both were instructed on anti-shortcut rules verbatim and given exact deliverable report formats so their claims can be checked against real files/output rather than narration.

**Next action (coordinator, while Team 2/3 run in background):** continue Team 1 (architecture/integration) investigation — map remaining disconnected code, duplicate implementations, dead code, god nodes — using Graphify, in parallel with waiting for Team 2/3 notifications. Do not block on the background agents; keep making real progress.

## Phase 1b — Team 1 architecture deep-dive: the REAL live decision path (coordinator)

**Time:** continuing same session, 2026-07-26. Coordinator performs Team 1 directly (real Serena/Graphify tool calls in-transcript), consistent with Phase 0's decision that Team 1 continues via the coordinator given its tight coupling to overall integration oversight.

**Serena calls made:** `find_symbol` (include_info) on `evaluate_day_trading_plan` (app/strategy/order_flow.py:246-294); `find_referencing_symbols` on it — callers are `app/paper/streaming_engine.py::DelayedPaperEngine._evaluate_now` (line 445), `app/research/episode_builder.py::build_episodes` (line 506), and `tests/test_strategy_order_flow.py`. `find_symbol` (include_body) on `run_headless_assistant` (tools/start_assistant.py:205-415), `DelayedPaperEngine.{__init__,_evaluate_now,_record,_consider_entry}` (app/paper/streaming_engine.py).

**Graphify calls made:** `graphify query "god nodes and high-fanin/fanout modules"`, `graphify query "predict_with_model and predict.py callers, is it dead code"`, `graphify query "feature contract FEATURE_COLUMNS in feature_contract.py versus train.py schema consistency"`, `graphify query "does tools/start_assistant.py use DelayedPaperEngine, and what feeds decisions to AutomaticRuntimeController.record_setup_decision"`. Read `graphify-out/GRAPH_REPORT.md` God Nodes section (top 10: MarketState 300 edges, MainWindow 165, MarketSessionRecorder 114, DelayedPaperEngine 88, SetupEvaluationResult 87, SetupConditionResult 85, OrderFlowThresholds 84, AccountRef 75, EpisodeConfig 74, SnapshotSource 63). Zero import cycles detected.

**CRITICAL CORRECTED FINDING (supersedes/refines Phase 0 finding #1):** `run_headless_assistant` (the real live entrypoint) wires `paper_engine.ingest` as an `AnalysisFeed` sink (`feed.add_sink(lambda event, state: paper_engine.ingest(event, state))`), and `DelayedPaperEngine._evaluate_now` calls the REAL `evaluate_day_trading_plan(...)` every event, for both LONG and SHORT, and `_record(...)` -> `_consider_entry(...)` turns an accepted evaluation into an actual risk-checked simulated (paper) order via `PaperExecutor.submit`. **This — not `AutomaticRuntimeController.record_setup_decision` — is the real, live, per-event accept/reject decision path that the headless assistant exercises today.** `record_setup_decision` is exercised only by `tools/start_prototype.py` (synthetic scenarios) and tests; it is a parallel/legacy decision-recording path not reached by the live runtime.

**Implication for Team 4 (must be communicated to that team before/at dispatch):** the "prove model output changes or can change shadow decisions" requirement must be satisfied against the REAL live path — i.e., ML shadow scoring must be able to influence (in a strictly observe-only/shadow-recorded way, never gating the actual paper order submission unless explicitly designed and safety-reviewed) the decision recorded in `DelayedPaperEngine`'s evaluation/record flow (`EvaluationRecord`, `_record`), OR a new explicit decision-log sink must be added that both the ML shadow predictor and the heuristic (`evaluate_day_trading_plan`) feed into with a recorded `decision_source`. Wiring only into `AutomaticRuntimeController.record_setup_decision` would satisfy the letter of the class name but NOT the "real headless assistant" requirement, since that method is proven unreached by `run_headless_assistant`. Team 4 must target the `DelayedPaperEngine` path (or an equivalent real sink reached by `run_headless_assistant`) as primary, and may additionally wire `record_setup_decision` for parity/GUI-snapshot consistency (`RuntimeSnapshot.shadow_decisions` is read by the GUI/snapshot layer), but the live-path wiring is the one that counts as "not a shortcut."

**No god-node/architecture correctness defects found yet** beyond the already-known predict.py disconnection (confirmed again via graphify: predict.py has no live-path callers, only its own test and train.py's `build_feature_matrix`/`load_model_artifact`, which challenger_pipeline.py also imports directly — NOT via predict.py's `predict_with_model` wrapper). No import cycles. No duplicate/competing implementations of the feature contract found (train.py and feature_contract.py share `FEATURE_COLUMNS` via direct import, confirmed by `test_feature_columns_match_the_training_contract`).

**Decision:** dispatch Team 4 with the corrected architectural target (DelayedPaperEngine live path) once Team 3 produces (or honestly fails to produce) a registered artifact. Continuing to monitor Team 2/3 background agents; will not block.

## Phase 1c — Registry/approval gate re-read + Team 4 design decision (coordinator)

**Serena calls:** `find_symbol` (include_body) on `registry.py::{validate_explicit_approval, _validate_record_evidence}`, `shadow_predictor.py::ObserveOnlyModelLoader` (full class), `feature_contract.py::{CausalFeaturePipeline, ObserveOnlyFeatureSink.ingest}`, `paper/models.py::{SetupProvenance, PaperTrade}`. Read `config/production_config.yaml` in full (confirms `live_enabled: false`, `live_mode: false` — hard safety floor, unrelated to ML work, not to be touched).

**Key finding — approval mechanism is safe and already sufficient, no architecture change needed to unlock scoring:** `validate_explicit_approval` returns state `APPROVED_RUNTIME_DISABLED` (the state `ObserveOnlyModelLoader.refresh()` requires to enter `SCORING`) precisely when `approved_artifact_id`+`approved_sha256` match a `PASSED` registry record **and** `runtime_loading_enabled`/`shadow_scoring_enabled` are BOTH left `false`. Setting either of those two booleans `true` makes the whole approval `INVALID` (hard-coded refusal, per `CHANGELOG.md`'s stated intent: "not implemented in this cycle"). This means: to legitimately turn on real shadow-mode scoring, the coordinator only needs to set `approved_artifact_id`/`approved_sha256` in `config/model_approval.yaml` to a real validated (`validation_state: PASSED`) registry record's values — leaving the two booleans false — once Team 3 produces one. This is the coordinator's delegated-autonomous approval action; it does not require any code change and does not touch the live-execution boundary (`config/production_config.yaml.live_enabled` stays false, completely separate file/gate).

**Team 4 design decision (autonomous, documented here so the dispatched subagent doesn't have to re-derive it and stays aligned):**
- Primary live-path target: `app/paper/streaming_engine.py::DelayedPaperEngine` (`_evaluate_now` → `_record` → `_consider_entry`), since this is what `run_headless_assistant` actually drives (Phase 1b finding).
- Add a `decision_source` taxonomy (`ML | HEURISTIC | BLENDED | FALLBACK`) plus `confidence`, `raw_model_output` (the shadow success_probability when available), `model_version` (artifact_id/sha), `fallback_reason`, `policy_version` to the recorded decision evidence (`EvaluationRecord` or a new adjacent record type) in `DelayedPaperEngine`, correlating the `ObserveOnlyModelLoader`'s most recent `ShadowPrediction` for the same session/direction/timestamp window to the heuristic's `evaluate_day_trading_plan` result at that same event.
- To satisfy the anti-shortcut requirement "prove model output changes or can change shadow decisions under validated conditions" honestly (not a cosmetic log-only field), implement this behind a new, default-OFF config flag (e.g. `paper_ml_decision_policy_enabled: false` in `config/production_config.yaml`, alongside existing paper-only flags — this never touches `live_enabled`). When OFF (default, safe), `decision_source` is always `HEURISTIC` or `FALLBACK` (model absent/stale/invalid) and behavior is byte-identical to today. When ON, a documented blending policy (e.g., a sufficiently low model success-probability can veto an otherwise-heuristic-accepted setup, recorded as `BLENDED`, with the reverse — model does not independently accept a heuristic-rejected setup, to keep the policy conservative/veto-only) must be proven via a dedicated deterministic unit test that constructs a scenario where flipping a stubbed model's probability provably changes the recorded/accepted outcome. This is real influence, not narration, while remaining strictly paper-only (never live — `live_enabled` remains an entirely separate, untouched, false gate) and off-by-default (safe rollout).
- Secondary/parity task: extend `AutomaticRuntimeController.record_setup_decision`'s dict with the same new fields (currently hardcoded `model_version: None`) for GUI/snapshot consistency, since `RuntimeSnapshot.shadow_decisions` already surfaces that structure — even though this method is not on the live path, keeping its schema in sync avoids a second, drifting decision-schema.
- Explicitly NOT in scope for Team 4: enabling the policy by default, touching `live_enabled`/`live_mode`, or making the model gate real broker orders (structurally impossible today since `live_enabled` is false and would need the full `app/execution/live_gate.py` ladder regardless).

**Next action:** dispatch Team 4 now (concurrently with the still-running Team 2/3 background agents) with this design locked in, targeting `app/paper/streaming_engine.py` primarily (no file overlap with Team 2's dataset-pipeline files or Team 3's challenger_pipeline/registry files) plus a small parity edit to `app/runtime/controller.py`.

## Phase 1d — Team 4 & Team 5 dispatched (coordinator)

**Time:** continuing same session, 2026-07-26, immediately after Phase 1c design lock-in.

**Actions taken:**
- `TaskUpdate`: Task #4 (Team 4) → in_progress, owner `team4-runtime`. Task #5 (Team 5) → in_progress, owner `team5-safety`.
- Dispatched **Team 4** (`Agent` tool, general-purpose, background, ID `a915581ecff1cb8c4`) — full mandate per the Phase 1c design: (1) add `paper_ml_decision_policy_enabled: false` to `config/production_config.yaml`; (2) give `DelayedPaperEngine` an optional structurally-typed `model_loader` reference threaded from `run_headless_assistant`; (3) extend `EvaluationRecord` with `decision_source/confidence/model_version/fallback_reason/policy_version`; (4) implement a conservative veto-only blending policy (model can only downgrade an accepted setup to rejected, never the reverse), default-off = byte-identical behavior; (5) deterministic unit test with a stub model_loader proving probability flip changes the recorded/accepted outcome; (6) regression test proving default-off behavior is unchanged; (7) parity edit to `AutomaticRuntimeController.record_setup_decision`; (8-10) Serena/graphify discipline, distinct `--basetemp=.pytest-tmp-team4`, `graphify update .` after edits. Explicitly told NOT to touch `live_enabled`/`live_mode`/`app/execution/live_gate.py`, and to fail safe (HEURISTIC/FALLBACK) whenever the model loader is absent, broken, or not SCORING.
- Dispatched **Team 5** (`Agent` tool, general-purpose, background, ID `a68c7e08c5093a8a5`) — safety/fallback audit across 11 concrete items (missing-model fallback, corrupted-artifact rejection, schema/feature-compatibility validation by name not just position — flagged as the most likely real bug in `train.py::build_feature_matrix`, deterministic feature ordering, stale-model detection function, invalid-feature/NaN guarding, probability-range guarding, exception-containment tracing, underperforming-model gate confirmation, explicit fallback reasons, shadow-only/no-broker-path enforcement). Explicitly forbidden from touching `app/paper/streaming_engine.py`, `app/runtime/controller.py`, or `config/production_config.yaml` (Team 4's territory) to prevent edit collisions — told to report any needed change there as a recommendation for the coordinator to merge instead. Distinct `--basetemp=.pytest-tmp-team5`.
- Both agents run concurrently with the still-in-flight Team 2 (`a2200235f06011968`) and Team 3 (`afd16ea5d9e70e601`) background agents — now 4 concurrent background teams plus the coordinator (Team 1) working live in-transcript.

**Next action (coordinator):** continue Team 1 architecture work and refresh `docs/AUTONOMOUS_HANDOFF.md` while all 4 teams run; do not block; integrate results and dispatch Team 6/7 as teams report back.

## Phase 1e — Team 1 evaluation/reporting landscape survey + Team 6 design draft (coordinator)

**Time:** continuing same session, 2026-07-26, while Team 2/3/4/5 run in background.

**Graphify/Serena calls made:** `graphify query "daily_learning.py and validation.py: what do they compare, model vs heuristic vs baseline evaluation reporting"`, `graphify query "does daily_learning.py or any report generator compare ML model predictions against heuristic decisions and a simple baseline"`, `get_symbols_overview` on `daily_learning.py`/`validation.py`, `find_symbol` (include_body) on `analyze_recorded_day`, `DailyConsistencySummary`, `SessionLearningSummary`. Read `CHANGELOG.md` in full. `git diff --stat` on all pre-existing uncommitted files to size them (GUI diffs +67/-0 lines total across screens/snapshot_source/view_models; ML diffs +96/-28 across feature_contract/session_training/shadow_predictor; start_assistant.py +50/-8).

**Findings:**
1. `app/machine_learning/validation.py` already has real walk-forward/statistical infrastructure Team 6 can build on: `walk_forward_evaluate`, `calibration_check` (`CalibrationCheckResult`, `CalibrationBin` — compares predicted probability buckets to observed outcome rates), `drift_check` (`DriftCheckResult` — "Compare live prediction distribution against the training prediction distribution"). These are genuine statistical checks, not placeholders — confirmed via symbol overview.
2. `app/machine_learning/daily_learning.py::analyze_recorded_day`/`DailyConsistencySummary` already produces a per-day, per-session descriptive report (market observation score, patterns, blockers, safety notes) with an explicit `strategy_performance` block currently gated to `status: "not_available_no_completed_real_outcomes"` / `profitability_claim_available: False` whenever no completed real (non-paper) outcomes exist — this is an honest, already-existing "we won't claim performance we can't prove" pattern Team 6 should extend rather than replace.
3. **No existing report currently compares ML-model shadow predictions against the heuristic's accept/reject decisions against a simple naive baseline (e.g. "always take the setup" or "coin flip") side-by-side.** This confirms the gap the task's point 12 ("compare ML performance vs heuristic performance vs a simple baseline") targets is real and unaddressed — Team 6's core deliverable.
4. The pre-existing uncommitted GUI diff (`app/gui/view_models.py`/`snapshot_source.py`, not authored by me) already added `ModelSnapshot.outcome_tracker_state/outcome_predictions_registered/outcome_resolved_*/outcome_dropped_*/outcome_pending` fields plus `outcome_coverage_fraction`/`abstention_rate` properties — this is genuine prior progress (by an earlier session or teammate) surfacing `PendingOutcomeTracker` state to the GUI. Team 6's report generator is complementary (a written/CLI report), not a duplicate of this GUI work.
5. `tools/paper_daily_report.py` and `tools/daily_learning_summary.py` are the existing CLI report-generation convention (thin `main()` wrapping a library function + writing to `data/reports/`) — Team 6 should follow this exact convention for a new `tools/model_evaluation_report.py` (or extend `daily_learning_summary.py`) rather than inventing a new pattern.

**Team 6 design (drafted now, to be handed to the dispatched agent once Team 3/4 land and there's real decision-source data + a real artifact to report on):**
- New function `app/machine_learning/evaluation_report.py::build_model_evaluation_report(...)` consuming: (a) the model registry record + `validation.json` (dataset id, artifact id, train/eval session date ranges, walk-forward OOS predictions, Brier score, calibration/drift check results) — all from Team 3's real artifact; (b) `EvaluationRecord`/paper-trade history annotated with the new `decision_source`/`confidence` fields from Team 4's wiring, aggregated into: count and win-rate by `decision_source` (HEURISTIC vs ML vs BLENDED vs FALLBACK), a naive "always-take-every-heuristic-accept" baseline expectancy for comparison, per-session and per-regime (using `daily_learning.py`'s existing regime/volatility tagging) breakdowns, drift indicators (via `validation.py::drift_check` comparing the live-scored probability distribution collected in the `PredictionJournal` against the training-time distribution in `validation.json`), and explicit acceptance/rejection-reason tallies (from `rejection_reasons`/`first_failure`).
- Must explicitly state limitations when insufficient real data exists (mirroring `DailyConsistencySummary`'s existing "not_available" pattern) rather than fabricating confidence.
- Entry point: `tools/model_evaluation_report.py` (new CLI, following `daily_learning_summary.py`'s `parse_args`/`main`/config-dataclass convention), writing to `data/reports/model_evaluation/<date>.md`+`.json`.

**Decision:** hold Team 6 dispatch until Team 4 (decision_source data) and Team 3 (real artifact) land — dispatching earlier would force Team 6 to work against stubs only, risking a shallow/fabricated-looking report. Continuing to monitor all 4 background agents; no blocking.

## Phase 1f — mid-flight status check + Team 7 design draft (coordinator)

**Time:** continuing same session, 2026-07-26, mid-flight check on Team 3/4 progress.

**Observed real filesystem/git evidence (not narration):**
- `git status --short` now shows Team 4 actively editing `app/paper/streaming_engine.py` (+131/-6 lines so far), `app/market/receiver.py` (+11/-2), `app/paper/options.py` (+20 new), `app/research/episode_builder.py` (+7), and `config/production_config.yaml` (confirmed real: `paper_ml_decision_policy_enabled: false` added with a clear comment, exactly matching the Phase 1c design — quoted in full: sets veto-only policy semantics, off by default, "Requires an app restart to take effect", explicitly notes PAPER-ONLY and never touches live_enabled/live_mode). Work is genuinely in progress, not yet complete (no test/report back yet).
- `config/model_approval.yaml` confirmed unchanged (`approved_artifact_id: null`, both booleans `false`) — Team 3/coordinator has not yet approved anything, correct at this stage.
- **Team 3's first real training attempt has already run and been honestly rejected**: `data/models/attempts/attempt-3d17ed8bc16762615117cc49.json` exists on disk (dataset-f94ffeaf9d2bd32d8299d57d, model_version 0.1.0) with `validation_state: REJECTED`, all 5 gates false, note: "insufficient walk-forward data (need >= 4 trading days with both label classes)", 0 total_rows/trading_days/oos_predictions. This is genuine evidence of an honest gate rejection (not fabricated), consistent with the anti-shortcut mandate — Team 3 is still working (per the "not yet notified complete" status) and will presumably retry with adjusted honest parameters or diagnose the dataset-build issue (0 rows is suspicious given 250 real sessions exist; likely a config/date-range/eligibility filter issue Team 3 is expected to diagnose per its mandate, not evidence of a data shortage per se).
- No `data/models/challengers/` directory exists yet (confirms no artifact published yet) — expected, not a problem.

**Test coverage map for Team 7 (confirmed via graphify, to inform Team 7's dispatch once Team 4/5 land):** dedicated test files already exist for essentially every ML module — `tests/test_shadow_predictor.py`, `tests/test_model_registry.py`, `tests/test_challenger_pipeline.py`, `tests/test_feature_contract.py`, `tests/test_feature_observer.py`, `tests/test_machine_learning.py` (train/validation/predict), `tests/test_outcome_journal.py`, `tests/test_pooled_training.py`, `tests/test_session_training.py`, `tests/test_daily_learning.py`, plus runtime/paper coverage (`tests/test_automatic_runtime.py`, `tests/test_paper_execution.py`, `tests/test_paper_lifecycle.py`, `tests/test_paper_daily_report.py`, `tests/test_dynamic_stops.py`) and a dedicated safety-boundary test (`tests/test_agents.py::test_agents_never_reference_execution_modules` — confirms `app/agents/*.py` never imports `app.execution`, full body read, a real structural guard, not just a docstring claim) and `tests/test_part6_hygiene.py` (property-based sizing tests, replay/burst/chaos fixtures — `test_chaos_mid_session_disconnect_fails_safe`, `test_bridge_burst_load_processes_thousands_of_ticks_quickly`).

**Team 7 design (drafted now, to dispatch once Team 4/5/6 land):** Team 7's job is NOT to write a large volume of new tests (coverage is already broad per above) but to: (1) run the FULL suite fresh after all other teams' edits land and confirm 0 regressions with an honest pass/fail count; (2) specifically add/confirm an end-to-end integration test that exercises the complete real chain in one test — synthetic Bookmap replay → receiver → `DelayedPaperEngine` with `paper_ml_decision_policy_enabled=True` and a real (not stubbed) tiny trained artifact (using a minimal synthetic dataset sized to actually pass the walk-forward gates, built specifically for this test, clearly labeled as a test fixture and never claimed as a real trading-performance result) → confirm `decision_source` becomes `"ML"`/`"BLENDED"` at least once and a `PredictionJournal`/`EvaluationRecord` entry reflects it; (3) verify `test_agents_never_reference_execution_modules`-style structural guards still pass for any new modules Team 4/5 added; (4) confirm no test was weakened/skipped/deleted by any team by diffing `tests/` against the pre-session baseline; (5) verify `live_enabled`/`live_mode` remain false throughout via `config/production_config.yaml`'s current content and a grep-after-graphify sweep for any new write path to that file.

**Next action:** continue monitoring Team 2/3/4/5; do not dispatch Team 6/7 yet (need Team 3's real artifact + Team 4's real decision_source wiring landed first, per Phase 1e/1f decisions). No blocking; coordinator remains productive.

## Phase 1g — Team 1 final audit infrastructure survey (coordinator)

**Time:** continuing same session, 2026-07-26, Team 1 (architecture) work while waiting for background-agent notifications.

**Key finding — existing machine-verifiable acceptance script as template for final audit extension:** `tools/verify_final_acceptance.py` already exists on disk with 17+ checks (mix of imports, construction tests, and grep-based assertions) — `check(name, passed, evidence)` function records each as a tuple, `main()` runs all, exits nonzero on any failure. Real checks already include: `observe_default`, `live_enabled_false` (reads `config/production_config.yaml` via `read_live_enabled`), `startup_disarmed`, `paper_research_isolated` (AST-walks `app/research/*.py` + `paper_gateway.py` and asserts zero imports of banned broker/execution modules — structural safety guard, same spirit as `test_agents_never_reference_execution_modules`), `endpoints_separated`, `live_requires_approval`, `bounded_queues`, `delayed_paper_evaluates_live_stream` (constructs real `DelayedPaperEngine`, sends 320 synthetic events, asserts `evaluations > 0` + wiring checks), `delayed_paper_opens_and_closes_positions` (constructs `PaperExecutor`, submits intent, on_tick lifecycle, asserts same-event-fill is None, target-close economics, tick-aligned fills, ledger wired in launcher), `paper_fills_are_tick_aligned`, `gui_isolated_from_capture`, `health_provider_wired`, `research_restoration`, `ledgers_separate`, `auto_finalize_wired`, `transport_tested`, `order_websocket_exists`, `cancel_replace_exists`, `partial_fill_state_machine`, `unresolved_rules_block_live` (loads real prop_rules_lucid.yaml, confirms `blocks_automated_execution`, evaluates live_gate and asserts "prop-firm" failure appears), and starts a git-ls-files check for credentials/raw-data tracking.

**Team 7 + final audit alignment:** the final audit's mandatory 12-step checklist (from the original autonomous-execution directive) should be implemented by: (a) Team 7 running the full test suite + adding the end-to-end synthetic ML-wired test; (b) the coordinator (me, at the very end after all teams report VERIFIED_COMPLETE) extending `tools/verify_final_acceptance.py` with ML-specific checks mirroring the 15 completion requirements, then running `python -m tools.verify_final_acceptance` and including its exact stdout in the final 14-item report. The existing checks already cover items like "shadow-only enforcement", "live_enabled remains false", "structural isolation of research/paper from broker code" — new checks Team 7/coordinator must add: registry record exists with exact artifact_id/sha256, model artifact is loadable via `ObserveOnlyModelLoader`, `decision_source`/`confidence` fields exist in `EvaluationRecord`, a real `PredictionJournal` entry exists on disk post-test, `config/production_config.yaml.live_enabled` is still `false` (re-confirm), no new imports of `app.execution` in any `app/machine_learning/*.py` file, etc.

**Next action:** continue monitoring; still no Team 2/3/4/5 completion notifications arrived yet (expected — they're all substantial mandates, not 5-minute tasks).

## Phase 1i — Team 3 interruption exposes legacy causal-schema mismatch

Status: `IN_PROGRESS` / internal rework required.

- Team 3 stopped on a transient agent API error and was immediately resumed on the same transcript; no duplicate team was created.
- Before interruption, Team 3 found that legacy per-session datasets lack `label_resolved_timestamp_ns`, while current `walk_forward_evaluate` requires that field to purge labels resolved after an evaluation period begins.
- This is an internal schema/evidence defect (`FAILED_REQUIRES_REWORK` within the active task), not `BLOCKED_EXTERNAL` and not a reason to weaken gates.
- Team 3 was directed to coordinate with Team 2's in-flight dataset work, rebuild honestly from completed real sessions when the current causal schema permits, retain all objective gates, and report exact counts plus real PASS or REJECT evidence.

## Phase 1h — Team 1 integration review finds detached-backend and prediction-freshness gaps

**Time:** continuing same session, 2026-07-26, while Teams 2–5 remain active.

**Graphify evidence:**
- `graphify query "Where are the remaining disconnected components between trained model artifacts, runtime shadow scoring, paper decision influence, outcome evaluation, reporting, and final acceptance verification?"` returned the relevant `DelayedPaperEngine`, `EvaluationRecord`, `ObserveOnlyModelLoader`, `OutcomeJournal`, `start_assistant.py`, and `verify_final_acceptance.py` region (broad result truncated, so focused Serena calls followed).
- `graphify path "start_backend.py" "DelayedPaperEngine"` confirmed a direct import edge; `graphify path "start_backend.py" "ObserveOnlyModelLoader"` exposed only an inferred path through `AssistantConfig`, prompting direct symbol inspection.

**Serena integration review:**
- Inspected `EvaluationRecord`, `DelayedPaperEngine._record`, `DelayedPaperEngine._apply_ml_policy`, the new ML-policy tests, `ObserveOnlyModelLoader.{__init__,score,snapshot,last_probability}`, `tools.start_assistant.{run_assistant,_build_feature_and_model_sinks}`, and `tools.start_backend.run_backend`; also enumerated `DelayedPaperEngine` and `read_ml_decision_policy_enabled` references.
- Team 4's in-flight implementation is targeting the correct `DelayedPaperEngine` path and already demonstrates low-probability veto/order suppression plus high-probability order preservation in deterministic tests.
- **Gap 1 — evidence schema:** current in-flight `EvaluationRecord` has `decision_source`, `confidence`, `model_version`, `fallback_reason`, and `policy_version`, but no explicit `raw_model_output`. The high-score test also allowed `{ML, HEURISTIC}` instead of requiring `ML`. Team 4 was messaged to correct both before completion.
- **Gap 2 — authoritative detached backend:** the default GUI launch is process-isolated and runs `tools/start_backend.py`. That function currently constructs a bare `ObserveOnlyFeatureSink()` and `DelayedPaperEngine(...)` without `_build_feature_and_model_sinks`, without a shared `ObserveOnlyModelLoader`/`PendingOutcomeTracker`, and without `read_ml_decision_policy_enabled`. Therefore the new model influence path would exist only in headless/in-process startup, not the default production-shaped detached backend. Team 4 was messaged to wire and regression-test this path.
- **Gap 3 — stale prediction reuse:** `ObserveOnlyModelLoader.last_probability(direction)` currently returns an un-timestamped cached float. `_apply_ml_policy` calls it with no decision timestamp, despite emitting a fallback string claiming a correlation window. A prior same-direction prediction can therefore be reused indefinitely. Teams 4 and 5 were both messaged: Team 4 owns correlation enforcement in the policy path; Team 5 owns the safety audit/hardening of timestamped prediction evidence.
- **Gap 4 — runtime prediction identity:** `last_probability` currently exposes only the float while artifact identity comes from a separate loader snapshot. A robust correlation result should be atomic enough to bind probability, timestamp, direction, and artifact ID/SHA to the same scored prediction; this must be reviewed in the integrated diff.

**Status:** all four agents still reported `running` through real `TaskOutput` calls. No team was promoted to `VERIFIED_COMPLETE`. Team 6/7 remain intentionally undispatched until the artifact and decision schema stabilize.

## Phase 1j — Team 6 CLI completed; coordinator verifies full suite; Team 7 dispatched

**Time:** continuing same session, 2026-07-26, after Team 4/5's decision-policy and safety work landed and Team 6's report library (`app/machine_learning/evaluation_report.py`, 15 tests) had already stabilized.

**Team 6 completion (coordinator-authored, thin CLI on top of the already-stable library):**
- Created `tools/model_evaluation_report.py`, following the `tools/paper_daily_report.py` convention exactly (argparse, always returns 0, prints markdown, delegates writing to `write_model_evaluation_report`). Reads real on-disk evidence only: dataset manifests (`data/models/datasets/*/dataset_manifest.json`), walk-forward attempts (`data/models/attempts/*.json`), the registry (`read_registry`), the approval file (`read_model_approval`), and the prediction/outcome journals (`PredictionJournal.recover`, `OutcomeJournal.recover`). Passes `decision_records=()` with an explicit inline comment, because `DelayedPaperEngine.recent_evaluations()` is confirmed in-memory-only (`deque(maxlen=500)`, no durable export exists anywhere in the codebase) — the report's own "authoritative decisions are retained in memory only" limitation string surfaces this honestly rather than fabricating or silently omitting it.
- Fixed a real Windows-console `UnicodeEncodeError` (cp1252 can't encode the `→` U+2192 arrow the markdown renderer emits in its linkage section heading): wrapped the print in `try/except UnicodeEncodeError`, falling back to `sys.stdout.buffer.write(..., errors="replace")`. The report file itself was always written correctly (UTF-8); only the terminal echo needed the fallback. Matches the codebase's existing `errors="replace"` precedent (`tools/diagnostic_export.py`) and the "reporting must never fail" contract already established by `tools/paper_daily_report.py`.
- Verified end-to-end against the real repository: `status=FAILED_REQUIRES_REWORK`, `completion_claim=False`, 2 real datasets (0 rows, 0/250 sessions included each), 2 real REJECTED attempts (`attempt-3d17ed8bc16762615117cc49`, `attempt-93a46314920348ddb4349679`) with correct N/A reasons, 0 registry artifacts, 0 decisions — an honest report of the real current state, not a fabricated pass.
- Added `test_cli_runs_against_real_shaped_on_disk_evidence` and `test_cli_handles_missing_models_root_without_crashing` to `tests/test_model_evaluation_report.py`, mirroring `tests/test_paper_daily_report.py`'s CLI-test convention (same file as the library tests, not a separate file). Suite is now 17/17 passing.
- `python -m py_compile tools/model_evaluation_report.py` → 0. Pyright diagnostics limited to the pre-existing, codebase-wide `reportMissingImports` artifact on the internal `app.machine_learning.*` import (confirmed identical pattern in `tests/test_challenger_pipeline.py` and every other ML test file) — not a real defect, not introduced by this file.

**Coordinator full-suite verification (post Team 4/5/6 landing):**
- `python -m pytest -q` (own basetemp `.pytest-tmp-coordinator-final`): **1032 passed, 0 failed** (133.56s) — before the two new CLI tests were added.
- After adding the two CLI tests: `python -m pytest -q` (own basetemp `.pytest-tmp-coordinator-recheck`): **1034 passed, 0 failed** (132.85s). Confirms zero regressions across Team 2/3/4/5/6's combined edits; the earlier Team 3/5-reported "blocked by concurrent Team 4 syntax corruption" integration concern is resolved — `py_compile` across every file in `app/` and `tools/` returns 0 errors, and full collection (1034 tests) succeeds cleanly.
- Re-confirmed `live_enabled: false` / `live_mode: false` in `config/production_config.yaml`, and `config/model_approval.yaml` remains unapproved (`approved_artifact_id: null`, both booleans `false`) — correct, since no `PASSED` registry artifact exists (Team 3 is `FAILED_REQUIRES_REWORK`, zero challengers/registry entries on disk).
- Cleaned up 20 leftover per-team pytest `--basetemp` scratch directories (`.pytest-tmp-team{2,3,4,5}-*`) left behind by the dispatched background agents — none were gitignored, all safely removable scratch output, not evidence.

**Decision:** Team 6 → `VERIFIED_COMPLETE` (library + CLI, both with dedicated passing tests, verified against real on-disk data, no fabricated metrics). Team 3 remains `FAILED_REQUIRES_REWORK` (correctly, since 0 rows/0 registered artifacts is the real, honest state — this is not something Team 6 or the coordinator can or should paper over). Dispatching Team 7 now that Teams 2/3/4/5/6 have all landed and the full suite is confirmed green.

**Next action:** dispatch Team 7 (testing & final verification) per the Phase 1f design draft: fresh full-suite confirmation (now done directly by the coordinator, 1034/1034), a real end-to-end synthetic-but-labeled integration test proving `decision_source` can become `ML`/`BLENDED` end-to-end, structural safety-guard re-confirmation, a diff-based check that no test was weakened/deleted, and extension + run of `tools/verify_final_acceptance.py` with new ML-specific checks.

## Phase 1k — Team 7 dispatched (coordinator)

**Time:** continuing same session, 2026-07-26, immediately after Team 6 completion + full-suite reconfirmation (Phase 1j).

**Action:** dispatched **Team 7** (`Agent` tool, general-purpose, background, ID recorded in task-tool metadata, owner `team7-verification`) with the full mandate: (1) a real end-to-end synthetic-but-labeled integration test proving `decision_source` can become `ML`/`BLENDED` via a genuinely trained tiny artifact satisfying the real `build_validated_challenger` gates (not a mock model, not a bypass), correlated to a real `PredictionJournal` entry by identity; (2) structural safety-guard re-confirmation (AST import guards, no new `app.execution` references, no new `live_enabled: true`/`live_mode: true` writes); (3) a diff audit of `tests/` against the last commit to confirm no test was weakened/deleted (coordinator's own preliminary check this segment: `git diff --stat HEAD -- tests/` shows only net-additive changes across 11 files, +358/-4 lines — no wholesale deletions); (4) direct re-verification (not just trusting the coordinator) that `live_enabled`/`live_mode` remain false and `app/execution/` is untouched this session; (5) a final full-suite run with its own `--basetemp=.pytest-tmp-team7` including the new test(s). Explicitly forbidden from touching `app/execution/*`, `data/models/registry/`, or `config/model_approval.yaml` (approval remains the coordinator's exclusive action, and only applies if Team 3's status changes, which it has not).

**Next action:** continue coordinator work while Team 7 runs: prepare the `tools/verify_final_acceptance.py` extension plan (ML-specific checks) so it's ready the instant Team 7 reports; do not dispatch a duplicate Team 7; do not approve any model; do not touch `live_enabled`/`live_mode`.

## Phase 1l — Team 7 and final verification complete

**Time:** final coordinator closure, 2026-07-27.

**Team 7 verification evidence:**
- Added `tests/test_end_to_end_ml_integration.py`, a synthetic-fixture-only mechanics proof using recorder-produced sessions, causal feature generation, fixed triple-barrier labels, real prior-days-only walk-forward validation, a genuinely fitted logistic-regression artifact, exact temporary ID+SHA approval, the real feature sink/loader, and the authoritative `DelayedPaperEngine` path.
- The test requires at least one authoritative evaluation with `decision_source` `ML` or `BLENDED`, and matches its prediction ID, artifact ID/SHA, session, and direction to the append-only `PredictionJournal` entry.
- The test explicitly disclaims market edge, trading performance, and profitability. It does not approve or publish any repository model.
- Structural AST checks confirm no `app.machine_learning` policy module imports `app.execution`; no production broker execution file was changed for the ML integration.
- Latest selected ML safety/integration verification: **35 passed, 281 warnings in 4.30s**.
- Machine acceptance verification: **30/30 checks passed**.
- Fresh complete project suite after the final documentation edits: **1041 passed, 321 warnings in 134.67s (0:02:14)**, exit code 0. Warnings were joblib/NumPy deprecations plus one non-failing pytest cache warning.

## Phase 1m — Architecture, lifecycle, and truth audit closure

**Authoritative reachability:**
- Bookmap Java events cross WebSocket transport into the Python receiver, accepted state is published through the analysis feed, `ObserveOnlyFeatureSink` builds deterministic validated causal vectors, and the shared exact-approval loader scores them.
- Both `tools/start_assistant.py` and `tools/start_backend.py` construct the same shared feature sink, loader, and `PendingOutcomeTracker`; both thread that loader into `DelayedPaperEngine`.
- `DelayedPaperEngine._evaluate_now` → `_apply_ml_policy` → `_record` is the authoritative paper-decision path. The default-off policy is conservative veto-only: ML may reject a heuristic accept but may never promote a heuristic reject.
- A usable score is atomically correlated by prediction ID, artifact ID/SHA, bound session, direction, event timestamp, and bounded age. Missing, stale, malformed, mismatched, corrupt, or unavailable evidence falls back safely.
- Each scored runtime prediction is journaled; predictions with complete entry/barrier evidence are registered with `PendingOutcomeTracker` and resolved through the same fixed triple-barrier semantics used offline.
- Later challenger training does **not** directly ingest `shadow_outcomes.jsonl`. It reconstructs equivalent causal historical outcome evidence from persisted completed sessions through `build_session_training_rows`. No direct outcome-journal-to-training edge is claimed.
- Historical dataset rows are deterministic and content-addressed, verify source hashes before and after construction, expose `label_resolved_timestamp_ns`, and fail closed for legacy rows missing that causal boundary.
- Walk-forward evaluation trains on prior days only, purges labels unresolved at each fold boundary, and evaluates unseen dates. Published artifacts and registry records bind model, dataset, feature-contract, validation, and content hashes. Runtime loading requires one explicit exact ID+SHA approval and immutable-evidence freshness.

**Graphify and Serena evidence:**
- Targeted Graphify lifecycle queries surfaced `DelayedPaperEngine`, `ObserveOnlyModelLoader`, `OutcomeJournal`, `PendingOutcomeTracker`, `build_challenger_dataset`, `build_validated_challenger`, startup wiring, `EvaluationRecord`, and the acceptance verifier.
- Focused Serena symbol/reference inspection confirmed both startup topologies share the loader/policy graph and that `AutomaticRuntimeController.record_setup_decision` is not the authoritative headless/detached path.
- Graphify was refreshed after implementation changes; the final refresh is repeated after this documentation closure.

**Working-tree audit:**
- Reviewed the combined tracked/untracked implementation rather than discarding team or pre-existing work. `.qoder/` and `graphify-out/` are preserved.
- Reviewed `data/processed/session_20260710T143000Z.build.json`: it is a real two-event delayed-session build summary with zero evaluations/candidates and source-file hashes, not a model artifact or fabricated metric. It is retained for final diff review rather than overwritten or deleted.
- `config/model_approval.yaml` remains unapproved. `live_enabled: false` and `live_mode: false` remain unchanged. No broker execution path was modified or enabled.

## Final persistent statuses

- Team 1 — `VERIFIED_COMPLETE`: authoritative topology, reachability, isolation, semantic wording, documentation, and working-tree audits are complete with concrete evidence.
- Team 2 — `VERIFIED_COMPLETE`: deterministic causal dataset pipeline and fail-closed legacy handling are implemented and tested.
- Team 3 — `FAILED_REQUIRES_REWORK`: the real-data attempt completed honestly but cannot produce a challenger under the current causal contract.
- Team 4 — `VERIFIED_COMPLETE`: model evidence reaches and can conservatively change the authoritative paper decision path.
- Team 5 — `VERIFIED_COMPLETE`: approval, integrity, staleness, correlation, exception, fallback, and execution-isolation gates are tested.
- Team 6 — `VERIFIED_COMPLETE`: evaluation/reporting consumes available durable evidence and reports missing comparisons and limitations honestly.
- Team 7 — `VERIFIED_COMPLETE`: trained-artifact synthetic mechanics integration, acceptance checks, focused safety tests, and the full suite passed.

**Unresolved real-market result (must not be upgraded by inference):** 250 physical session manifests exist, but only 5 are analysis-eligible, 1 is replay-eligible, and 0 are model-training-eligible. The 4,109 legacy rows lack `label_resolved_timestamp_ns` and fail closed. There are 0 current-contract real training rows; all five gates failed on both real attempts. Therefore no real challenger was published, no registry record exists, no artifact is approved, and no claim of cross-session market learning, edge, or profitability is available. This remains `FAILED_REQUIRES_REWORK`, not `BLOCKED_EXTERNAL` and not `VERIFIED_COMPLETE`.

## Phase 1n — Bridge-handshake-provenance root cause confirmed fixed and regression-tested

**Time:** continuing coordinator session, 2026-07-28.

**Goal:** determine, with evidence rather than inference, whether the uncommitted `app/market/receiver.py` handshake-event-ordering change is by itself sufficient to unblock real-session `handshake_accepted` provenance, or whether the Java bridge also requires changes.

**Causal chain traced (Graphify-oriented, then Serena/Read-confirmed):**
1. `bookmap_addon_java/.../MessageFactory.java::connected()` (lines 77-82) builds the `connected` control message via `controlMap()` and stamps `capabilities=BridgeConfig.CAPABILITIES`, `provider="bookmap"`.
2. `bookmap_addon_java/.../BridgeConfig.java` already declares `PROTOCOL_VERSION = "1.2"` (line 18) and `CAPABILITIES = "aggregated_depth,trades,aggressor_side,source_timestamps"` (line 28) — this is the exact capability set `app/research/session_catalog.py` requires (`required_capabilities`), and `"1.2"` satisfies `app/market/protocol.py::_check_compatibility()` (`SUPPORTED_PROTOCOL_MAJOR = 1`, `MINIMUM_PROTOCOL_MINOR = 2`).
3. **Conclusion: the Java bridge was never the blocker.** It already sends a fully protocol-compatible, fully-capability-declared handshake.
4. The actual defect was Python-side: in `app/market/receiver.py::consume_market_stream()`, the recorder previously persisted each control event (`recorder.record_control_event(event)`) *before* `on_control_event(event)` ran — but `on_control_event` is `tools/start_receiver.py`'s `_guarded_control_event` closure, which is what stamps `event["handshake_accepted"] = True` after a compatible `parse_handshake()` check. The recorder therefore always recorded the pre-stamp, unenriched event. The uncommitted diff swaps the order so enrichment runs first.

**Test-coverage gap found and closed:** all pre-existing "handshake persists" tests (`test_initial_connected_event_persists_accepted_handshake`, and the recorder-unit tests in `tests/test_database_recorder.py`) exercised only the `initial_control_events` preload path (already correctly ordered before this diff, since it manually calls `_guarded_control_event(event)` then `recorder.record_control_event(event)`) or the recorder in isolation — never the live-websocket `consume_market_stream()` path that a real Bookmap capture actually drives and that the diff modifies. Added `test_live_connected_event_persists_accepted_handshake` / `_server_persists_live_handshake` to `tests/test_start_receiver.py`, sending a `connected` handshake directly over the live socket (not preloaded) and asserting `bridge_provenance.handshake_accepted is True` with the correct protocol version, provider, and capability list in the resulting session manifest.

**Regression-validity check:** temporarily reverted the `app/market/receiver.py` ordering fix, reran the new test in isolation, confirmed it failed (`assert False is True` on `handshake_accepted`), then restored the fix and reconfirmed the test passes. `git diff --stat -- app/market/receiver.py` after restoration matches the original uncommitted shape (9 insertions, 2 deletions) — no residual drift, no leftover backup file.

**Test evidence:**
- `tests/test_start_receiver.py` alone: 8 passed (7 pre-existing + 1 new).
- `tests/test_start_receiver.py` + `tests/test_database_recorder.py` + `tests/test_bridge_protocol.py` together: 37 passed.
- `tests/test_session_catalog.py`: 29 passed.
- Full project suite (`python -m pytest -q`), prior to this phase's test addition: 1067 passed, 0 failed (137.80s). A fresh full-suite rerun including the new test is in progress; see the next worklog entry or `AUTONOMOUS_HANDOFF.md` for its result.

**What this changes for the dataset/labeling-integrity track:** the bridge-handshake-provenance defect moves from "plausibly fixed, root cause not fully confirmed" to **fixed and regression-tested in the current uncommitted working tree**. The 250 already-recorded manifests remain permanently unrepairable by design (retroactive enrichment of already-written immutable manifests is not attempted). Real-data model-training eligibility now depends only on running a fresh live capture through the fixed code — not on any further code change. This does not by itself produce any new eligible training rows; it only removes the code-level blocker that was preventing new sessions from acquiring accepted-handshake provenance.

**Graphify refresh:** ran `graphify update .` after the test-file structural addition — 7934 nodes, 19146 edges, 366 communities rebuilt cleanly (1682 non-code files, e.g. session manifests, correctly produce zero nodes and are skipped).

**Two other uncommitted-diff features test-verified this phase (previously only diff-reviewed, not yet run):**
- **ML decision-policy veto** (`app/paper/streaming_engine.py`): `tests/test_ml_decision_policy.py` directly exercises `DECISION_SOURCE_BLENDED`, `ML_VETO_PROBABILITY_THRESHOLD`, and the veto-only correlation/fallback semantics (e.g. `test_veto_only_never_accepts_a_heuristic_rejected_setup`, "Policy off must not even consult a broken loader - true byte-identical path").
- **Fixed-contract PAPER risk sizing** (`app/paper/execution.py`): `tests/test_paper_execution.py` directly exercises `ExecutionConfig(fixed_contracts=..., max_risk_per_trade_usd=...)`, including a hard-cap-exceeded case and a legacy-default-compatible case.
- Combined run: `tests/test_ml_decision_policy.py tests/test_paper_execution.py tests/test_real_paper_ledger.py tests/test_part6_hygiene.py` → **63 passed, 0 failed**.

---

## Goal C Mission — 2026-07-29 (branch feature/automatic-runtime)

New mission phase begins here. Full directive recorded verbatim in
`docs/GOAL_C_AUTONOMOUS_MISSION.md`. This section logs execution of that mission's 8 phases.
CONTEXT CONTROL rules apply: one subagent at a time, subagents write full reports to repo files
and return <=10 summary lines, no full diffs/graphs/logs printed in chat, graphify-first for
source reads, docs updated after every phase.

### Orientation (complete)

- `git status` / `git log -10` confirmed branch `feature/automatic-runtime`, same modified/
  untracked file set as prior session (ML-003 diff still uncommitted, per `docs/review_packet.md`).
- `graphify query "top-level GUI, autonomous research, reporting, launcher, and machine learning
  architecture overview"` run (BFS depth=2, 672 nodes, truncated to budget) — confirmed key
  entry points: `app/gui/screens.py` (`build_screens`, `OverviewScreen`, `ResearchHealthScreen`,
  `Screen`), `app/gui/view_models.py` (`AppSnapshot`, `Health`, `Capability`), `app/gui/widgets.py`
  (`Card`, `StatTile`, `StatusBadge`, `MetricMeter`, `EvidenceTable`), `app/gui/charts.py`
  (`HistoryChart`, `AggressorBar`, `ChartPanel`), `app/research/research_service.py`
  (`ResearchService`, `.run_batch()`), `app/research/auto_research.py` (`ResearchRuntimeConfig`,
  `CandidateConfig`, `SessionRef`, `AcceptedSetup`, `ResearchResult`, `ResearchCheckpoint`,
  `ReceiverHealth`), `app/research/profitability_progress.py` (`ProgressCache`),
  `app/machine_learning/{train.py,validation.py}` (`build_feature_matrix`, `build_label_array`,
  `load_labeled_dataset`, `walk_forward_evaluate`, `split_labeled_rows_by_trading_day`),
  `tools/start_assistant.py` (`AssistantConfig`, `AssistantStartupError`).
- `docs/GOAL_C_AUTONOMOUS_MISSION.md` created with full verbatim mission text.
- This worklog and `docs/AUTONOMOUS_HANDOFF.md` updated with a Goal C section.

### Phase 1 — Runtime architecture audit (next)

About to dispatch `runtime-architect` subagent per mission Phase 1. Prompt will include the
graphify-first instruction and require output written to `docs/audits/runtime_architecture.md`
with <=10 summary lines returned to the conversation.

### Phase 1 — Runtime architecture audit (complete)

`runtime-architect` subagent dispatched; wrote full findings to
`docs/audits/runtime_architecture.md` (421 lines). Key findings:
- `AutomaticRuntimeController`/`DelayedPaperEngine` split is real and load-bearing, not a bug,
  but `record_setup_decision`/`ShadowExecutionStub` inside `controller.py` are dead-on-production
  vestiges (only reachable from `tools/start_prototype.py` and tests) that risk being mistaken for
  the live decision chain. Recommends extraction/namespacing + a boundary regression test for
  Phase 8.
- `tools/backend_supervisor.py`/`start_backend.py` already provide mature duplicate-prevention
  (fingerprinted `SingletonLock`), bounded readiness-wait, append-only log preservation, and
  graceful-then-forced shutdown with an audit trail (`runtime/forced_shutdowns.jsonl`). Phase 7
  must wrap `ensure_supervisor`/`request_stop`, never reimplement or add a second lock mechanism.
- `ResearchService`'s background-thread + `ClaimRegistry` + read-only `SnapshotSource` pattern is
  the correct hook point for Phase 3's Autonomous Intelligence subsystem and Phase 4's GUI page;
  new work must flow through the existing challenger/registry/approval gate chain and must never
  hold a reference into `DelayedPaperEngine`/`PaperExecutionGateway` beyond read-only status.
- Flagged: two divergent lock implementations (`PrototypeInstanceLock` weaker than `SingletonLock`
  — Phase 7 must confirm the desktop launcher never targets `start_prototype.py`); two parallel
  GUI windows (`AppWindow` production, `MainWindow` legacy, both must be preserved per mission).
- One open verification item from the audit — whether the receiver thread gates
  `DelayedPaperEngine` ingestion on `AutomaticRuntimeController.decisions_allowed` — was resolved
  directly by the lead agent post-audit via `docs/AUTOMATIC_PAPER_PIPELINE.md`: this is
  intentional and by design. `decisions_allowed` gates broker/shadow decisions only (delayed data
  must never route an order); paper evaluation is always on regardless, fixed in commit `f81486e`
  after a prior regression conflated the two concerns. Audit doc updated in place with this
  resolution.

Phase 1 is now complete and verified. Proceeding to Phase 2 (UI premium glass-morphism redesign).

## Phase 2 — UI premium glass-morphism redesign (complete)

Dispatched `ui-product-designer` subagent (this agent type is read-only: Read/Glob/Grep/Bash/
WebSearch/WebFetch/mcp__serena__* only, cannot write files). First two dispatch attempts failed
with a transient Gemini-API `400: missing thought_signature` tool-calling error; third attempt
with an explicit `model: "sonnet"` override succeeded. Subagent returned its full review inline
(since it cannot write); lead agent wrote the output verbatim to `docs/audits/ui_product_design.md`
and `docs/ui_page_parity_matrix.md`, and appended a pointer section to `docs/ui_redesign_system.md`.

Subagent's central verdicts: 8 `AppWindow` screens and 13 `MainWindow` tabs must all remain
untouched structurally (confirmed, see parity matrix); reject Card hover-glow (Card has no
interactive semantics) and reject chart frosted-glass backdrop (Qt has none; do a static
area-gradient instead); the highest-priority, lowest-risk item is wiring the fully-unused
`BUTTON_PRIMARY/SECONDARY/DANGER/GHOST` design tokens into real QSS and onto the three
`ExecutionScreen` buttons — those are the most safety-relevant interactive controls in the app.

Before implementing anything, the lead agent independently re-verified every claim against
source rather than trusting the subagent's inline text as ground truth (subagents are
reviewers/researchers only per CONTEXT CONTROL rule 2):
- Serena `find_referencing_symbols` on `BUTTON_PRIMARY` in `app/gui/design_tokens.py` returned
  `{}` — confirmed zero references anywhere, i.e. the button-variant token system really was
  fully dead code.
- Direct `Read` of `app/gui/design_tokens.py`, `app/gui/widgets.py` (`Card`, `StatusBadge`),
  `app/gui/theme.py` (`DASHBOARD_DARK`/`DASHBOARD_LIGHT`/`APP_STYLESHEET` — confirmed
  `APP_STYLESHEET` really is legacy-`MainWindow`-only per its own module docstring, so it was
  correctly left untouched), `app/gui/screens.py` (`ExecutionScreen.__init__`,
  `LiveOrderFlowScreen.__init__`), `app/gui/charts.py` (`HistoryChart.paintEvent`,
  `AggressorBar.paintEvent`), and `app/gui/app_window.py` (confirmed the trust-strip badges
  `trust_live`/`trust_source`/`trust_integrity`/`trust_drops` are the ones users actually see;
  `OverviewScreen`'s own `overview_live_locked`/`overview_provenance` badges are intentionally
  hidden duplicates, matching the parity matrix's note).
- Found and resolved a gap the subagent's plan had not fully closed: `SHADOW_GLOW_*` tokens in
  `design_tokens.py` are CSS-box-shadow-style strings (`"0 0 20px rgba(...)"`), not directly
  usable as a `QColor` for `QGraphicsDropShadowEffect.setColor()`; and `BADGE_LOCKED` had no
  `glow` field set at all (unlike `BADGE_ERROR`/`BADGE_INFO`/`BADGE_SUCCESS`/`BUTTON_PRIMARY`/
  `BUTTON_DANGER`). Added a new `COLOR_LOCKED_GLOW` rgba constant rather than trying to parse the
  CSS-string tokens at runtime.

Implemented directly (four files, all in `AppWindow`'s dashboard system only — `MainWindow`/
`APP_STYLESHEET` untouched, confirmed out of scope):
1. `app/gui/theme.py`: added `QPushButton[variant="primary|secondary|danger|ghost"]` QSS rules
   (plus `:hover`/`:disabled`) to both `DASHBOARD_DARK` and `DASHBOARD_LIGHT`; added `:focus`
   rings for `flow_price_chart`/`flow_cvd_chart`/`flow_aggressor_bar`.
2. `app/gui/screens.py`: set `variant` Qt property on `ExecutionScreen`'s three buttons
   (`execution_connect_demo`=primary, `execution_sync_now`=secondary,
   `execution_disconnect`=danger).
3. `app/gui/widgets.py`: `StatusBadge.set_status()` now attaches a `QGraphicsDropShadowEffect`
   glow (16px blur, `COLOR_LOCKED_LIGHT`/`COLOR_ERROR_LIGHT`) only for `state in
   ("locked", "fail")`, clearing it otherwise — reuses `Card`'s existing shadow-effect pattern.
4. `app/gui/design_tokens.py`: added `COLOR_LOCKED_GLOW` constant.
5. `app/gui/charts.py`: `HistoryChart.paintEvent` now fills a closed area path under the line
   with a `QLinearGradient` (low-alpha `COLOR_PRIMARY` fading to transparent) before stroking
   the line itself — pure paint-layer addition, no data/state change.

Deliberately not implemented, per the audit's own verdicts (recorded, not silently dropped):
sidebar section-grouping separators (no section grouping exists yet to separate — deferred),
generic `Card` hover-glow (Card has no interactive/clickable semantics — rejected), and
card `border-radius:10px` vs `RADIUS_LG`/`RADIUS_XL` token drift (low priority, cosmetic only —
left as-is).

Verification: `tests/test_app_window.py` 52/52 passed unchanged. `tests/test_gui_visual_regression.py`
failed on 8/24 states on first run (Execution, Risk and Lucid Account, Overview, Live Order Flow —
exactly the screens touched), which is the expected, intended signal since those screens' pixels
changed by design. Re-blessed all 24 baselines via `python -m tools.bless_gui_baselines` (a
purpose-built, explicitly-manual-invocation-only tool per its own module docstring: "Run only
when the visual change is intentional and has been reviewed"). Reran both suites: 77/77 passed.

Phase 2 is now complete and verified. Proceeding to Phase 3 (autonomous ML/strategy/risk
research system) — dispatching `ml-strategy-researcher` subagent next.

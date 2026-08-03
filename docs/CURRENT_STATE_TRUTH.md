# Current-State Truth

_Last updated: 2026-07-27. Evidence classifications describe repository behavior and local verification, not financial performance._

## Verified Working

| Capability | Evidence |
|---|---|
| Protocol-guarded Bookmap intake, feed-quality accounting, bounded recording, and clean finalization | `tools/start_receiver.py`, `app/market/feed_guard.py`, `app/database/recorder.py`; receiver/recorder/transport tests |
| Detached backend/supervisor; GUI restart cannot interrupt capture | `tools/backend_supervisor.py`, `tools/start_backend.py`; `tests/test_process_isolation.py` |
| Delayed-paper causal evaluation, independent risk rejection, and no broker import from the paper engine | `app/paper/streaming_engine.py`; streaming/paper lifecycle tests |
| Strict model-training session gate with protocol/provider/capability/quality provenance | `app/research/session_catalog.py`; `tests/test_session_catalog.py` |
| Deterministic catalog-gated dataset, canonical content identity, and source hashes verified before and after construction | `app/machine_learning/challenger_pipeline.py`; `tests/test_challenger_pipeline.py` |
| Shared causal feature contract is used online and offline; current rows carry `label_resolved_timestamp_ns` and legacy rows without it fail closed | `app/machine_learning/feature_contract.py`, `app/machine_learning/session_training.py`; feature/session-training tests |
| Prior-days-only walk-forward evaluation uses unseen dates, forbids train/test overlap, and purges labels unresolved at each fold boundary | `app/machine_learning/pooled_training.py`; pooled-training tests |
| Immutable challengers and registry records bind model bytes, dataset bytes/ID, feature contract, validation evidence, and artifact path | `app/machine_learning/registry.py`; model-registry tests |
| Runtime loader requires one exact approved artifact ID+SHA and rejects missing, malformed, corrupt, mismatched, or stale evidence | `app/machine_learning/shadow_predictor.py`, `app/machine_learning/registry.py`; shadow-predictor and safety-audit tests |
| Runtime artifact age derives from immutable walk-forward `test_day` evidence; 30 days is fresh and 31 days is stale; stale models are unloaded and cannot score | loader/registry staleness tests; `tools/verify_final_acceptance.py` |
| In-process and detached startup construct one shared feature sink, loader, prediction journal, and outcome tracker, and pass the same loader to `DelayedPaperEngine` | `tools/start_assistant.py::_build_feature_and_model_sinks`, `tools/start_backend.py::run_backend` |
| Exact scored evidence reaches the authoritative paper decision recorder and may conservatively veto a heuristic accept; it never promotes a heuristic reject | `app/paper/streaming_engine.py::_apply_ml_policy`; runtime-policy and end-to-end integration tests |
| Authoritative evaluations persist decision source, confidence/raw output, artifact/prediction identity, fallback reason, and policy version | `app/paper/streaming_engine.py::EvaluationRecord`; paper-policy tests |
| Cached prediction evidence is correlated atomically by prediction ID, artifact ID/SHA, session, direction, timestamp, and maximum age | `ShadowProbabilityEvidence`, `ObserveOnlyModelLoader.last_probability`; safety tests |
| Append-only runtime prediction/outcome journals resolve eligible predictions with the same fixed triple-barrier rule used by offline labels | `app/machine_learning/outcome_journal.py`, `app/machine_learning/triple_barrier.py`; outcome-journal tests |
| Evaluation/reporting compares available model, heuristic, fallback, baseline, prediction, outcome, registry, approval, dataset, and attempt evidence without inventing missing metrics | `app/machine_learning/evaluation_report.py`, `tools/model_evaluation_report.py`; report tests |
| GUI surfaces real model/outcome-tracker status rather than placeholder success | `app/gui/view_models.py`, `app/gui/snapshot_source.py`, `app/gui/screens.py`; GUI/process tests |
| ML/paper policy remains structurally isolated from broker execution | AST checks in safety tests and `tools/verify_final_acceptance.py` |

## Authoritative Lifecycle

The production-shaped reachability is:

`Bookmap Java event → WebSocket → Python receiver/feed guard → analysis-thread MarketState → ObserveOnlyFeatureSink → validated causal FeatureVector → exact-approved ObserveOnlyModelLoader → PredictionJournal → DelayedPaperEngine._apply_ml_policy → safety/fallback → authoritative EvaluationRecord/paper decision → PendingOutcomeTracker/OutcomeJournal → persisted completed sessions → deterministic historical rows → prior-days-only walk-forward retraining → immutable challenger/registry → later exact-approval loading`.

`AutomaticRuntimeController.record_setup_decision` is used by prototype scenarios and tests; it is not the authoritative headless/detached path. `DelayedPaperEngine._evaluate_now` → `_apply_ml_policy` → `_record` → `_consider_entry` is authoritative.

The policy is default-off and paper-only. When enabled under validated conditions, it is veto-only. Missing or invalid evidence preserves the heuristic outcome through explicit fallback. It cannot enable broker execution.

## Outcome-to-Retraining Boundary

Runtime predictions are causally resolved and persisted to `shadow_outcomes.jsonl`. The current challenger pipeline does **not** directly ingest that journal. It rebuilds equivalent causal outcome evidence from completed raw session events via `build_session_training_rows` and the shared triple-barrier semantics. This is a valid causal reconstruction path, but it is not a direct `OutcomeJournal → build_validated_challenger` edge and must not be described as one.

## Present but Non-Authoritative or Disconnected

- `app/machine_learning/predict.py::predict_with_model` can score a loaded artifact in isolation, but production runtime scoring uses `ObserveOnlyModelLoader` directly.
- `AutomaticRuntimeController.record_setup_decision` retains schema parity for prototype/GUI-facing scenarios but is not the authoritative runtime paper path.
- Legacy GUI placeholder paths are not evidence; the default snapshot/provider path is authoritative.
- Generic calibration/drift utilities exist. Reports expose only comparisons supported by durable evidence and mark unavailable inputs explicitly.

## Real-Data Training Result: Failed, Not Hidden

The local corpus has:

- 250 physical session manifests;
- 5 analysis-eligible sessions;
- 1 replay-eligible session;
- 0 model-training-eligible sessions;
- 4,109 legacy rows missing `label_resolved_timestamp_ns` and therefore rejected;
- 0 current-contract real training rows.

Both real challenger attempts failed all five walk-forward gates. No real challenger was published, no registry record was created, and no artifact was approved. Team 3 therefore remains `FAILED_REQUIRES_REWORK`.

## Synthetic Integration Boundary

`tests/test_end_to_end_ml_integration.py` uses recorder-produced synthetic sessions, real causal feature/label construction, real walk-forward folds and gates, a genuinely fitted logistic-regression artifact, exact temporary approval, the real loader and feature sink, and the authoritative `DelayedPaperEngine`. It proves that exact model evidence can change a recorded paper decision and that decision identity matches the prediction journal.

It does **not** prove market edge, generalization to real markets, profitability, fill quality, or genuine cross-session real-market learning.

## Safety State

- `config/production_config.yaml`: `live_enabled: false`, `live_mode: false`.
- `config/model_approval.yaml`: `approved_artifact_id: null`, `approved_sha256: null`, `runtime_loading_enabled: false`, `shadow_scoring_enabled: false`.
- No model is approved.
- No production broker execution path was modified for ML integration.
- No automatic DEMO or LIVE order execution is authorized.
- The legacy serialized `decision_impact: "none"` marker means no direct loader/broker mutation; it does not prohibit causally correlated paper-policy consumption.

## Verification Snapshot

- Latest selected ML safety/integration tests: **35 passed, 281 warnings in 4.30s**.
- Machine-verifiable acceptance: **30/30 checks passed**.
- Complete project suite after the final documentation edits: **1041 passed, 321 warnings in 134.67s**, exit code 0.
- Graphify was refreshed after code changes and used with Serena symbol/reference inspection to confirm production reachability and execution isolation.

## Not Implemented / Not Authorized

- Approval or promotion of any model that has not passed real lifecycle gates.
- A claim of real-market cross-session learning, edge, or profitability.
- Model changes to risk limits.
- Automated DEMO or LIVE orders.
- Direct challenger ingestion of `shadow_outcomes.jsonl`.
- MBO/order-ID/native-iceberg/queue-position features unavailable from the current feed.

## Next Legitimate Evidence Step

Collect and cleanly finalize enough new real sessions that satisfy the current protocol, capability, continuity, model-training eligibility, and causal-label-resolution contract. Then rerun the unchanged challenger pipeline and objective gates. Do not approve a model unless a real challenger passes and its immutable evidence independently validates.

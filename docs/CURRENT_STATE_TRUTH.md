# Current-State Truth

_Last updated: 2026-07-23. Evidence classifications describe the repository and local tests, not financial performance._

## Verified Working

| Capability | Evidence |
|---|---|
| Protocol-guarded Bookmap event intake, feed-quality accounting, bounded recording, and clean finalization | `tools/start_receiver.py`, `app/market/feed_guard.py`, `app/database/recorder.py`; `tests/test_start_receiver.py`, `tests/test_database_recorder.py` |
| Detached backend/supervisor; GUI restart cannot interrupt capture | `tools/backend_supervisor.py`, `tools/start_backend.py`; `tests/test_process_isolation.py` |
| Delayed-paper causal evaluation and independent risk rejection; no broker import from paper engine | `app/paper/streaming_engine.py`; `tests/test_streaming_paper_engine.py` |
| Eligible finalized session -> episode build -> research -> daily/paper reports | `tests/test_learning_chain.py` (local WebSocket and real backend subprocess) |
| Strict model-training session gate with protocol/provider/capability/quality provenance | `app/research/session_catalog.py`; `tests/test_session_catalog.py` |
| Deterministic catalog-gated dataset and immutable source hashes | `app/machine_learning/challenger_pipeline.py`; `tests/test_challenger_pipeline.py` |
| Prior-days-only pooled validation, immutable offline challenger registry, exact ID+SHA approval truth | `app/machine_learning/pooled_training.py`, `app/machine_learning/registry.py`; `tests/test_pooled_training.py`, `tests/test_model_registry.py` |
| Default GUI reports real challenger validation/approval state | `app/gui/view_models.py`, `app/gui/snapshot_source.py`, `app/gui/screens.py`; `tests/test_app_window.py`, `tests/test_process_isolation.py` |
| Shared causal feature builder is used by offline training and supports incremental construction with a canonical-vector golden fixture; runtime integration remains disconnected | `app/machine_learning/feature_contract.py`, `app/machine_learning/session_training.py`; `tests/test_feature_contract.py` |
| Walk-forward evidence uses CME trading days and purges labels unresolved before each validation fold | `app/machine_learning/session_training.py`, `app/machine_learning/pooled_training.py`; `tests/test_session_training.py`, `tests/test_pooled_training.py` |
| Registered challengers are integrity-bound to model bytes, feature contract, content-addressed bundle/validation evidence, and content-addressed dataset bytes | `app/machine_learning/registry.py`; `tests/test_model_registry.py` |
| The shared feature observer consumes every accepted state on the analysis thread, resets and rewarms on gaps/session boundaries, and records memory-only feature observations with no model or decision effect | `app/machine_learning/feature_contract.py`, `tools/start_assistant.py`; `tests/test_feature_observer.py` |

## Partially Working

- Bookmap protocol/capability provenance is now promoted to new manifests; older manifests remain provenance-incomplete and fail closed for ML.
- Per-session training remains descriptive and manual. Its CLI now uses catalog eligibility, but per-session models are not registry candidates.
- Pooled challenger construction is manual and offline. It produces no automatic approval or runtime effect.
- The backend imports a read-only, disarmed DEMO status service. Automatic capture/paper/research paths cannot submit orders, but the broad historical claim that the backend imports no execution package is false.
- Synthetic production-path load/soak harnesses exist. Their results are not equivalent to a multi-hour installed-Bookmap shadow session.

## Present but Disconnected

- `app/machine_learning/predict.py` can score a loaded artifact in isolation, but the automatic runtime does not call it.
- Generic drift/calibration utilities exist but are not connected to challenger outcomes.
- Legacy GUI model placeholders are not evidence; the default `AppWindow` snapshot path is authoritative.

## Planned

1. Exact approved-artifact observe-only loader on the analysis thread.
2. Append-only prediction and resolved-outcome journals.
3. Calibration, confidence, drift, and abstention reporting.
4. Human-reviewed champion/challenger promotion after sufficient real shadow evidence.

## Blocked by External Capability

- MBO/order IDs/native iceberg and queue position: unavailable on the current forwarded feed.
- Real-time scalping decisions: only delayed Bookmap data has been exercised.
- Real Bookmap durability after recent capture fixes: requires a new market session.
- Tradovate/Lucid execution: outside this cycle; prop-rule evidence remains unresolved.

## Not Implemented / Not Authorized

- Automatic retraining or promotion.
- Runtime ML decision gating.
- Model changes to risk limits.
- Automated DEMO or LIVE orders.
- Any profitability claim.

## Safety State

`config/production_config.yaml` keeps LIVE false. `config/model_approval.yaml` defaults to no approval, runtime loading false, and shadow scoring false. Registered challengers have `runtime_loaded=false`, `shadow_predictions=0`, and `decision_impact=none`.

## Evidence Boundaries

- Unit/integration tests and synthetic load tests prove software behavior under controlled inputs.
- They do not prove an order-flow edge, profitability, real fill quality, paid-feed capability, or multi-hour live-feed durability.

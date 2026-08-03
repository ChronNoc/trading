# Engineering-to-QA Handoff

_Last updated: 2026-07-27._

## Cycle scope

Causal cross-session ML lifecycle mechanics through the authoritative paper/shadow decision path: provenance-gated historical data, deterministic labels/features, prior-days-only walk-forward validation, immutable artifacts, exact approval and staleness validation, runtime scoring, conservative paper-policy consumption, decision provenance, prediction outcomes, evaluation reporting, and fail-closed safety.

This cycle does not authorize broker execution and does not establish real-market edge or profitability.

## Architecture invariants

- Receiver and recorder do not call broker order code.
- `app.machine_learning` does not import `app.execution`.
- Training reads finalized catalog-approved raw sessions and uses only causally available features.
- Labels expose the timestamp at which they became known; unresolved-at-fold-boundary and legacy missing-timestamp rows are purged.
- Approval is exact artifact ID+SHA and is never written by training.
- Runtime policy is default-off, paper-only, and veto-only; it may reject a heuristic accept but never promote a heuristic reject.
- Missing, stale, malformed, mismatched, corrupt, or unavailable model evidence safely preserves the heuristic result.
- Research completion is evidence-backed: per-job evidence is atomically published before its key is marked complete and the checkpoint is saved.
- Default approval, runtime loading, shadow scoring, DEMO/LIVE behavior, `live_enabled`, and `live_mode` remain false.

## Changed behavior

- Manifests preserve bridge protocol/provider/IDs/capability declarations and observed coverage.
- Catalog exposes strict model-training eligibility and reasons.
- `tools.train_per_session` is catalog-gated and parts-aware.
- `tools.train_pooled` builds a deterministic content-addressed dataset and either an honest attempt rejection or an immutable offline challenger.
- Walk-forward folds use prior CME trading days, unseen evaluation dates, and purge labels unresolved before each fold boundary.
- Registry reads authenticate the bundle, validation, dataset manifest/bytes, model bytes, feature contract, and immutable artifact path.
- `ObserveOnlyModelLoader` loads only an exact-approved non-stale artifact and emits append-only scored evidence. The class name remains for compatibility; its guarantee is no direct broker-side effect, not zero paper-policy use.
- `tools/start_assistant.py` and `tools/start_backend.py` share one feature-sink/model-loader/outcome-tracker construction graph and pass the same loader into `DelayedPaperEngine`.
- `DelayedPaperEngine._evaluate_now` → `_apply_ml_policy` → `_record` is the authoritative paper decision path. Evaluations persist full ML/fallback provenance.
- `PendingOutcomeTracker` resolves eligible runtime predictions with the shared fixed triple-barrier rule and appends outcomes.
- The challenger pipeline reconstructs equivalent causal historical outcome evidence from completed raw sessions; it does not directly ingest `shadow_outcomes.jsonl`.
- Default GUI status shows actual loader identity/state, prediction count, failures, and outcome coverage.

## Unchanged behavior

- Broker execution gateways, risk authority, DEMO command authorization, and LIVE gates.
- Existing generated raw/processed/label/report/model data remain user-owned.
- `config/model_approval.yaml` remains unapproved.

## Expected artifacts

Manual challenger runs write under `data/models/datasets/<dataset-id>/`, `attempts/`, optional `challengers/<artifact-id>/`, and `registry/<artifact-id>.json`. Runtime evidence uses append-only prediction/outcome JSONL files under the configured model root. Generated artifacts are gitignored; tests use temporary roots.

## QA challenges

- Ineligible, active, old-protocol, missing-capability, discontinuous, or damaged session exclusion.
- Source mutation during dataset construction.
- Deterministic rebuild, content-addressed identity, and immutable overwrite refusal.
- Legacy missing `label_resolved_timestamp_ns` rejection and unresolved-label fold purging.
- Insufficient days, one class, no edge, or any objective gate failure creates no registry record.
- Artifact/dataset/bundle hash mismatch, partial approval, unsafe approval flags, and stale immutable evaluation evidence.
- Loaded artifact is unloaded when later invalid or stale; non-`SCORING` states cannot score.
- Prediction correlation rejects wrong session, direction, artifact, future timestamp, or excessive age.
- ML cannot promote a heuristic rejection; malformed/missing model evidence preserves the heuristic outcome.
- Exact prediction/artifact/session/direction identity joins authoritative evaluations to prediction records.
- Runtime outcomes honor target-first/stop-first/tie/timeout semantics without future leakage.
- Structural AST proof of no ML-to-broker execution imports.
- Synthetic integration remains explicitly mechanics-only.

## Known evidence limitation

The local real corpus currently has 0 current-contract model-training rows. The 4,109 legacy rows lack causal label-resolution timestamps and fail closed. Both real challenger attempts failed every walk-forward gate; no real challenger, registry record, approval, or profitability evidence exists. Team 3 remains `FAILED_REQUIRES_REWORK`.

## Verification commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_shadow_predictor.py tests/test_ml_safety_audit.py -q
.\.venv\Scripts\python.exe -m tools.verify_final_acceptance
.\.venv\Scripts\python.exe -m pytest -q
graphify update .
```

The synthetic end-to-end test fits and validates a real logistic-regression artifact against generated fixtures, but it must never be cited as real trading performance.

## Rollback boundary

Disable paper-policy use through configuration and leave approval empty. Pointing `models_root` at an empty directory makes the loader fail closed. Never delete raw sessions, append-only prediction/outcome evidence, or immutable attempt/challenger evidence as rollback. Never modify broker execution paths to roll back ML behavior.

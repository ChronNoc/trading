# Engineering-to-QA Handoff

## Cycle scope

Provenance-gated ML dataset/challenger evidence and truthful GUI model state. No runtime model loading or trade effect.

## Architecture invariants

- Receiver/recorder do not call broker order code.
- Registry/challenger modules do not import strategy, paper, risk, execution, or runtime controller modules.
- Training reads only finalized catalog-approved raw sessions.
- Approval is exact ID+SHA and is never written by training.
- Default approval, runtime loading, shadow scoring, and LIVE remain false.

## Changed behavior

- New manifests preserve bridge protocol/provider/IDs/capability declarations and observed coverage.
- Catalog exposes strict model-training eligibility and reasons.
- `tools.train_per_session` is catalog-gated and parts-aware.
- `tools.train_pooled` builds a deterministic dataset and either an attempt rejection or immutable offline challenger.
- Walk-forward folds use CME trading days and purge labels unresolved before validation.
- Registry reads authenticate the content-addressed bundle, validation, dataset manifest, dataset bytes, model bytes, and feature contract.
- Default GUI shows registry/validation/approval truth and explicitly says there is no decision effect.

## Unchanged behavior

- Strategy, paper entry, sizing, risk, DEMO command surface, and LIVE gates.
- Existing generated raw/processed/label/report/model data remain user-owned and untouched.

## Expected artifacts (manual CLI, not test defaults)

`data/models/datasets/<dataset-id>/`, `attempts/`, optional `challengers/<artifact-id>/`, and `registry/<artifact-id>.json`. They are gitignored.

## QA challenges

- Ineligible/active/old-protocol/missing-capability session exclusion.
- Source mutation during build.
- Deterministic rebuild and immutable overwrite refusal.
- Insufficient days/one class/no edge rejection with no registry record.
- Artifact hash mismatch and partial/unsafe approval rejection.
- Snapshot codec strictness and malformed-registry GUI state.
- Structural proof of no ML-to-decision/execution imports.

## Known omissions

- Runtime model loading/scoring, prediction outcomes, drift/calibration, automatic retraining/promotion, real Bookmap post-fix soak, and all broker order execution. The feature-only analysis sink is connected but cannot load or score a model.

## Verification commands

Run focused provenance, challenger/registry, GUI/codec, process-isolation and learning-chain tests; then the complete pytest suite. Run load/short-soak only against temporary roots. Java rebuild is unnecessary because this cycle does not modify Java.

## Rollback boundary

Disable/remove registry observation by pointing `models_root` at an empty directory; runtime behavior is unchanged because no model is loaded. Never delete raw sessions or immutable attempt/challenger evidence as rollback.

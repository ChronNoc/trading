# Research-to-Engineering Contract

Every research handoff must be falsifiable and machine-reproducible.

## Required fields

1. **Experiment ID/version, owner, date, status.**
2. **Hypothesis and mechanism:** what should happen, why, failure regimes, and simpler competing explanation.
3. **Admissible data:** exact session IDs, dataset ID/hash, source provenance, capability requirements, quality exclusions.
4. **Feature contract:** exact ordered columns, version/hash, formula/window/normalization/missing-data behavior/cost.
5. **Label contract:** target, stop, horizon, tie handling, incomplete-horizon handling, overlap/purge/embargo rules.
6. **Validation design:** chronological folds, train/validation/final periods, untouched evaluation set, random seeds and hyperparameters.
7. **Execution assumptions:** spread, latency, slippage, fees, fill uncertainty and optimistic/base/conservative scenarios.
8. **Baselines:** price return, volume, volatility, spread, session time, and current champion/rule where applicable.
9. **Predeclared acceptance gates:** sample/day minimums, calibration, expectancy, drawdown, stability, sensitivity and abstention.
10. **Outputs/observability:** paths, schemas, logs, GUI fields and reason codes.
11. **Nonclaims:** what the result cannot establish.
12. **Rollback:** how to disable/remove runtime use without deleting evidence.
13. **Prohibited effects:** research code cannot change risk limits, approve itself, or enable DEMO/LIVE.

## Current cycle contract

- Input: only `SessionEntry.eligible_for_model_training` sessions.
- Dataset: content-addressed manifest and JSONL rows from `challenger_pipeline.py`.
- Model: deterministic logistic regression.
- Validation: prior trading days only, explicit costs and take-all baseline.
- Output: rejected attempt or immutable unapproved challenger.
- Runtime effect: none; `runtime_loaded=false`, `shadow_predictions=0`, `decision_impact=none`.

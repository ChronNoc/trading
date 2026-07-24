# Autonomous Product Backlog

## Current

| ID | Priority | Owner | Item | Reason / acceptance |
|---|---:|---|---|---|
| ML-001 | P0 | Data + ML | Provenance-gated reproducible dataset and immutable offline challenger | Completed when eligible/excluded sessions and hashes are explicit, folds are prior-days-only, registry is immutable, and runtime impact is none. |
| OPS-001 | P0 | Reliability | Complete capture provenance and current-session loss truth | Completed when protocol/provider/capability and lifetime-vs-session loss agree across manifest and catalog. |

## Next

| ID | Priority | Dependencies | Item | Acceptance |
|---|---:|---|---|---|
| ML-003 | P1 | ML-002 | Explicit approved-model observe-only loader | Loads exact ID+SHA only; schema/hash failure disables scoring; analysis thread only; no decision effect. |
| ML-004 | P1 | ML-003 | Prediction/outcome journal | Prediction ID joins to target/stop/timeout outcome without leakage; append-only and quality-stamped. |
| GUI-002 | P1 | ML-003/4 | Runtime model evidence | GUI shows loaded ID, prediction, abstention, outcome coverage, and errors from real snapshot state. |
| DATA-002 | P1 | market availability | New real delayed Bookmap shadow session | Zero new session drops, clean finalization, accepted 1.2 provenance, automatic reports. |

## Later

- ML-005 (P2): calibration bins, Brier confidence intervals, PSI/drift and abstention quality.
- ML-006 (P2): audited champion/challenger comparison and reversible human promotion.
- EDGE-001 (P2): incremental OOS tests for microprice/depth-imbalance features against returns/volume/spread/time baselines.
- GUI-003 (P2): process control and diagnostic export through confirmed backend state.
- PERF-002 (P2): quantitative Qt event-loop responsiveness under concurrent 1,650+ events/s capture.

## Blocked

- EXEC-001: Tradovate DEMO order submission — requires separate authorization and safety review.
- LIVE-001: any LIVE execution — not authorized.
- EDGE-MBO: queue/MBO/native iceberg work — current feed exposes no order IDs.

## Rejected

- Loading the newest model by filename or modification time: non-reproducible and unsafe.
- Automatic promotion after training: validation and deployment must remain separate.
- Deep/RL models before simulator, parity, and baseline evidence: unnecessary degrees of freedom.
- Missing capability represented as zero: zero has market meaning and would be deceptive.

## Completed

- Capture moved off fragile GUI ownership.
- Bounded receiver/recorder/analysis stages with conservation metrics.
- Session rotation after bridge/recorder damage.
- Protocol 1.2 batching/global sequencing.
- Manual pooled walk-forward evaluation.
- ML-001 and OPS-001 first-cycle vertical slice.
- ML-002 shared causal feature builder and canonical-vector golden fixture; runtime integration remains disconnected.
- Purged CME-trading-day walk-forward evidence with label-resolution provenance.
- Registry evidence graph integrity binding for model, bundle, validation, dataset, and feature contract.
- Every-event analysis-thread feature observer with gap/session reset, rewarm, and memory-only status; no model scoring.

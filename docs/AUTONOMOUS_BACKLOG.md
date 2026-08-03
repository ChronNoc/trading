# Autonomous Product Backlog

_Last updated: 2026-07-27. Status distinguishes verified software mechanics from unavailable real-market learning evidence._

## Current

| ID | Priority | Owner | Item | Reason / acceptance |
|---|---:|---|---|---|
| DATA-002 | P0 | Data + ML | Collect current-contract real delayed Bookmap sessions | Complete only when enough finalized sessions pass protocol/provider/capability/continuity/model-training gates and yield causally resolved rows for prior-days-only walk-forward evaluation. |
| ML-REAL-001 | P0 | ML | Rerun real challenger lifecycle without relaxing gates | Complete only when a real-data challenger passes every objective gate, immutable evidence validates independently, and an honest registry record is published. Current status: `FAILED_REQUIRES_REWORK`. |

## Next

| ID | Priority | Dependencies | Item | Acceptance |
|---|---:|---|---|---|
| ML-005 | P1 | sufficient real prediction/outcome coverage | Calibration bins, Brier confidence intervals, PSI/drift, and abstention quality | Report only statistically supportable comparisons; unavailable coverage remains explicit. |
| ML-006 | P1 | real challenger passes | Audited champion/challenger comparison and reversible human promotion | Exact artifact ID+SHA approval remains separate from training; no automatic promotion. |
| GUI-003 | P2 | stable backend evidence | Process control and diagnostic export | GUI commands operate through confirmed detached-backend state and preserve capture isolation. |
| PERF-002 | P2 | production-shaped soak | Quantitative Qt responsiveness under concurrent capture | Measured event-loop and capture behavior at the declared rate; no inferred success. |

## Blocked / Not Authorized

- EXEC-001: Tradovate DEMO order submission requires separate authorization and safety review.
- LIVE-001: any LIVE execution is not authorized.
- EDGE-MBO: queue/MBO/native-iceberg work is unavailable because the current feed exposes no order IDs.
- Model approval is blocked by evidence, not tooling: no real challenger has passed.

## Rejected

- Loading the newest model by filename or modification time: non-reproducible and unsafe.
- Automatic promotion after training: validation, approval, and deployment must remain separate.
- Deep/RL models before simulator, parity, and baseline evidence: unnecessary degrees of freedom.
- Missing capability represented as zero: zero has market meaning and would be deceptive.
- Relaxing causal timestamps, session eligibility, or walk-forward gates to manufacture an artifact.
- Presenting the synthetic integration fixture as market edge, cross-session real-market learning, or profitability.

## Verified Complete

- Capture moved off fragile GUI ownership.
- Bounded receiver, recorder, and analysis stages with conservation metrics.
- Session rotation after bridge/recorder damage.
- Protocol 1.2 batching and global sequencing.
- OPS-001 capture provenance and current-session loss truth.
- ML-001 provenance-gated deterministic dataset, prior-days-only evaluation, immutable attempt/challenger evidence, and exact approval separation.
- ML-002 shared causal feature contract and canonical-vector golden fixture, used online and offline.
- ML-003 exact-approved, integrity- and staleness-validated loader. It creates no direct broker effect but supplies atomically correlated evidence to an optional paper-only policy consumer.
- ML-004 append-only prediction and outcome journals with shared fixed triple-barrier resolution.
- Authoritative `DelayedPaperEngine` integration with default-off conservative veto-only policy and exact decision provenance.
- Shared in-process/detached startup graph for feature sink, loader, and outcome tracker.
- GUI model/outcome status from real runtime snapshots.
- Synthetic trained-artifact mechanics proof through the authoritative paper decision path.
- Final acceptance verifier covers approval, staleness, provenance, journal round trip, execution isolation, and false live flags.

## Current Real-Evidence Failure

The local corpus contains 250 physical manifests, 5 analysis-eligible sessions, 1 replay-eligible session, and 0 model-training-eligible sessions. All 4,109 legacy rows lack `label_resolved_timestamp_ns` and fail closed; there are 0 current-contract real training rows. Both real attempts failed all five walk-forward gates. No real artifact was published, registered, or approved.

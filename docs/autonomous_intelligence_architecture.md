# Autonomous Intelligence Architecture (Phase 3 design)

Status: design document, not yet implemented. Grounded in the findings of
`docs/audits/ml_strategy_research.md`. This document defines what Phase 3
implementation must build, and what Phases 4 (GUI page) and 5 (Reports tab)
will read.

## Non-negotiable boundaries (carried from the mission)

- No new path may bypass dataset → walk-forward → challenger → registry →
  explicit human approval → loader.
- No autonomous component may write to `PaperExecutionGateway`,
  `DelayedPaperEngine`, any broker gateway, the approval file, or any mutable
  production safety-limit configuration. Read-only access to these is fine.
- No look-ahead leakage, no random time-series splits, no in-sample promotion,
  no live execution, ever.
- A "candidate strategy" in the PROPOSED/EVALUATED/etc. states below is
  **review evidence only**. It never constitutes runtime approval. Only the
  existing `registry.py` explicit-approval mechanism (exact artifact ID + SHA)
  can authorize a model to be loaded.

## 1. `AutonomousIntelligenceService`

A new service, separate from `ResearchService`, added under
`app/research/` (co-located with the existing service it reuses patterns
from, but a distinct class — it must not be merged into `ResearchService`,
which owns strategy-replay research and has its own job/checkpoint contract).

Reused from `ResearchService` (pattern, not inheritance — copy the proven
approach, don't couple the two services):
- Receiver-priority throttling before any disk I/O (cheap gates first, per
  `research_service.py:365-377`).
- Atomic claim-based job dispatch (`ClaimRegistry` pattern) so concurrent
  invocations cannot double-process a candidate.

**Fixed, not reused as-is** (this was the confirmed defect in
`ml_strategy_research.md` §5): persist evidence *before* marking a job
complete, not after. The write order must be:
1. Write candidate evidence/result to a uniquely-named temp file.
2. Atomically rename into place (`tmp.replace(path)`, same pattern already
   used correctly elsewhere in this codebase, e.g.
   `research_service.py:490-492`, `:576-579`).
3. Only then mark the job complete in the checkpoint and save the checkpoint.

Other operational requirements not present in `ResearchService` today, to be
built in from the start rather than retrofitted:
- Unique per-job temp file names (not the shared checkpoint `.tmp` path
  pattern in `auto_research.py:450-455`).
- Periodic in-flight heartbeats during long-running work, not only after
  futures complete.
- Lease fencing tokens so a recovered "stale" claim can't race a slow-but-alive
  worker.
- Per-job wall-clock deadlines.
- Deterministic seeds recorded per candidate (required for reproducibility of
  any result before it can be trusted).
- Resource quotas: max concurrent candidates, max candidates per day, max disk
  usage for candidate artifacts, retention/eviction policy for old candidates.

Inputs the service receives (constructor/config, all read-only references):
- Paths to existing dataset/session catalog readers.
- Receiver-health provider (same contract `ResearchService` already consumes).
- Bounded candidate definitions (what the service is allowed to try — e.g. a
  fixed enumerable set of hyperparameter/feature-window variations, not
  arbitrary code execution).
- Existing `build_challenger_dataset` / `walk_forward_evaluate` /
  `build_validated_challenger` functions, called read-only-with-respect-to-
  production-state (they write challenger artifacts to a models directory and
  registry records, which is the existing, already-safe behavior of
  `register_challenger` — unapproved by construction).

The service does **not** get a handle to `PaperExecutionGateway`,
`DelayedPaperEngine`, the approval file, or any broker gateway. This is
enforced by simply never constructing it with those dependencies — there is no
code path for it to reach them.

## 2. `CandidateStrategyRecord` data model

Content-addressed record (candidate ID derived from a hash of its defining
inputs, so identical candidates never duplicate). Fields:

- `candidate_id`, `schema_version`
- `kind`: one of `MODEL`, `STRATEGY_REPLAY`, `RISK_ANALYSIS`
- `hypothesis`: free-text description of what's being tried and why
- `parent_id`: optional, for candidates derived from a prior one
- `baseline_id`: what this candidate must beat (constant-prior, previous
  approved artifact, etc. — never "nothing")
- `proposer`: which component/process generated this candidate
  (`autonomous_intelligence_service` vs. a human-triggered run)
- `software_revision`: git SHA at candidate-build time
- `feature_hash`, `label_hash`: from the existing `feature_contract.py`
  contract, so drift is detectable
- `source_session_ids`: exact sessions used, for reproducibility and audit
- `split_policy`: chronological split description + confirmation an untouched
  final test period was reserved (per §6 of the audit's protocol)
- `model_or_strategy_config`: the exact hyperparameters/config used
- `seed`: deterministic seed
- `cost_and_fill_assumptions`: explicit record of which assumptions were used
  (pooled fixed-cost model vs. `EpisodeConfig`'s richer fill model) — the audit
  found these two are not reconciled today; every candidate record must state
  which one it used so that gap is visible per-candidate rather than silent.
- `preregistered_gates`: the acceptance thresholds fixed *before* evaluation
  (per §6 of the audit) — day count, setup count, day-concentration limit,
  bootstrap CI bound, Brier skill bound, calibration bound, stress-slippage
  check
- `resource_budget`: compute/time/disk limits for this candidate
- `attempt_evidence_refs`: paths to persisted evidence (never inline results —
  always a reference to an immutable evidence file)
- `state`: see state machine below
- `errors`: structured error history
- `safety_attestation`: explicit boolean fields confirming no write access to
  gateway/broker/approval-file/safety-config was used in producing this
  candidate

### State machine

```
PROPOSED -> QUEUED -> CLAIMED -> BUILDING_DATASET -> EVALUATING
    -> GATE_FAILED | EVALUATED -> CHALLENGER_REGISTERED
    -> AWAITING_EXPLICIT_APPROVAL

(any state) -> FAILED_RETRYABLE -> QUEUED  (bounded retry count)
(any state) -> FAILED_TERMINAL
EVALUATED | CHALLENGER_REGISTERED -> REJECTED   (human review rejects)
any terminal state -> ARCHIVED  (retention policy)
```

`CHALLENGER_REGISTERED` means `register_challenger` (existing, unmodified
function) has recorded the artifact — it is registered but **unapproved**,
exactly as `build_validated_challenger` already behaves today. Nothing in this
design changes what "registered" means or adds a second approval path.
`AWAITING_EXPLICIT_APPROVAL` is a terminal state for the candidate record — a
human reviewing it uses the existing, unmodified explicit-approval mechanism
in `registry.py:252-293` (exact artifact ID + SHA) to actually approve it. The
candidate record is never itself consulted by the loader.

## 3. GUI read seam (for Phase 4)

`SnapshotSource` (`app/gui/snapshot_source.py`) is already the established
read-only seam pattern (see `_research_snapshot`, `_challengers`). Phase 4
must add a parallel read-only accessor — e.g. `_autonomous_intelligence_snapshot`
— that:
- Reads only cached/persisted candidate records and service status.
- Never triggers research work, never performs blocking disk I/O on the GUI
  thread beyond what the existing snapshot pattern already tolerates.
- Never exposes a GUI action that can approve a model. Approval stays a
  deliberate, out-of-band operator action against `registry.py`, not a button.

## 4. Evaluation protocol (binding on the service, not just documentation)

The service must refuse to mark any `MODEL` candidate `EVALUATED` unless it
has run the full protocol from `ml_strategy_research.md` §6: cheap baselines
first, evaluation against the exact runtime policy (heuristic-accepted setups
+ 0.35 veto + paper execution simulator — not the pooled 0.5-threshold offline
metric alone), untouched chronological test period, day/setup count minimums,
day-concentration limit, bootstrap CI, Brier skill vs. constant prior,
calibration bound, and slippage-stress sign-reversal check. A candidate that
skips any of these must land in `FAILED_TERMINAL` with a specific reason, never
silently in `EVALUATED`.

## 5. What Phase 3 implementation must ship

1. `CandidateStrategyRecord` model + persistence (content-addressed JSON,
   atomic write pattern) under `app/research/`.
2. `AutonomousIntelligenceService` with the fixed persist-before-checkpoint
   ordering, lease fencing, per-job deadlines, resource quotas.
3. The binding evaluation protocol as executable gates, not just documented
   thresholds.
4. Tests: restart-safety (kill mid-persist, verify no false-complete),
   duplicate-service-start rejection, budget enforcement, protocol-gate
   enforcement (a candidate that would fail any gate must not reach
   `EVALUATED`).
5. Update `docs/AUTONOMOUS_HANDOFF.md` and the worklog once implemented and
   tested.

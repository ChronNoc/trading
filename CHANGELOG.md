# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-25

### Safety boundary

**Runtime model loading remains explicitly disabled in this release.** Every
piece of ML infrastructure added below — dataset construction, training,
validation, the model registry, and shadow scoring — is offline-only and has
no path into live decision-making:

- `config/model_approval.yaml` ships with `runtime_loading_enabled: false`
  and `shadow_scoring_enabled: false`, and no artifact is approved
  (`approved_artifact_id: null`, `approved_sha256: null`).
- `app/machine_learning/registry.py` rejects any registry record that claims
  `runtime_loaded=True`, nonzero `shadow_predictions`, or a `decision_impact`
  other than `"none"`, and `validate_explicit_approval` hard-fails if
  `runtime_loading_enabled` or `shadow_scoring_enabled` is ever set to `True`
  ("runtime loading and shadow scoring are not implemented in this cycle").
- `app/machine_learning/challenger_pipeline.py` builds, validates, and
  registers challengers but never loads a model into the trading runtime;
  every published bundle is stamped `runtime_loaded: false`,
  `shadow_predictions: 0`, `decision_impact: "none"`.
- The observe-only feature sink and `ObserveOnlyModelLoader` in
  `app/machine_learning/shadow_predictor.py` build feature vectors and score
  them into an append-only prediction journal purely for offline evidence
  collection — trading decisions, sizing, and order flow are untouched.

This boundary is documented in `docs/RESEARCH_TO_ENGINEERING_CONTRACT.md`
and `docs/DECISION_LEDGER.md`, and is enforced by the D-001/D-006 safety
gates referenced in the pipeline's own commit history. Enabling runtime
loading or shadow scoring is out of scope for this release and requires a
future, explicitly reviewed change.

### Added

- **Offline challenger ML pipeline** (`app/machine_learning/challenger_pipeline.py`):
  deterministic, content-addressed dataset construction from catalog-gated,
  finalized sessions only; source files are hashed before and after row
  construction so an in-progress or externally modified session can never be
  published under misleading provenance. `build_validated_challenger`
  chains dataset build → walk-forward validation → gate check → immutable
  model packaging → registry publication, and returns a `"rejected"` status
  (with a written attempt report) rather than a model whenever any gate
  fails.
- **Shared causal feature contract** (`app/machine_learning/feature_contract.py`):
  a single, versioned, hash-stable feature-construction path used by both
  training and the observe-only runtime sink, so training/serving skew is
  structurally impossible.
- **Immutable model registry** (`app/machine_learning/registry.py`): SHA256
  evidence validation binds every registry record to its exact model bytes,
  bundle manifest, validation report, and source dataset; `read_registry`
  revalidates all of this on every read. `ModelApproval` requires an exact
  `approved_artifact_id` **and** `approved_sha256` pair — nothing is ever
  inferred as "latest."
- **Observe-only shadow scoring** (`app/machine_learning/shadow_predictor.py`):
  an `ObserveOnlyFeatureSink` that builds feature vectors on the analysis
  thread, an `ObserveOnlyModelLoader` for exact-approval-gated scoring, and
  a `PredictionJournal` append-only JSONL evidence sink — all decoupled from
  strategy, paper, risk, and execution code.
- **Pooled walk-forward learning** (`app/machine_learning/pooled_training.py`,
  `tools/train_pooled.py`): pools triple-barrier-labeled rows across every
  recorded session, sorts by time, and walk-forward validates (train on past
  days, test on each later day, no look-ahead) instead of training
  per-session on too few rows to find a signal. Reports honest net
  expectancy after costs versus taking every trade, with no profitability
  claimed unless the model beats that baseline out-of-sample.
- **Per-session ML training** (`app/machine_learning/session_training.py`,
  `app/machine_learning/train.py`, `tools/train_per_session.py`): causal
  triple-barrier labeling with an out-of-sample temporal holdout, hardened
  to survive a corrupt or unreadable session rather than aborting the run.
- **New GUI widgets and review tooling**: `app/gui/charts.py`
  (`HistoryChart`, `AggressorBar` market-visualization widgets),
  `app/gui/widgets.py` (reusable `Card`, `StatTile`, `StatusBadge`,
  `MetricMeter`), and `app/gui/review_snapshot.py` (a synthetic-free
  snapshot builder for GUI testing).
- **Comprehensive ML test coverage**: `tests/test_challenger_pipeline.py`,
  `tests/test_feature_contract.py`, `tests/test_feature_observer.py`,
  `tests/test_model_registry.py`, and `tests/test_shadow_predictor.py` —
  48 new tests covering dataset provenance, byte-stable feature
  construction, gap handling, immutability/tamper detection in the
  registry, and zero-decision-impact observe-only scoring.
- **New engineering documentation**:
  [`docs/AUTONOMOUS_BACKLOG.md`](docs/AUTONOMOUS_BACKLOG.md) (prioritized
  work items with acceptance criteria),
  [`docs/CURRENT_STATE_TRUTH.md`](docs/CURRENT_STATE_TRUTH.md) (system
  capabilities and honest limitations),
  [`docs/DECISION_LEDGER.md`](docs/DECISION_LEDGER.md) (architectural
  decisions with rationale),
  [`docs/ENGINEERING_TO_QA_HANDOFF.md`](docs/ENGINEERING_TO_QA_HANDOFF.md)
  (test scenarios and verification steps),
  [`docs/FAILURE_LEARNING_LEDGER.md`](docs/FAILURE_LEARNING_LEDGER.md)
  (post-mortem findings and preventions), and
  [`docs/RESEARCH_TO_ENGINEERING_CONTRACT.md`](docs/RESEARCH_TO_ENGINEERING_CONTRACT.md)
  (the offline/online safety boundary contract referenced above).
- **Configurable risk/paper-trading behavior**: configurable daily
  paper-trade/loss caps (including an unlimited-cap option of `0` for the
  learning stage), and break-even/trailing stop management for the paper
  engine, subsequently tightened to lock in profit sooner.
- Historical per-session, daily paper-trading digest, and daily-learning
  reports backfilled into `data/reports/` and `data/processed/` for
  2026-07-10 through 2026-07-24, giving the pooled walk-forward pipeline a
  multi-day, multi-session dataset to train and validate against.

### Changed

- `numpy` is now declared as a direct `ml` extra dependency in
  `pyproject.toml` (it was previously an undeclared transitive dependency
  pulled in via scikit-learn, exercised directly by
  `app/machine_learning/train.py`). `pandas`/`polars`, evaluated
  experimentally this cycle, were left undeclared since neither is imported
  by any first-party code.
- `app/database/recorder.py`, `app/gui/app_window.py`,
  `app/market/feed_guard.py`, `app/market/protocol.py`, and
  `app/research/session_catalog.py` were extended to support the new ML
  pipeline (atomic write/replace-with-retry helpers used by dataset and
  registry publication, model-training eligibility in the session catalog,
  and related plumbing).
- `.gitignore` updated to exclude ephemeral per-session `buildstatus.json`
  logs (rewritten on every automatic rebuild attempt) and two oversized
  per-session `decisions.jsonl` files (8.8MB and 106MB) that are
  regenerable build output rather than durable reference data.

### Fixed

- Per-session training (`app/machine_learning/session_training.py`) no
  longer aborts the whole training run when a single session's data is
  corrupt or unreadable — it now skips the bad session and continues.

[0.2.0]: https://github.com/ChronNoc/trading/compare/v0.1.0...v0.2.0

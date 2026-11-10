# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Buy Databento history from the app** (`app/importers/databento_api.py`,
  `databento_download.py`, `databento_key.py`; the "Download from Databento" card in the import
  window; `python -m tools.databento_download`; guide `docs/DATABENTO_IMPORT.md`). With the user's
  own API key: a price check first, a confirmation of the exact price, then MNQ MBP-10 history is
  bought as daily CSV files and imported through the existing importer. Never spends more than
  confirmed, never pays twice for a day (imported days are left out; purchases are recorded in a
  ledger so a stopped run continues), and the key is kept outside the repository and sent only to
  `*.databento.com`. Historical only: Bookmap stays the live feed. The only module allowed to use
  the network (user-approved exception in AGENTS.md).
- Databento downloads can buy **trading hours only** (09:30–16:50 New York of each weekday), the
  default, instead of whole 24-hour days, which cost about $13 per trading day on the first real
  quote. History is chosen in trading days, from one day up to a year (`--hours rth|full` and
  trading-day `--days` on the command line).
- `python -m tools.databento_training [--on|--off] [--day YYYY-MM-DD]`
  (`app.importers.databento.set_training_permission`): allow or stop machine-learning training on
  Databento days already imported. Only the opt-in and the verdicts derived from it change; the catalog
  still keeps a day with failed quality checks out of training.
- A "Databento Import" desktop shortcut with its own icon
  (`tools/install_desktop_shortcut.ps1 -Importer`).

- **Lucid risk governor** (`app/risk/lucid_governor.py`, `config/risk_governor.yaml`,
  `docs/RISK_GOVERNOR.md`): account-level rules fitted to the LucidFlex 25K evaluation - an
  end-of-day-floor day loss budget (25% of the buffer), a per-trade risk cap, a retire threshold,
  a consistency-rule daily profit cap read from the profile, an entry window and flat time, entry
  and streak limits, a trade-quality floor from costs, a microscalping guard, and size that only
  scales on the bot's own proven edge. PAPER only; LIVE stays locked. It only ever adds
  restrictions on top of the generic engine, and none of the protected risk files was modified.
  Active in the two production start-up paths only; `DelayedPaperEngine` is unchanged by default.
  Takes effect after a full backend restart; `enabled: false` restores the previous behaviour.
- `app/risk/lucid_fit.py` and `python -m tools.lucid_fit_report`: a deterministic simulation of a
  Lucid evaluation, governed and under the previous production rules, across a range of true edges.
- `CloseReason.RISK_GOVERNOR` for positions the governor flattens.

### Changed

- **Book features read the top 10 levels of each side, live and in history** (user decision,
  2026-11-09; `BOOK_FEATURE_LEVELS` in `app/market/state.py`). Databento MBP-10 history has 10 levels
  and live Bookmap recordings up to 20, so features that summed the whole visible book differed
  between the two. Book imbalance, liquidity added/cancelled, the largest defended level, and the
  strategy's own block, loading-liquidity, ask-cancellation and size-at-price checks now read the same
  top 10 everywhere. Live effect: a block more than 10 levels (2.5 points) from the price no longer
  counts. The ML feature contract is now `shared-causal-market-features-v4` (v3 datasets and models
  are not comparable; new training builds v4 datasets), and the strategy is `order-flow-plan-v2`, so
  the build orchestrator rebuilds every session's episodes with the new rule.
- An imported day may train when at most 1 trade in 10,000 had no aggressor side (those trades are
  left out at import); it used to be blocked by a single one. Databento marked 2 of 1,340,741 trades
  that way on 2026-11-02. User decision, 2026-11-09
  (`IMPORT_UNSIDED_TRADES_PER_ALLOWED` in `app/research/session_catalog.py`).

- **Replay is about 3x faster, with byte-identical results** (2026-11-09; one Databento trading day
  took ~48 minutes). Profiled on 10 minutes of 2026-11-02 (2.6 million events): block detection
  built each snapshot's price-to-size map once instead of scanning the book per lookup (53.7 million
  scans); the book update inserts by binary search and builds the state once instead of rebuilding,
  re-sorting and copying it twice per event (a hand-built book out of order still takes the old
  sorting path); the level tracker works out the New York clock once per second and prunes only when
  the day changes; the episode builder reuses one JSON encoder for the stream hash and copies the
  window only when a trade is pending; the replay loader no longer copies every row. The same slice
  built by the previous code and the new code gives byte-identical decisions, episodes, labels and
  build summary (256 s before, 86 s after); randomized tests compare the fast book update with the
  sorting one and the tracker shortcuts with recomputing every event.

- **Background work can no longer overload the PC** (2026-11-10: the PC powered off hard, Kernel-Power
  41 with no bug check, under hours of full load). Research used one worker process per logical CPU
  (15 of 16) to rebuild every session after the strategy change, beside a training run and a
  Databento import. Now (`app/runtime/background_limits.py`): research uses at most 2 worker
  processes by default (an explicit worker count or the high-performance switch still decide); every
  background worker, the automatic session rebuild (now in its own child process instead of a thread
  inside the recording backend), training (`tools.train_pooled`), episode builds and Databento
  imports run with one compute thread (two for imports) at below-normal priority. Measured: a training
  run uses exactly one core (6% of a 16-thread CPU); the whole PC averaged 17.5% while it ran.
- **A progress bar for model training** on the Research and Model Health page ("Model training
  progress"): size n of 3, session, percent and time left, then what each finished size showed. The
  training run writes `runtime/training_progress.json` (`app.machine_learning.training_progress`); the
  GUI reads it itself, so it never travels in the backend's status file (the snapshot codec skips the
  field, and a backend of either version stays readable). A run that stops writing shows as STOPPED.

- **More kinds of model** (2026-11-10, user request): the offline pipeline now trains and tests
  gradient-boosted trees and a random forest beside the logistic regression
  (`MODEL_KINDS`/`make_classifier` in `app/machine_learning/pooled_training.py`), every one
  single-threaded with a fixed seed, through the same walk-forward, stability and cost gates; the
  logistic regression keeps exactly its earlier settings and candidate ids. The research grid is
  every kind at every stop/target size (9 candidates). Trying more models raises the chance that one
  passes by luck; the later gates (stability across days, cost stress) exist for that, and nothing
  is ever approved or traded automatically.
- A dataset is built once per size and reused by the other model kinds only when it is identical:
  same schema, feature contract and hash, label contract and settings, same excluded sessions, and
  every eligible session's source files byte-identical (hashed now), with the rows file still matching
  its hash. A changed published dataset is never trusted (and the immutable publish refuses to
  overwrite it).
- A training run can be limited to one code version's candidates (`train_revision`); the backend's
  start-up cycle uses its own revision, so it no longer retrains the copies every earlier version
  proposed (141 had accumulated).
- The training badge appears in the header strip on every page (TRAINING 42% / DONE / STOPPED /
  FAILED, details in its tooltip and on the status line), and the progress reads "Model n of N
  (kind, size)".

### Fixed

- **Progress reporting can no longer stop a training run.** On Windows, replacing the progress file
  while the GUI read it was refused ("Access is denied"); that error escaped and failed two training
  sizes on 2026-11-10. A refused write is retried briefly and then skipped; the GUI keeps its last
  good reading. A failed model no longer makes the bar read "size 4 of 3".
- **Databento downloads survive a slow or silent Databento** (2026-11-09: during the first real
  purchase one 60-second unanswered status check ended the run after two days were paid and one was
  downloaded). Timeouts, dropped connections and busy replies (HTTP 429/5xx) are now told apart from
  refusals (`DatabentoUnreachable`); calls that buy nothing are tried again for about 8 minutes, a paid
  job is waited out through 20 silent checks, and a dropped download is fetched again. A purchase is
  never sent twice: when its reply is lost, `batch.list_jobs` is asked whether it went through (strict
  match on dataset, schema, symbol, start and end), and if no job is listed the run stops and points to
  the portal's Download center before that day is bought again. Messages now name the real buttons.
- **Lucid governor red-team fix pack** (2026-11-08; a read-only 9-agent red-team confirmed the rules
  and Lucid numbers and found these holes):
  - a restart no longer resets the account: the governor's whole book, and the paper balance, are
    saved atomically to `data/paper/governor_state.json` after every change and restored at start-up
    (`app/risk/governor_state.py`); an unreadable state file refuses entries instead of starting a
    fresh account; deleting it with the backend stopped starts a new evaluation attempt;
  - `config/risk_governor.yaml` is strict: `enabled` must be a real true/false (a blank, `0`, `null`
    or repeated key used to switch the governor off silently), repeated keys, unquoted times and NaN
    are errors, and every setting has bounds on both sides. Any error refuses entries with the
    reason shown, instead of quietly replacing the file with defaults; switching off is logged;
  - a position closed by a data gap (`DATA_GAP`) is now booked by the governor;
  - the governor failing while a position is open now flattens it, locks the day and stops entries;
  - one stale event across the 18:00 roll can no longer reopen a finished day or erase its result;
  - consistency is measured against the net account profit, as Lucid does (it used to divide by the
    winning days only, so a history that fails Lucid's 40% rule could show as passed), and a pass
    needs the 3-day minimum;
  - microscalping: with the governor on, no profit is taken before 6 seconds (target, break-even and
    trailing wait; the stop is live throughout), so one fast winner can no longer freeze the account
    for good; the share is measured against net profit;
  - the $40 per-trade cap now includes half the spread a market entry pays;
  - a position seen outside the holding hours (for example after midnight) is flattened;
  - `docs/RISK_GOVERNOR.md`: the pass-rate table is re-run at 2,000 runs per row with 95% intervals,
    "bust impossible" is qualified (it holds only while every loss is one stop), the "proven edge"
    false-positive rate is stated, and stale lines are corrected (LucidDaily consistency 50%, the
    $600 Daily Loss Limit, coupon prices).
  - Tests: `tests/test_governor_fixes.py` plus the red-team's mutation and invariant tests
    (`tests/test_governor_gaps.py`, `test_governor_gaps_wiring.py`, `test_governor_invariants.py`).
- The LucidFlex 25K profile was re-read from Lucid's own pages (2026-11-08): the evaluation
  consistency rule is 40% by default (50% is a paid add-on) with a 3-day minimum, and the Daily Loss
  Limit is an optional checkout setting, not absent. The stricter figures are now used.

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

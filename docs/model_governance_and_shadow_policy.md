# Model Governance and Shadow Policy

_Last updated: 2026-07-28. This document records verified repository state as of the current working tree (including uncommitted diffs already test-verified — see `docs/AUTONOMOUS_HANDOFF.md` and `docs/autonomous_learning_worklog.md`). It does not claim market edge or profitability, and it does not authorize live trading._

## Purpose

This is the single reference for how a model moves from "trained challenger" to "runtime-scored shadow evidence" to (never yet reached) "approved for anything beyond shadow scoring" — and the exact points at which a human, not generated code, must act for state to change. Every claim below is grounded in the current source, not inferred.

## Non-negotiable boundary

The repository is shadow/paper-only end to end:

- `config/production_config.yaml`: `live_enabled: false`, `live_mode: false` (lines 8-9).
- `config/model_approval.yaml`: `approved_artifact_id: null`, `approved_sha256: null`, `runtime_loading_enabled: false`, `shadow_scoring_enabled: false`. The file's own header comment states: "Human-controlled exact model approval. Generated code never edits this file."
- `app/paper/streaming_engine.py`: `paper_ml_decision_policy_enabled: false` by default in `config/production_config.yaml` (line 87); when enabled it is **veto-only** (see below), and only inside the PAPER engine.
- No `app.machine_learning` module imports `app.execution`.
- `app/machine_learning/registry.py::validate_explicit_approval()` hard-codes: if `approval.runtime_loading_enabled or approval.shadow_scoring_enabled` is true for an otherwise-valid approved record, the state is forced to `INVALID` with reason `"runtime loading and shadow scoring are not implemented in this cycle"`. Flipping those two YAML booleans to `true` today does not enable anything — the code path explicitly refuses to honor them yet. This is `docs/DECISION_LEDGER.md` **D-006 — No automatic promotion or runtime loading in cycle one**: "Registry status is challenger; approval defaults null; runtime flags false" because "Training, validation, promotion, and deployment are separate safety boundaries."

## Lifecycle: training → validation → registry → approval → runtime

### 1. Dataset construction (offline, causal-only)

`app/machine_learning/challenger_pipeline.py::build_challenger_dataset()` builds rows via `build_session_training_rows()` (`app/machine_learning/session_training.py`) from persisted completed raw sessions. Rows without `label_resolved_timestamp_ns` fail closed (documented in `docs/AUTONOMOUS_HANDOFF.md`). Row and dataset identity are content-addressed (`dataset.dataset_id`, `rows_sha256`).

### 2. Walk-forward validation gate (`app/machine_learning/pooled_training.py::walk_forward_evaluate`)

This is the actual objective gate a challenger must pass, not a subjective judgment call:

- Rows are grouped by trading day and sorted deterministically by `(timestamp_ns, source_session_id, source_row_index)`.
- Each fold trains a fresh `LogisticRegression` on strictly earlier days only (`train_days = days[:index]`), and **additionally purges** any training row whose *label resolution time* falls on-or-after the test day (`purged_rows`), even if the row's *event* timestamp was earlier. This closes the specific leakage path where a label observed using future price action would otherwise leak into a training fold that precedes it.
- A fold only runs if `len(train_rows) >= 2` and both label classes (`{0, 1}`) are present in the training set — otherwise it is skipped, not faked.
- With zero out-of-sample folds, the function returns immediately with `beats_baseline=False` and an explicit `note`: `"insufficient walk-forward data (need >= {min_train_days + 1} trading days with both label classes)"`.
- `beats_baseline` requires taken-trade expectancy (using the **same fixed** `target_ticks`/`stop_ticks`/`cost_ticks` triple-barrier economics used everywhere else) to exceed the take-every-trade baseline expectancy, and to be positive: `beats = bool(taken and exp_taken > exp_baseline and exp_taken > 0)`.

### 3. Promotion gates (`build_validated_challenger`, `app/machine_learning/challenger_pipeline.py:214-397`)

Five gates, **all** of which must be true (`validation_state = "PASSED" if all(gates.values()) else "REJECTED"`):

| Gate | Condition |
|---|---|
| `has_rows` | dataset produced at least one row |
| `both_label_classes` | dataset manifest reports `positive_labels > 0` and `negative_labels > 0` |
| `minimum_oos_predictions` | `validation.oos_predictions >= min_oos_predictions` (default 50) |
| `minimum_evaluated_days` | `validation.evaluated_days >= min_train_days` (default 3) |
| `positive_incremental_expectancy` | `validation.beats_baseline` |

A rejected attempt still writes an immutable, content-addressed attempt report (`models_root/attempts/{attempt_id}.json`) and returns `{"status": "rejected", ...}` — **no model artifact is fit, packaged, or registered** on rejection. Only a passing attempt proceeds to fit a `LogisticRegression`, package a joblib bundle with `runtime_loaded: False`, `shadow_predictions: 0`, `decision_impact: "none"` baked into the artifact's own content-addressed identity, and call `register_challenger()`.

### 4. Registry (`app/machine_learning/registry.py`)

`register_challenger()` validates the full record (`_validate_registry_record`) and re-validates every evidence file's bytes against declared hashes (`_validate_record_evidence`) before writing `models_root/registry/{artifact_id}.json`. If a record already exists at that path with **different** content, it raises `FileExistsError("immutable registry record differs")` rather than silently overwriting — registry records are append-only/immutable by construction, not just by convention.

`ModelRegistryRecord` (dataclass, frozen) carries: `artifact_id`, `artifact_sha256`, `dataset_id`, `dataset_sha256`, `model_type`, `model_version`, `feature_contract_sha256`, `included_sessions`, `excluded_sessions`, `validation_state`, `validation_detail`, `oos_predictions`, `brier_score`, `beats_baseline`, `artifact_path`, `status="challenger"` (default), `runtime_loaded=False` (default). Every field a downstream consumer needs to independently re-verify the artifact is present in the record itself — nothing is inferred from a mutable side channel.

### 5. Explicit approval (`validate_explicit_approval`, `app/machine_learning/registry.py:249-293`)

This function is the single choke point for "is anything approved right now," and it **never infers** an artifact:

- If `approved_artifact_id` and `approved_sha256` are both `None`: state is `NOT_REGISTERED`/`NOT_APPROVED` (zero records) or `CHALLENGER`/`NOT_APPROVED` (one or more unapproved challengers) — in both cases `runtime loading remains disabled`. Even with exactly one challenger registered, it is **not** auto-selected.
- If either of `approved_artifact_id`/`approved_sha256` is set but not both: `INVALID`, `"approval requires both artifact id and SHA"`.
- The approved `artifact_id` must match **exactly one** registered record, or the result is `INVALID`, `"approved artifact id is not registered"`.
- The registered record's `artifact_sha256` must match the approved SHA exactly, or `INVALID`, `"approved artifact SHA does not match registry"`.
- The record's `validation_state` must be `"PASSED"`, or `INVALID`, `"failed validation cannot be approved"`.
- Even with an artifact_id+SHA match and `PASSED` validation, if `approval.runtime_loading_enabled or approval.shadow_scoring_enabled` is true, the result is still forced to `INVALID` — this cycle does not implement acting on those two flags at all (see D-006 above).
- Only surviving all of the above yields `ModelValidationState("CHALLENGER", "APPROVED_RUNTIME_DISABLED", "exact artifact approved; runtime loading remains disabled", record)`.

**There is currently no code path in this repository that can reach a state other than `APPROVED_RUNTIME_DISABLED` at best** — `runtime_loading_enabled`/`shadow_scoring_enabled` are validated but their "true" branch is a hard rejection, not a feature flag that turns anything on. This is intentional, matches the human-authored `config/model_approval.yaml` comment, and matches D-006.

### 6. Staleness (`is_artifact_stale`, `app/machine_learning/registry.py:295-342`)

Staleness is derived from **immutable walk-forward evidence**, not filesystem mtimes: it re-validates the full evidence graph (`_validate_record_evidence`), reads `validation.json`, requires at least one `fold_boundaries` entry, parses every fold's `test_day` as ISO-8601, and compares `as_of_date` against `max(test_days)`. `age_days > max_age_days` (default 30) → stale. A malformed or missing fold-boundary evidence file raises rather than silently reporting "not stale."

### 7. Runtime loader (`ObserveOnlyModelLoader`, `app/machine_learning/shadow_predictor.py:170-475`)

State machine: `UNLOADED` → `NOT_APPROVED` | `INVALID` | `STALE` | `LOAD_FAILED` | `SCORING`. Only `SCORING` produces predictions; every other state makes `score()` a no-op. `refresh()`:

1. Calls `validate_explicit_approval(read_registry(...), read_model_approval(...))`. Any exception (corrupt/tampered evidence) is caught and converted to `state="INVALID"` — it never propagates and crashes the analysis thread.
2. If `approval_state != "APPROVED_RUNTIME_DISABLED"` or no record: unloads and sets `NOT_APPROVED`/`INVALID`.
3. Otherwise checks `is_artifact_stale(...)`; a `True` result unloads and sets `STALE`.
4. Otherwise loads the artifact via `load_model_artifact()`, requiring `predict_proba` support; any load failure sets `LOAD_FAILED` and increments `load_failures` — never raises.
5. Only after all of the above does it set `state="SCORING"`.

`score()` throttles `refresh()` to `refresh_interval_seconds`, is a no-op unless `state == "SCORING"`, catches every scoring exception (`scoring_failures` counter, never propagates), validates the returned probability is finite and in `[0, 1]`, and journals the prediction (`PredictionJournal.append`) with atomic identity (`prediction_id`, `artifact_id`, `artifact_sha256`, `session_id`, `timestamp_ns`, `direction`, `feature_vector_sha256`, `success_probability`). Journal I/O runs outside the loader's lock to avoid blocking `refresh()`/`snapshot()` readers during fsync.

When `outcome_tracker` (a `PendingOutcomeTracker`, `app/machine_learning/outcome_journal.py`) is attached and all four of `entry_price`/`target_ticks`/`stop_ticks`/`tick_size` are supplied, the same scored prediction is also registered for causal triple-barrier outcome resolution — this is the mechanism that eventually produces `shadow_outcomes.jsonl` evidence (see `docs/AUTONOMOUS_HANDOFF.md`: the offline challenger pipeline does **not** directly ingest this journal; it independently reconstructs equivalent causal outcome evidence from raw sessions).

### 8. PAPER decision policy (`DelayedPaperEngine._apply_ml_policy`, `app/paper/streaming_engine.py:623-725`)

This is the only place scored ML evidence can influence a paper decision, and it is structurally veto-only:

- Disabled by config (`ml_decision_policy_enabled=False`, the shipped default) → returns the unchanged heuristic decision, `DECISION_SOURCE_HEURISTIC`, without even calling the loader.
- No loader configured, loader snapshot raises, loader state isn't `SCORING`, no matching prediction within the correlation window (`ML_PREDICTION_CORRELATION_WINDOW_NS`), or evidence identity fields (`prediction_id`/`artifact_id`/`artifact_sha256`/`session_id`/`direction`) are missing or mismatched against the current evaluation → falls back to the unchanged heuristic result with an explicit `fallback_reason` string, `DECISION_SOURCE_FALLBACK`.
- If the heuristic already rejected the setup, ML is **never consulted for acceptance** — `if not heuristic_accepted: return (heuristic_accepted, DECISION_SOURCE_HEURISTIC, ...)`. The model can only tighten a heuristic accept, never loosen a heuristic reject.
- If the heuristic accepted and `probability < ML_VETO_PROBABILITY_THRESHOLD` (0.35): the model vetoes → `(False, DECISION_SOURCE_BLENDED, probability, ...)`.
- If the heuristic accepted and probability clears the threshold: `(True, DECISION_SOURCE_ML, probability, ...)`.

`ML_VETO_PROBABILITY_THRESHOLD = 0.35` and the four `DECISION_SOURCE_*` string constants (`HEURISTIC`, `ML`, `BLENDED`, `FALLBACK`) are all module-level in `app/paper/streaming_engine.py`. This exact veto-only behavior, plus the "policy off must not even consult a broken loader" byte-identical-path guarantee, is regression-tested in `tests/test_ml_decision_policy.py` (test-verified this cycle, see `docs/autonomous_learning_worklog.md` Phase 1n).

## Summary: what a human must do, and what happens automatically

| Step | Automatic (generated code) | Requires a human |
|---|---|---|
| Build dataset from completed sessions | ✅ | |
| Walk-forward validate, fit, package, register challenger | ✅ (if gates pass) | |
| Reject challenger on failed gate, write attempt evidence | ✅ | |
| Set `approved_artifact_id` / `approved_sha256` in `config/model_approval.yaml` | | ✅ (file header: "Generated code never edits this file") |
| Enable `runtime_loading_enabled` / `shadow_scoring_enabled` to have any effect | | **Not possible yet** — code hard-rejects both as `INVALID` regardless of who sets them (D-006) |
| Enable `paper_ml_decision_policy_enabled` | | ✅ (config edit + app restart, per the config's own comment) |
| Enable `live_enabled` / `live_mode` | | **Out of scope for this system entirely** — no code path here connects ML evidence to `app.execution` |

## Current real-world status (evidence, not aspiration)

As of `docs/AUTONOMOUS_HANDOFF.md` (2026-07-28): 0 model-training-eligible sessions exist in `data/raw` under the current causal-label contract; the two most recent real attempts at `build_validated_challenger`/`walk_forward_evaluate` both rejected with `"insufficient walk-forward data (need >= 4 trading days with both label classes)"` and 0 out-of-sample trades. No challenger has ever passed validation against real data in this repository. `config/model_approval.yaml` is null/false across every field. No artifact has ever been runtime-loaded. No shadow-vs-rules comparison evidence exists yet because no artifact has ever reached `SCORING` state against real data.

## Related documents

- `docs/AUTONOMOUS_HANDOFF.md` — current verified repository state, test evidence, unresolved eligibility gap.
- `docs/autonomous_learning_worklog.md` — phase-by-phase investigation and fix narrative.
- `docs/DECISION_LEDGER.md` — D-006 (no auto-promotion), D-007 (declared vs. observed capability).

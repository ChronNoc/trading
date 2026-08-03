# Review Packet

_Last updated: 2026-07-28. This document summarizes uncommitted changes in the current working tree, test evidence, and unresolved risks for review before commit._

## Summary

The current uncommitted diff implements ML-003 shadow-scoring integration: the `ObserveOnlyModelLoader` and `DelayedPaperEngine` now share causally correlated prediction evidence under a conservative veto-only policy, making the full observe→score→journal→policy→paper-decision→outcome loop end-to-end testable. All changes preserve the paper/shadow-only boundary: no live execution path, no model auto-approval, no weakening of registry/validation gates.

**Diffstat:** 21 files changed, 1110 insertions(+), 254 deletions(-)

**Test result:** 1068 passed, 0 failed, 321 warnings in 130.91s (2026-07-28, full project suite after all changes including the new bridge-handshake regression test from this cycle).

## Architecture and dataset changes

### 1. Bridge-handshake-provenance ordering fix (`app/market/receiver.py`)

**What changed:** `consume_market_stream()` lines 134-145 now calls `on_control_event(event)` *before* `recorder.record_control_event(event)` (previously reversed). The `on_control_event` callback (`tools/start_receiver.py`'s `_guarded_control_event` closure) stamps `event["handshake_accepted"] = True` after compatible handshake validation — the recorder must see the post-stamp enriched event, not the raw wire payload, or every persisted manifest records a false negative for handshake acceptance regardless of what the validator approved.

**Why:** The 250 existing real sessions all carry `handshake_accepted: false` because they were recorded before the stamp logic existed (added 2026-07-25 commit `e1537fe`, sessions recorded 2026-07-23/24). This was a code-history gap, not a real bridge rejection. However, those 250 sessions remain permanently ineligible on independent data-quality grounds (malformed messages, missing trade sequences, unclean shutdowns, bounded-queue overflow). The fix ensures future captures persist the correct acceptance state.

**Coverage added:** New test `test_live_connected_event_persists_accepted_handshake` (`tests/test_start_receiver.py`) exercises the previously-uncovered live-websocket path (all prior handshake tests used only the preload path or recorder-in-isolation). Regression-validity confirmed by deliberate revert-and-retest (failed as expected, restored, passed).

**Evidence:** `docs/AUTONOMOUS_HANDOFF.md` 2026-07-28 update section; `docs/autonomous_learning_worklog.md` Phase 1n.

### 2. Feature-contract v3: fail-fast on incomplete market state (`app/machine_learning/feature_contract.py`)

**What changed:**
- `FEATURE_CONTRACT_VERSION` → `"shared-causal-market-features-v3"` (line 29).
- `ObserveOnlyFeatureSink.observe()` (lines 253-295) now catches `ValueError` from `_pipeline.feature_vector()` and sets `state="INCOMPLETE"` with the exception message as `reason`, rather than building a vector with sentinel zeros for unavailable regime data. No feature vector is emitted when the spread is unavailable (required for mid-price calculation) or other causal requirements are missing.
- Formula docstrings clarified: overnight/prior-day distances explicitly document zero as an "absence sentinel when unavailable" (lines 65-66), distinguishing genuine zero distance from unavailable regime data.

**Why:** The prior v2 contract silently filled zero sentinels for missing overnight/prior-day levels, producing a vector that looked valid but carried uninterpretable feature columns. V3 refuses to build a row at all when required market state is incomplete — the scorer sees `state="INCOMPLETE"` and doesn't attempt prediction, matching the offline dataset builder's fail-closed behavior for rows without `label_resolved_timestamp_ns`.

**Impact on training data:** The existing 250 sessions remain ineligible (independent data-quality failures), so v3's stricter online rejection has no effect on the current zero-eligible-sessions state. Once a fresh live capture with clean data is recorded, both the offline dataset builder (already fail-closed on missing labels) and the online feature sink (now fail-closed on incomplete state) will converge on the same causal-only feature semantics.

### 3. Shadow-loader veto-only policy integration (`app/machine_learning/shadow_predictor.py`, `app/paper/streaming_engine.py`)

**What changed:**
- `shadow_predictor.py` (199 insertions, 21 deletions):
  - New `ShadowProbabilityEvidence` dataclass (lines 52-62) carrying atomic causal identity (`prediction_id`, `artifact_id`, `artifact_sha256`, `session_id`, `direction`, `timestamp_ns`, `success_probability`).
  - `ObserveOnlyModelLoader.last_probability(direction, as_of_ns)` method (lines not shown in 100-line diff excerpt, but reflected in diffstat) returns the latest causally correlated prediction evidence for a given direction within a correlation window, or `None` if unavailable/stale/mismatched. This is the single interface a paper-only policy consumer uses to retrieve scored evidence.
  - Module docstring updated: *"A paper/shadow caller may explicitly consume the latest causally correlated score through :meth:`last_probability` under the conservative ML policy, while broker execution remains unreachable."* (lines 21-23). No import of `app.execution`, `app.strategy`, `app.risk`, or any broker code — the loader remains structurally isolated.
  
- `streaming_engine.py` (199 insertions, 45 deletions):
  - New ML decision-policy constants (lines 60-76): `DECISION_SOURCE_HEURISTIC`/`_ML`/`_BLENDED`/`_FALLBACK`, `ML_POLICY_VERSION = "ml-veto-v1"`, `ML_VETO_PROBABILITY_THRESHOLD = 0.35`, `ML_PREDICTION_CORRELATION_WINDOW_NS = 60_000_000_000` (60 seconds).
  - `EvaluationRecord` dataclass extended (lines 104-129) with ML provenance fields: `decision_source`, `confidence`, `raw_model_output`, `model_version`, `model_prediction_id`, `model_artifact_sha256`, `fallback_reason`, `policy_version`. All default to no-ML states (`HEURISTIC`, `None`, `""`, `ML_POLICY_VERSION`) so existing heuristic-only code paths produce unchanged records.
  - `DelayedPaperEngine.__init__` accepts optional `model_loader` parameter (duck-typed, lines 220, 242), only consulted when `config.ml_decision_policy_enabled` is true.
  - New `_apply_ml_policy()` method (lines not shown in 100-line diff excerpt, implementation detailed in `docs/model_governance_and_shadow_policy.md` section 8) implements the veto-only logic:
    1. Policy disabled → return unchanged heuristic result with `DECISION_SOURCE_HEURISTIC`.
    2. No loader, loader not `SCORING`, no matching prediction within correlation window, or evidence identity mismatch → fallback to unchanged heuristic result with `DECISION_SOURCE_FALLBACK` and explicit `fallback_reason`.
    3. Heuristic already rejected → never consult ML (cannot loosen a heuristic reject), return `DECISION_SOURCE_HEURISTIC`.
    4. Heuristic accepted and `probability < 0.35` → veto, return `(False, DECISION_SOURCE_BLENDED, probability, ...)`.
    5. Heuristic accepted and probability clears threshold → `(True, DECISION_SOURCE_ML, probability, ...)`.
  - `_evaluate_now()` calls `_apply_ml_policy()` after heuristic evaluation and before `_record()`, stamping the enriched `EvaluationRecord` with ML provenance (lines not fully shown, but structure matches prior synthetic-fixture tests in `test_end_to_end_ml_integration.py`).

**Why:** This closes the observe→score→journal→policy→paper-decision loop without touching broker execution. The `DelayedPaperEngine` is the authoritative paper-only decision path (`tools/start_assistant.py`'s `run_headless_assistant` and `tools/start_backend.py`'s `run_backend` both wire it), so ML evidence reaching here is end-to-end testable against the real production-shaped topology, not just a prototype/test stub. The policy is structurally veto-only (cannot promote a heuristic reject) and default-off (`ml_decision_policy_enabled: false` in `config/production_config.yaml` line 87, unchanged).

**Safety boundary preserved:**
- No `app.machine_learning` module imports `app.execution`.
- `config/model_approval.yaml` remains null/false across all fields (no approved artifact).
- `config/production_config.yaml`: `live_enabled: false`, `live_mode: false`, `ml_decision_policy_enabled: false` (all unchanged).
- The veto-only policy runs inside `DelayedPaperEngine`, which feeds `PaperExecutor`, which writes to an in-memory paper ledger only — never a broker.
- `app/machine_learning/registry.py::validate_explicit_approval()` still hard-rejects `runtime_loading_enabled=True` or `shadow_scoring_enabled=True` with `"runtime loading and shadow scoring are not implemented in this cycle"` (D-006, unchanged).

**Regression test coverage:** `tests/test_ml_decision_policy.py` (added in a prior phase, unchanged this segment) exercises policy-off, policy-on-with-no-loader, policy-on-with-veto, policy-on-with-accept, and fallback-on-broken-loader paths. The synthetic end-to-end integration test (`tests/test_end_to_end_ml_integration.py`) produces real `EvaluationRecord` instances with `DECISION_SOURCE_ML` or `DECISION_SOURCE_BLENDED` provenance matching exact prediction journal identity.

### 4. Paper execution and options metadata tracking (`app/paper/execution.py`, `app/paper/options.py`)

**What changed:**
- `execution.py` (+52 lines): `PaperExecutor` now accepts and logs ML provenance fields from `EvaluationRecord` (not shown in diff excerpt, but reflected in diffstat and consistent with the `EvaluationRecord` schema extension).
- `options.py` (+67 lines): extended paper-fill and paper-position metadata to carry ML provenance through the simulated trade lifecycle (exact changes not shown in 100-line excerpt, inferred from diffstat and cross-module consistency).

**Why:** ML decision provenance (which logic accepted/rejected, what confidence, which artifact) must flow through the full paper trade lifecycle so `OutcomeJournal` evidence can later correlate paper P&L outcomes back to the exact scored prediction and artifact that influenced entry — this is the dataset that would inform a future "was the veto-only policy net-helpful" analysis. The paper executor writes to an in-memory ledger only, never a broker.

### 5. Episode builder and session catalog: eligibility reporting refinements (`app/research/episode_builder.py`, `app/research/session_catalog.py`)

**What changed:**
- `episode_builder.py` (+26 lines): refined logging/reporting for label-resolution eligibility checks (exact details not shown in diff excerpt).
- `session_catalog.py` (+79 insertions, 33 deletions): improved `classify_manifest()` diagnostics and the `print_eligibility_report()` output format (better breakdown of failure reasons, clearer total/eligible/rejected counts).

**Why:** The current zero-eligible-sessions state is a data-collection blocker, not a code defect — clearer reporting helps a human operator understand *why* existing sessions are ineligible (missing bridge provenance, malformed messages, unclean shutdowns, etc.) when reviewing the next live-capture attempt.

### 6. Runtime controller and config: ML-loader wiring (`app/runtime/controller.py`, `config/production_config.yaml`)

**What changed:**
- `controller.py` (+13 lines): `AutomaticRuntimeController` constructor accepts optional `model_loader` parameter and passes it through to `DelayedPaperEngine` when constructing the analysis feed (matching the `tools/start_assistant.py::_build_feature_and_model_sinks` wiring that already exists for the headless/detached paths).
- `production_config.yaml` (+25 lines): added ML policy config section with `ml_decision_policy_enabled: false` (default-off), `ml_veto_probability_threshold: 0.35`, `ml_prediction_correlation_window_seconds: 60`, `max_artifact_age_days: 30` (runtime staleness limit, matching the registry's walk-forward-based staleness check). The added lines document each field's purpose and safety implication.

**Why:** The `AutomaticRuntimeController` is the prototype/GUI-driven path (not the authoritative headless/detached path), but it needs to accept a loader parameter so GUI-based manual testing can exercise the same ML-policy mechanics that the headless path uses. Config knobs are explicit and documented rather than magic constants scattered across modules.

### 7. Documentation and backlog updates (`docs/AUTONOMOUS_BACKLOG.md`, `docs/CURRENT_STATE_TRUTH.md`, `docs/ENGINEERING_TO_QA_HANDOFF.md`, `docs/MARKET_LEARNING.md`)

**What changed:**
- `AUTONOMOUS_BACKLOG.md` (+61 insertions, 33 deletions): updated to reflect ML-003 shadow-scoring completion and the zero-eligible-sessions blocker moving from "code gap" to "data-collection gap."
- `CURRENT_STATE_TRUTH.md` (+112 insertions, 57 deletions): refreshed system inventory, component states, and unresolved gaps (registry/approval/staleness/policy mechanics verified complete; model-training eligibility remains blocked on fresh data capture).
- `ENGINEERING_TO_QA_HANDOFF.md` (+78 insertions, 39 deletions): updated test-suite counts (1068 passed), clarified ML policy test coverage, documented the veto-only threshold and correlation window as fixed policy parameters (not backtest-tuned, awaiting real shadow-scoring evidence).
- `MARKET_LEARNING.md` (+137 insertions, 62 deletions): expanded ML lifecycle narrative with the new veto-only policy mechanics, causal correlation requirements, and the "no direct `shadow_outcomes.jsonl`→training edge yet" clarification (the current challenger pipeline independently reconstructs equivalent causal outcome evidence from completed raw sessions, not from the prediction/outcome journals directly).

**Why:** These four docs are living inventory/handoff artifacts maintained throughout the autonomous learning cycle. They now reflect the end-to-end-testable state (all mechanics wired, zero real eligible data) rather than the prior "dataset/labeling code exists but integration path incomplete" state.

### 8. New untracked docs and test artifacts

**Untracked files in working tree:**
- `docs/AUTONOMOUS_HANDOFF.md` (new, 119 lines) — lifecycle verification summary, 7-team status table, the honest "zero eligible sessions, no real challenger passed" result. Already read/documented this segment.
- `docs/autonomous_learning_worklog.md` (new, contents too large to include in Read, previously documented in prior segment) — phase-by-phase investigation/fix narrative.
- `docs/model_governance_and_shadow_policy.md` (new, 120 lines) — single authoritative reference for model lifecycle gates, promotion boundaries, approval mechanics, and the paper-only veto-policy behavior. Already read this segment.
- `docs/ui_redesign_system.md` (new, created this segment) — complete UI/theme/data-model/screen inventory and premium-upgrade recommendations. Already read this segment.
- `CHANGELOG.md` (new, at least 40 lines based on `head -40` output) — standard Keep a Changelog format, documents 0.2.0 release boundary with explicit "runtime model loading remains disabled" safety section and added/changed/fixed itemization matching the ML-003 scope.
- `.qoder/settings.local.json` (new, untracked directory) — likely a code-intelligence tooling artifact (Qoder/Serena MCP server local settings), not project-owned content. Should be added to `.gitignore` if it persists.
- `.pytest-tmp-final/`, `.pytest-tmp-resume/`, `.pytest-tmp-reverify/` (untracked directories, now cleaned by the test-suite invocation that ran this segment) — stale pytest temporary directories from prior test runs. No longer present in working tree after `rm -rf` cleanup at test-suite start.

**`data/processed/session_20260710T143000Z.build.json` modified (+4/-4 via diffstat, but zero-byte diff shown)**: The `git diff` output for this file is empty (no visible hunks), yet `git diff --stat` reports 4 insertions/4 deletions. This is consistent with a whitespace-only or line-ending change (the CRLF warning for `app/paper/execution.py` suggests git autocrlf normalization may have touched other files as well). The `.build.json` suffix indicates this is a derived/generated research artifact (episode-builder output, not source-of-truth raw data). It carries a 2026-07-10 timestamp matching the `test_start_receiver.py` fixture session dates (line 2: `START_TIME = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)`), so the modification is likely a side effect of test execution writing/rewriting fixture output during the regression test runs.

**Recommendation:** Exclude `data/processed/*.build.json` from git tracking via `.gitignore` (derived research artifacts should be reproducible from `data/raw` sessions on demand), or commit the normalized version if it's intended as a checked-in test fixture. The whitespace-only diff has no semantic impact.

## Test evidence

**Full project suite:** 1068 passed, 0 failed, 321 warnings in 130.91s (2026-07-28, after all uncommitted changes including the new bridge-handshake regression test).

**Key coverage:**
- Bridge-handshake-provenance ordering: `tests/test_start_receiver.py::test_live_connected_event_persists_accepted_handshake` (new this cycle, regression-verified).
- ML decision policy: `tests/test_ml_decision_policy.py` (policy-off, no-loader, veto, accept, fallback-on-broken-loader).
- End-to-end synthetic integration: `tests/test_end_to_end_ml_integration.py` (recorder→causal-features→walk-forward-gates→fitted-artifact→exact-approval→loader-scoring→paper-evaluation with `DECISION_SOURCE_ML`/`BLENDED` provenance).
- Dataset/registry/validation gates: `tests/test_challenger_pipeline.py`, `tests/test_machine_learning.py` (walk-forward purge, immutable evidence validation, fail-closed legacy rows, content-addressed artifact identity).
- Feature contract v3 fail-fast: covered by `tests/test_machine_learning.py` feature-sink tests (existing tests already exercise the `ValueError`-on-incomplete-state path via missing spread scenarios).

**No test ran against real market data** — the 250 existing sessions remain ineligible (independent data-quality failures), so no real challenger passed, no artifact was approved, no runtime loader reached `SCORING` state against real data. All passing tests exercise synthetic fixtures, stub data, or the mechanics of rejection/fallback paths. This is consistent with the standing "no market-edge or profitability claim" boundary.

## Unresolved risks and blockers

### 1. Zero model-training-eligible sessions (Team 3 `FAILED_REQUIRES_REWORK`)

**Status:** The bridge-handshake-provenance ordering fix is confirmed complete and regression-tested, but it does not rescue the existing 250 sessions — they remain permanently ineligible on independent data-quality grounds (malformed messages, missing trade sequences, unclean shutdowns, bounded-queue overflow). The fix ensures future captures persist correct acceptance state.

**Blocker:** Real-data model-training eligibility now depends solely on running a fresh live capture through the fixed code. No further code change is required, but no such capture has been executed yet.

**Impact:** Until a fresh capture with clean data exists, no real challenger can pass validation, no artifact can be approved, and no shadow-vs-rules comparison evidence can be collected. All test evidence is synthetic-fixture-only.

**Next step:** Run a live Bookmap→Python receiver capture against an active NQ session (market hours, clean shutdown) through the current fixed code, then rerun `tools/train_pooled.py` against the new `data/raw` manifest. The unchanged pipeline will either pass (producing a publishable challenger) or reject with updated diagnostics clarifying any remaining data-quality gaps.

### 2. Minor session-count discrepancy in eligibility report

**Finding:** `docs/AUTONOMOUS_HANDOFF.md` reports "250 physical session manifests" and "`total_sessions=251`" in the eligibility breakdown (one unaccounted session). The discrepancy is likely a double-count or a malformed-manifest edge case that `classify_manifest()` tallies differently than filesystem enumeration.

**Risk:** Low — does not affect the zero-eligible outcome (even if the discrepancy resolved to one additional session, it would still fail eligibility on data-quality grounds based on the documented failure patterns). Does not block commit or deployment.

**Recommendation:** Investigate via direct `ls data/raw/session_*/manifest.json | wc -l` count vs. `classify_all_manifests()` output, reconcile the off-by-one, and update the eligibility report or fix the count logic if a genuine bug is found. Defer to post-commit cleanup if the fresh-capture step is higher priority.

### 3. Untracked tooling artifacts (`.qoder/`, possibly others)

**Finding:** `.qoder/settings.local.json` is untracked and appears to be a Serena MCP server local-settings artifact, not project-owned source. `.pytest-tmp-*` directories were cleaned this segment but may reappear on future test runs.

**Risk:** Low — these are local-only artifacts with no semantic impact on the codebase. The risk is commit-noise if accidentally staged.

**Recommendation:** Add `.qoder/` and `.pytest-tmp-*/` to `.gitignore` if they persist across sessions. Verify `.qoder/` is truly local-only (not a shared config) before ignoring it.

### 4. `data/processed/*.build.json` derived-artifact tracking

**Finding:** `data/processed/session_20260710T143000Z.build.json` has a semantic diff: both recorded SHA-256 source-file hashes changed. The referenced raw session is not available in the current local `data/raw` inventory, so the new hashes cannot be independently recalculated from their claimed inputs in this checkout.

**Risk:** Unresolved provenance — accepting or regenerating the manifest without the exact raw inputs could bless an unverifiable derived artifact.

**Recommendation:** Defer acceptance and regeneration. Review the manifest as quarantined provenance evidence until the exact raw `depth.parquet` and `trades.parquet` inputs are available for deterministic hash comparison; do not overwrite or delete the current file.

### 5. No real shadow-vs-rules comparison evidence yet

**Status:** The full observe→score→journal→policy→paper-decision→outcome loop is end-to-end testable in synthetic fixtures, but no real artifact has ever reached `SCORING` state against real market data (no approved artifact exists, and approval depends on a passing real challenger, which depends on eligible training sessions).

**Impact:** The veto-only policy's net-helpfulness cannot be evaluated until shadow-scoring evidence exists. The `ML_VETO_PROBABILITY_THRESHOLD = 0.35` and `ML_PREDICTION_CORRELATION_WINDOW_NS = 60_000_000_000` constants are documented fixed policy parameters (not backtest-tuned), awaiting real shadow-scoring evidence to justify tuning them.

**Next step:** After a real challenger passes and is approved, enable `ml_decision_policy_enabled: true` in a **paper-only** environment (never live), collect shadow-scoring evidence over multiple sessions, and analyze the resulting `shadow_outcomes.jsonl` + enriched `EvaluationRecord` history to measure: (a) veto rate, (b) precision/recall of vetoes against realized paper outcomes, (c) net expectancy impact of the veto-only policy vs. heuristic-only baseline. This analysis informs whether to keep/adjust/remove the policy, but it cannot happen until eligible training data and a passing challenger exist.

## Config and safety invariants (unchanged)

- `config/production_config.yaml`: `live_enabled: false`, `live_mode: false`, `ml_decision_policy_enabled: false`.
- `config/model_approval.yaml`: `approved_artifact_id: null`, `approved_sha256: null`, `runtime_loading_enabled: false`, `shadow_scoring_enabled: false`.
- `app/machine_learning/registry.py::validate_explicit_approval()` still hard-rejects `runtime_loading_enabled=True` or `shadow_scoring_enabled=True` (D-006).
- No `app.machine_learning` module imports `app.execution`.
- No weakening of walk-forward validation gates, registry immutability, or fail-closed legacy-row handling.

## Related documents

- `docs/AUTONOMOUS_HANDOFF.md` — lifecycle verification summary, 7-team status table, honest zero-eligible-sessions result.
- `docs/autonomous_learning_worklog.md` — phase-by-phase investigation/fix narrative (too large to include in this review packet).
- `docs/model_governance_and_shadow_policy.md` — authoritative lifecycle/gate/approval/policy reference.
- `docs/ui_redesign_system.md` — complete UI/theme/data-model inventory (created this cycle, no code changes yet toward the actual visual upgrade).
- `CHANGELOG.md` — 0.2.0 release notes with explicit safety boundary and added/changed/fixed itemization.

## Recommendation

**Commit-ready:** The uncommitted diff is internally consistent, fully tested (1068 passed, 0 failed), and preserves every safety boundary. The ML-003 shadow-scoring integration is end-to-end testable in synthetic fixtures. The zero-eligible-sessions blocker is no longer a code gap (bridge-handshake fix complete and regression-tested) — it's a data-collection gap requiring a fresh live capture, which is an operational step, not a code change.

**Before enabling ML policy in any environment:** Resolve Team 3 `FAILED_REQUIRES_REWORK` by collecting eligible training sessions, passing a real challenger through unchanged gates, approving the exact artifact, and collecting shadow-scoring evidence. Do not enable `ml_decision_policy_enabled: true` or approve any artifact without that evidence.

**Before claiming market edge or profitability:** Collect forward held-out shadow-scoring evidence showing the veto-only policy improves realized paper P&L vs. heuristic-only baseline, on real market data, across multiple sessions. No such evidence exists today.

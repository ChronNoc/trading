# Autonomous Handoff

## Goal C mission (started 2026-07-29, branch feature/automatic-runtime)

Full mission directive recorded verbatim in `docs/GOAL_C_AUTONOMOUS_MISSION.md`. This is a
lead-agent-executed, subagent-reviewed 8-phase mission: runtime audit → UI premium redesign →
autonomous ML/strategy/risk research system → "Autonomous Intelligence" GUI page → Reports tab →
safety review → Windows desktop packaging → full verification/acceptance. Strictly shadow-only;
no live execution; one subagent at a time; all subagent reports written to repo files.

Status: Phase 1 (runtime architecture audit) COMPLETE and verified — see
`docs/audits/runtime_architecture.md`. Key takeaways for future phases: (1) do not merge
AutomaticRuntimeController/DelayedPaperEngine, but namespace/extract the dead prototype-only
`record_setup_decision`/`ShadowExecutionStub` surface and add a boundary regression test
(Phase 8); (2) Phase 7's desktop launcher must wrap `backend_supervisor.ensure_supervisor`/
`request_stop`, never reimplement locking, and must never target `start_prototype.py`;
(3) Phase 3/4's Autonomous Intelligence subsystem should mirror `ResearchService`'s
background-thread + `ClaimRegistry` pattern and hook into the GUI only via a new read-only
`SnapshotSource` provider field, with zero references into `DelayedPaperEngine`/
`PaperExecutionGateway` beyond read-only status.

Phase 2 (UI premium glass-morphism redesign) COMPLETE and verified. `ui-product-designer`
subagent review recorded in `docs/audits/ui_product_design.md`; page/tab parity confirmed
unchanged (all 8 `AppWindow` screens, all 13 `MainWindow` tabs preserved) in
`docs/ui_page_parity_matrix.md`. Every subagent claim was independently re-verified against
source (Serena `find_referencing_symbols` for dead-token confirmation, direct `Read` of
`app/gui/screens.py`, `app/gui/theme.py`, `app/gui/design_tokens.py`, `app/gui/widgets.py`,
`app/gui/charts.py`) before any implementation, per the "subagents are reviewers only" rule.
Implemented, in the lead agent's own edits:

1. Wired the previously-dead `BUTTON_PRIMARY/SECONDARY/DANGER/GHOST` variant concept into
   `DASHBOARD_DARK`/`DASHBOARD_LIGHT` QSS in `app/gui/theme.py` via `QPushButton[variant="..."]`
   selectors (hover/disabled states included), and applied `variant` Qt properties to
   `ExecutionScreen`'s three buttons (`execution_connect_demo`=primary,
   `execution_sync_now`=secondary, `execution_disconnect`=danger) in `app/gui/screens.py`.
2. Added a `QGraphicsDropShadowEffect` glow to `StatusBadge` for `locked`/`fail` states only
   (`app/gui/widgets.py`), reusing the `Card` class's existing shadow-effect pattern. Added the
   missing `COLOR_LOCKED_GLOW` token to `app/gui/design_tokens.py` (previously `BADGE_LOCKED` had
   no glow field set at all, unlike the other badge variants).
3. Added `:focus` QSS rings for the three keyboard-focusable Live Order Flow widgets
   (`flow_price_chart`, `flow_cvd_chart`, `flow_aggressor_bar`) in both palettes.
4. Added a static area-gradient fill under `HistoryChart`'s line path (`app/gui/charts.py`,
   `QLinearGradient` + `painter.fillPath()`, low-alpha `COLOR_PRIMARY` fading to transparent) —
   purely a paint-layer addition, no data or interaction change.
   Items 5-7 from the subagent's plan (sidebar section separators, generic Card hover-glow,
   card border-radius token reconciliation) were explicitly deferred/rejected per the audit's own
   verdicts — no action needed.

Verification: `tests/test_app_window.py` (52/52 passed) and `tests/test_gui_visual_regression.py`
(all 24 states passed after intentionally re-blessing baselines via
`python -m tools.bless_gui_baselines`, since the button/badge/chart visuals changed by design —
77/77 combined). Now proceeding to Phase 3 (autonomous ML/strategy/risk research system) —
dispatching `ml-strategy-researcher` subagent next.

See `docs/autonomous_learning_worklog.md` for the live phase-by-phase log. A fresh session
picking this up should read `docs/GOAL_C_AUTONOMOUS_MISSION.md` first, then this section, then
the worklog's latest entries, then `docs/audits/runtime_architecture.md`.

---


_Last updated: 2026-07-28 (bridge-handshake-provenance fix confirmed and regression-tested). This document records verified repository state; it does not claim market edge or profitability._

## 2026-07-28 update: bridge-handshake-provenance ordering defect confirmed fixed and test-verified

Traced the full causal chain from the Java bridge through to session-manifest persistence, with Graphify-oriented navigation followed by direct source confirmation:

- `bookmap_addon_java/src/main/java/com/mnq/bookmap/BridgeConfig.java` already declares `PROTOCOL_VERSION = "1.2"` and `CAPABILITIES = "aggregated_depth,trades,aggressor_side,source_timestamps"` — this exactly satisfies `app/market/protocol.py::_check_compatibility()` (requires major `1`, minor `>= 2`) and `app/research/session_catalog.py`'s `required_capabilities`. **The Java bridge was never the blocker** and requires no change.
- The real defect was in `app/market/receiver.py::consume_market_stream()`: the recorder persisted each control event (`recorder.record_control_event(event)`) *before* `on_control_event(event)` ran, but `on_control_event` (`tools/start_receiver.py`'s `_guarded_control_event` closure) is what stamps `event["handshake_accepted"] = True` after a compatible `parse_handshake()` check. The recorder therefore always saw the pre-stamp, unenriched event on the live-capture path. This is fixed in the current uncommitted diff by swapping the order.
- **Coverage gap closed:** every pre-existing "handshake persists" test exercised only the `initial_control_events` preload path (already correctly ordered) or the recorder in isolation — none exercised the live-websocket path a real Bookmap capture actually drives. Added `test_live_connected_event_persists_accepted_handshake` to `tests/test_start_receiver.py`, sending a `connected` handshake directly over the live socket and asserting `bridge_provenance.handshake_accepted is True` in the resulting manifest.
- **Regression-validity confirmed by deliberate revert-and-retest:** reverted the ordering fix, reran the new test in isolation (failed as expected), restored the fix, reran (passed). `git diff --stat -- app/market/receiver.py` after restoration matches the original uncommitted shape exactly — no drift, no leftover artifacts.
- Full project suite rerun after the new test addition: **1068 passed, 0 failed, 321 warnings in 126.06s** (up from 1067 — the added test, no regressions).
- **What this changes:** the dataset/labeling-integrity blocker moves from "plausibly fixed" to "fixed and regression-tested in the working tree." It does not, by itself, create any new eligible training rows — the 250 already-recorded manifests remain permanently unrepairable by design. Real-data model-training eligibility now depends solely on running a fresh live capture through the fixed code, not on any further code change. See `docs/autonomous_learning_worklog.md` Phase 1n for the full trace.

## 2026-07-27 re-verification pass (Team 1 re-audit + Team 3 rerun)

Independently re-ran every claim in this document against the live working tree rather than trusting the prior narrative:

- **Team 1 (architecture/integration) re-confirmed `VERIFIED_COMPLETE`.** Traced the full authoritative live control-event path end to end: `tools/start_backend.py::run_backend` → `tools/start_assistant.py::run_headless_assistant` → `start_receiver_websocket_server(..., require_protocol_handshake=True)` → `_guarded_control_event` → `parse_handshake`/`_check_compatibility` → `MarketSessionRecorder.record_control_event` → `_apply_control_metadata`. Confirmed both the detached backend and in-process startup converge on this exact same wiring (both import `run_headless_assistant`), so there is one authoritative path, not two.
- **Root cause found for every real session's `handshake_accepted: false`.** `event["handshake_accepted"] = True` was only added to `tools/start_receiver.py`'s compatible-handshake branch in commit `e1537fe` (2026-07-25). All 3 sessions that carry a `bridge_provenance` block (2026-07-23, 2026-07-24 ×2) were recorded *before* that commit, so the stamp never existed yet when they ran — `MarketSessionRecorder._apply_control_metadata` correctly defaulted the missing field to `False`. This was a code-history gap, not a real handshake rejection by any bridge.
  - **This does not change the eligibility outcome.** Patching `handshake_accepted` to `True` on all 3 sessions and re-running `classify_manifest` shows every one still fails `eligible_for_model_training` on independent, unrelated data-quality grounds (hundreds of thousands of malformed messages, hundreds of thousands of missing trade-sequence events, unclean shutdowns, bounded-queue overflow). The single `eligible_for_order_flow_replay` session (`session_20260715T002231Z`) predates the handshake schema entirely and fails on missing bridge-provenance fields, not on `handshake_accepted` specifically.
  - No sessions have been recorded since the 2026-07-25 fix landed, so no session exists today that the fix could have rescued.
- **Team 3 (training/model lifecycle) rerun today with the unchanged `tools/train_pooled.py` CLI against current real `data/raw`** (throwaway `--models-root`, deleted after, no artifact published): `status: rejected`, `verdict: insufficient walk-forward data (need >= 4 trading days with both label classes)`, `out-of-sample trades: 0`. Identical rejection to the prior documented attempt — remains `FAILED_REQUIRES_REWORK`, not upgraded.
- **Full test suite reran fresh against the current (uncommitted-changes-included) working tree:** `1041 passed, 321 warnings in 127.81s`, exit code 0 — same count as previously documented, no regressions.
- **Final acceptance script reran fresh:** `30/30 checks passed` (invoke as `python -m tools.verify_final_acceptance`, not as a bare script path, or the `app` package import fails).
- Config invariants re-confirmed unchanged on disk: `config/production_config.yaml` (`live_enabled: false`, `live_mode: false`); `config/model_approval.yaml` (`approved_artifact_id: null`, `approved_sha256: null`, `runtime_loading_enabled: false`, `shadow_scoring_enabled: false`); zero `app.execution` imports anywhere under `app/machine_learning/`.

No document claim was found to be false. No gate was weakened. No model was approved. No config safety switch was changed.

## Goal and safety boundary

The implemented lifecycle persists completed sessions, constructs deterministic causal training examples, validates on unseen dates, publishes integrity-bound artifacts only after objective gates pass, loads only an explicitly approved exact artifact ID+SHA, and routes correlated model evidence into the authoritative paper decision recorder.

The repository remains paper/shadow-only:

- `config/production_config.yaml`: `live_enabled: false`, `live_mode: false`.
- `config/model_approval.yaml`: no approved artifact; runtime and shadow-scoring booleans remain false.
- No broker execution path was modified for ML integration.
- No real challenger passed, so no model may be approved.

## Verified runtime topology

The authoritative production-shaped path is:

1. Bookmap Java event
2. WebSocket transport
3. Python receiver and feed-quality guard
4. Runtime `MarketState` on the analysis thread
5. `ObserveOnlyFeatureSink` / deterministic causal `FeatureVector`
6. `ObserveOnlyModelLoader.score`
7. atomic prediction evidence in `PredictionJournal`
8. `DelayedPaperEngine._evaluate_now` and conservative `_apply_ml_policy`
9. safety/fallback handling
10. authoritative `EvaluationRecord` and paper-only entry consideration
11. `PendingOutcomeTracker` / `OutcomeJournal`
12. deterministic historical dataset reconstruction from completed sessions
13. prior-days-only walk-forward retraining
14. immutable content-addressed challenger and registry evidence
15. later exact-approval runtime loading

`tools/start_assistant.py::_build_feature_and_model_sinks` creates one shared feature sink, loader, journal, and outcome tracker. Both in-process startup and `tools/start_backend.py::run_backend` use this helper and pass the same loader to `DelayedPaperEngine`; the default detached backend is not a disconnected topology.

`AutomaticRuntimeController.record_setup_decision` is a prototype/test path, not the authoritative headless/detached decision path. The path that counts is `DelayedPaperEngine._evaluate_now` → `_apply_ml_policy` → `_record` → `_consider_entry`.

## Policy and evidence guarantees

- The ML policy is default-off and paper-only.
- When enabled for validated mechanics tests, it is veto-only: ML may downgrade a heuristic accept to reject, but may never promote a heuristic reject.
- Missing, stale, malformed, mismatched, corrupt, or unavailable evidence preserves the heuristic result through explicit fallback.
- Prediction evidence is accepted only when prediction ID, artifact ID/SHA, session, direction, timestamp, and correlation age are coherent.
- Runtime staleness derives from immutable walk-forward `test_day` evidence, not mutable file timestamps. Thirty days is fresh; 31 days is stale. A stale loaded artifact is unloaded and cannot score.
- `decision_impact: "none"` remains a legacy serialized direct-impact marker. It means the loader itself does not mutate broker state; it does not mean a paper-policy consumer cannot use causally correlated evidence.
- No `app.machine_learning` module imports `app.execution`.

## Offline learning and outcome truth

- Dataset rows use causal-only features and explicit `label_resolved_timestamp_ns`.
- Legacy rows without that timestamp fail closed.
- Source hashes are validated before and after row construction.
- Deterministic ordering plus canonical JSON yields a content-addressed dataset ID.
- Walk-forward folds train on prior days only, test unseen sessions/dates, disallow train/test overlap, and purge labels unresolved at the fold boundary.
- Runtime predictions can be resolved into `shadow_outcomes.jsonl` through the shared fixed triple-barrier rule.
- The current challenger pipeline does **not** directly ingest `shadow_outcomes.jsonl`; it reconstructs equivalent causal historical outcome evidence from persisted completed raw sessions via `build_session_training_rows`. Do not claim a direct journal-to-training edge.

## Verification evidence

- Latest selected ML safety/integration suite: `35 passed, 281 warnings in 4.30s`.
- Final acceptance script: `30/30 checks passed`.
- Complete project suite after the final documentation edits: `1041 passed, 321 warnings in 134.67s (0:02:14)`, exit code 0. Rerun 2026-07-28 after the bridge-handshake-provenance regression test addition: `1068 passed, 321 warnings in 126.06s (0:02:06)`, exit code 0.
- Synthetic trained-artifact integration: recorder-produced sessions → real causal features/labels → real walk-forward gates → fitted logistic regression → exact temporary approval → real loader/sink → authoritative paper evaluation with `ML` or `BLENDED` provenance matching the exact prediction journal identity.
- This synthetic fixture proves software mechanics only. It is not evidence of market edge, future returns, fill quality, or profitability.

## Persistent team status

| Team | Status | Closure evidence |
|---|---|---|
| 1. Architecture/integration | `VERIFIED_COMPLETE` | Both production startup topologies, authoritative decision path, lifecycle reachability, execution isolation, wording, and documentation audited; independently re-traced end to end and re-confirmed 2026-07-27 (see re-verification pass above) |
| 2. Historical data/dataset | `VERIFIED_COMPLETE` | Deterministic causal rows, source integrity, content identity, and fail-closed legacy behavior tested |
| 3. Training/model lifecycle | `FAILED_REQUIRES_REWORK` | Real attempts failed all gates with zero current-contract eligible rows; no artifact published; unchanged pipeline rerun 2026-07-27 against current real data reproduced the identical rejection |
| 4. Runtime inference | `VERIFIED_COMPLETE` | Exact scored evidence reaches and can veto an authoritative paper decision |
| 5. Safety/fallback | `VERIFIED_COMPLETE` | Approval, integrity, staleness, correlation, probability, fallback, and isolation tests pass |
| 6. Evaluation/reporting | `VERIFIED_COMPLETE` | Durable evidence is reported with unavailable comparisons and limitations explicit |
| 7. Testing/final verification | `VERIFIED_COMPLETE` | End-to-end mechanics integration, focused checks, acceptance, and full suite pass |

## Honest unresolved result

Local evidence contains 250 physical session manifests, 5 analysis-eligible sessions, 1 replay-eligible session, and 0 model-training-eligible sessions. There are 4,109 legacy rows without `label_resolved_timestamp_ns`, so they fail closed; there are 0 current-contract real training rows. Both real attempts failed all five walk-forward gates. Consequently:

- no real challenger was published;
- no model registry record was created;
- no artifact is approved or runtime-loaded;
- no genuine cross-session real-market learning claim is available;
- no market-edge or profitability claim is available.

## Exact next action

Do not approve a model or enable live execution. The next legitimate lifecycle step is to collect/finalize enough new real sessions satisfying the current protocol, capability, continuity, and causal-label contract, then rerun the unchanged challenger pipeline and gates. Team 3 remains `FAILED_REQUIRES_REWORK` until a real challenger passes and its complete evidence can be independently verified.

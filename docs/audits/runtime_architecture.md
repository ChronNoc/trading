# Runtime Architecture Audit -- MNQ Trading Assistant

**Date:** 2026-07-29
**Branch:** feature/automatic-runtime
**Scope:** Read-only audit. No files modified. Findings driven by Serena symbol
inspection, targeted Graphify queries (graphify query/path/explain against
graphify-out/graph.json), and direct reads of the cited line ranges.

---

## 1. AutomaticRuntimeController vs DelayedPaperEngine

### Verified facts

- AutomaticRuntimeController (app/runtime/controller.py:96-484) is a
  dataclass that owns session resolution, regime classification, profile
  routing, contract resolution, RuntimeStateMachine/HealthMonitor
  lifecycle, and a synthetic ShadowExecutionStub (app/runtime/controller.py:72-92).
  Its record_setup_decision method (controller.py:290-365) writes a
  decision dict with ML-parity fields (confidence, raw_model_output,
  decision_source) that are always hard-coded to None/"HEURISTIC"
  (controller.py:343-347) -- the comment at lines 337-342 explicitly states
  this path "never actually consults a model" and exists only so its schema
  matches the live engine's EvaluationRecord for GUI/snapshot parity.
- record_setup_decision is called from exactly two places, both non-live:
  tools/start_prototype.py:450 and :460 (_record_setup_if_window_complete,
  synthetic scenario windows), and test files
  (tests/test_automatic_runtime.py, tests/test_prototype_mode.py,
  tests/test_drop_attribution.py). It is not called from
  tools/start_backend.py or tools/start_assistant.py::run_assistant's
  production path (confirmed via find_referencing_symbols on
  AutomaticRuntimeController -- verified by enumerating every reference site).
- DelayedPaperEngine (app/paper/streaming_engine.py:204-887) is the real
  decision path. _apply_ml_policy (streaming_engine.py:623-725) implements
  the veto-only ML policy: a heuristic-accepted setup can be downgraded to
  rejected when the model's success probability is below
  ML_VETO_PROBABILITY_THRESHOLD = 0.35 (streaming_engine.py:73), never the
  reverse (heuristic-rejected setups are never resurrected by ML -- confirmed
  by reading the constant's docstring at lines 68-73, consistent with
  decision_source enum values HEURISTIC/ML/BLENDED/FALLBACK).
  EvaluationRecord (streaming_engine.py:91-138) carries genuine model
  provenance (model_version, model_prediction_id, model_artifact_sha256).
- Both tools/start_backend.py::run_backend (start_backend.py:132-154) and
  tools/start_assistant.py::run_assistant's in-process branch
  (start_assistant.py:538, 546, 560) instantiate both
  AutomaticRuntimeController (for session/regime/lifecycle/health state and
  the GUI's RuntimeSnapshot) and DelayedPaperEngine (for actual setup
  evaluation and simulated order flow). They are wired side by side into a
  shared SnapshotSource (start_backend.py:193-200).
- AutomaticRuntimeController.handle_market_state_snapshot still computes
  decisions_allowed (session/regime/contract routing gate,
  controller.py:285-286). Whether the receiver thread actually gates
  DelayedPaperEngine ingestion on this value was NOT confirmed in this pass
  -- flagged as an unresolved verification requirement in Section 4.

### Assessment

The split is real and structurally load-bearing, but not self-documenting.
AutomaticRuntimeController is simultaneously:
1. The genuine owner of session/regime/contract/lifecycle/health state used by
   the live production path (start_backend.py, start_assistant.py), and
2. A vestigial decision-recording path (record_setup_decision,
   ShadowExecutionStub) that is only exercised by the legacy prototype
   (tools/start_prototype.py) and tests, and is explicitly documented in its
   own code comment as non-authoritative for ML provenance.

This dual role inside one class is the actual risk -- not the existence of two
paths. A future engineer skimming controller.py sees record_setup_decision
building a decision dict with model_version, confidence, etc., and could
reasonably assume this is (or should become) part of the live decision chain.
It is not, and the code says so only in an inline comment
(controller.py:337-342), not in module-level documentation or a name that
signals "prototype/legacy path only."

There is currently no test that asserts AutomaticRuntimeController.record_setup_decision
is never called from run_backend/run_assistant's production branch -- the
separation is enforced only by the absence of a call site today, which is easy
to silently violate in a future edit.

### Recommendation

- Do not merge the two classes -- AutomaticRuntimeController legitimately
  owns different state (session/regime/health/lifecycle) than
  DelayedPaperEngine (event-driven strategy evaluation + simulated
  execution), and unifying them would entangle GUI-facing lifecycle state with
  the hot event-evaluation path.
- Rename or clearly namespace the prototype-only decision-recording surface --
  e.g. move record_setup_decision/ShadowExecutionStub out of
  AutomaticRuntimeController into a small PrototypeDecisionRecorder used
  only by tools/start_prototype.py, or at minimum add a module-level
  docstring banner in controller.py stating "record_setup_decision is a
  prototype/test-only path; the authoritative live decision path is
  DelayedPaperEngine._apply_ml_policy."
- Add a regression test (e.g. in tests/test_repo_hygiene.py or a new
  tests/test_runtime_boundary.py) asserting record_setup_decision has no
  call sites under tools/start_backend.py / tools/start_assistant.py's
  production branch, using an AST or grep-based check, so the boundary is
  enforced mechanically rather than by convention.

---

## 2. Process lifecycle (backend / supervisor) for a future Phase 7 desktop launcher

### Verified facts

- tools/backend_supervisor.py implements a two-tier process model:
  - SingletonLock (app/runtime/process_files.py) is an OS-lease-backed lock
    keyed by runtime/backend.lock + runtime/backend.lock.guard; a second lock
    keyed by SUPERVISOR_LOCK_NAME = "supervisor.lock" /
    SUPERVISOR_GUARD_NAME = "supervisor.lock.guard"
    (backend_supervisor.py:34-35,161-169) protects the supervisor itself.
  - ensure_supervisor (backend_supervisor.py:172-200) attaches to an
    already-alive, fingerprint-compatible supervisor, or spawns a new detached
    one (spawn_supervisor, lines 145-158, using
    CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS on Windows, line 122-125)
    and polls StatusFile.backend_alive(expected_fingerprint=...) up to
    STARTUP_HANDSHAKE_SECONDS = 90.0 (line 33).
  - supervise() (lines 269-322) owns a restart loop with exponential backoff
    (BACKOFF_INITIAL_SECONDS=2.0 to BACKOFF_MAX_SECONDS=60.0, lines 23-24),
    honors StopRequest.pending() to avoid restarting after an intentional
    stop (lines 295-297, 316-318), and calls _recover_backend (lines
    238-266) which requests a graceful StopRequest, waits
    grace_seconds=8.0, and only then sends SIGTERM plus an audit record to
    runtime/forced_shutdowns.jsonl (lines 254-261).
  - request_stop() (lines 325-345) is the clean-shutdown entry point: it
    writes a StopRequest, then polls both the backend and supervisor
    SingletonLock identities until both report not-alive or a 30s timeout,
    printing (not silently swallowing) a timeout failure.
  - Duplicate-backend prevention is fingerprint-based
    (BackendSpec.fingerprint, lines 55-74, folding in
    CAPTURE_RUNTIME_REVISION = "capture-reliability-v2" plus every resolved
    path) -- a GUI built from newer source cannot silently attach to an old,
    semantically-incompatible detached backend; ensure_backend
    (lines 203-235) recovers and respawns when the fingerprint mismatches.
  - Log preservation: spawn_backend/spawn_supervisor open
    runtime/backend.out / runtime/supervisor.out in append binary mode
    ("ab", lines 132, 148) -- prior logs are never truncated across restarts.
    _log() (lines 114-119) appends to runtime/supervisor.log.
  - tools/start_assistant.py::run_assistant (lines 489-536) is the current
    GUI-attach entry point: when config.gui and not config.in_process
    (the default), it calls ensure_supervisor and then launches AppWindow
    reading status via FileSnapshotProvider(config.runtime_dir) -- the GUI
    process holds no backend socket/thread reference (explicit design
    comment, lines 489-495).
  - tools/start_backend.py::run_backend (lines 86-233) is the actual backend
    process entry: acquires SingletonLock, builds AssistantConfig, starts
    the receiver thread, waits up to 30s for the receiver to bind
    (lines 208-220), and on bind failure calls _shutdown_receiver then writes
    a FAILED_BIND status and releases the lock (lines 221-233) before
    exiting -- this is a clean failure path, not a silent hang.
  - The main status loop (lines 235-260+) treats 3 consecutive status-write
    failures as fatal and marks clean = False, restarting rather than
    silently degrading.

### Assessment

This is a genuinely mature two-tier supervisor/backend design -- already
covering duplicate-attach prevention (fingerprinted lease + status heartbeat),
bounded readiness-wait, append-only log preservation, and a graceful-then-forced
shutdown escalation with an audit trail. This is substantially more robust
than a typical prototype launcher, and Phase 7 should build strictly on top
of it, not replace it.

Gaps relevant to a "Windows desktop application" launcher (Phase 7):
- There is currently no single top-level "app launcher" that combines icon /
  .ico / shortcut concerns with ensure_supervisor -- that logic today lives
  inline inside run_assistant (tools/start_assistant.py:489-536), coupled
  to CLI argument parsing. A Phase 7 launcher should be a thin wrapper that
  calls tools.backend_supervisor.ensure_supervisor and
  tools.start_assistant.run_assistant/_run_gui, not a reimplementation.
- request_stop()'s 30s wait window and STARTUP_HANDSHAKE_SECONDS = 90.0
  are hard-coded module constants, not surfaced as launcher-configurable
  values -- a desktop shortcut wrapper that wants a different UX timeout
  (e.g., a splash screen with a longer allowance) would need to import and
  override these directly.
- I did not verify whether tools/start_prototype.py's PrototypeInstanceLock
  (start_prototype.py:170-206, a bare os.O_CREAT|O_EXCL file lock with no
  staleness detection, no PID liveness check, no guard file) could collide
  with or be confused for the production SingletonLock mechanism by a
  future desktop launcher that might accidentally target
  start_prototype.py instead of start_backend.py. This is an unresolved
  verification requirement -- Phase 7 must confirm the desktop launcher
  always drives start_backend.py/backend_supervisor.py, never
  start_prototype.py.

### Recommendation for Phase 7

- Build tools/start_desktop.py (or similar) as a thin orchestrator that:
  1. Resolves BackendSpec from the installed application's config location
     (not CWD-relative defaults) so a desktop shortcut works regardless of
     working directory.
  2. Calls backend_supervisor.ensure_supervisor(...) exactly as
     run_assistant does today, reusing the existing fingerprinting.
  3. On success, launches the GUI exactly via _run_gui/AppWindow, or
     shells out to python -m tools.start_assistant so there is one binary
     entry point.
  4. On shutdown (window close), do not call request_stop() -- preserve
     the existing "GUI closes, backend keeps recording" behavior
     (start_assistant.py:530-535) unless the user explicitly invokes a
     "Stop backend" action, which should shell to
     python -m tools.backend_supervisor --stop.
- Do not introduce a second lock/heartbeat mechanism; add an
  .ico/shortcut-only layer on top of ensure_supervisor/request_stop.
- Add an explicit assertion/test that the desktop shortcut installer
  (Phase 7 deliverable) points only at start_backend.py/backend_supervisor.py,
  never start_prototype.py.

---

## 3. Where an "Autonomous Intelligence" research/ML subsystem should hook in

### Verified facts

- ResearchService (app/research/research_service.py:226-...) already runs
  as a background-thread service (self._thread, line 256) with pause/stop
  events (self._pause_event, self._stop_event, lines 254-255), a
  ClaimRegistry for atomic on-disk job claiming across workers/restarts
  (line 246), and a ProgressCache computed on the background loop only
  (lines 261-265, explicit comment: "the GUI... only reads"). This is wired
  into start_backend.py via _build_research_service(config, controller,
  pipeline_holder) (start_backend.py:162), reusing
  AutomaticRuntimeController only for read access to session/health context
  -- not for decision execution.
- PaperExecutionGateway (app/paper/models.py / gateway module, per Graphify
  community 117) is documented as "the ONLY gateway research/replay/shadow
  may touch... simulation-only... records intents, never talks to any
  broker" -- this is the existing safety boundary pattern new research/ML
  work should follow.
- The model lifecycle already has a formal gate chain visible in Graphify
  community 121 ("test_simulator_metrics.py" hub, listing the exact pipeline):
  dataset construction -> walk_forward_evaluate
  (app/machine_learning/pooled_training.py) -> promotion gates
  (build_validated_challenger, app/machine_learning/challenger_pipeline.py:214-397)
  -> registry.py -> explicit approval (validate_explicit_approval,
  registry.py:249-293) -> staleness (is_artifact_stale, registry.py:295-342)
  -> runtime loader (ObserveOnlyModelLoader,
  app/machine_learning/shadow_predictor.py:170-475) -> PAPER decision policy
  (DelayedPaperEngine._apply_ml_policy, streaming_engine.py:623-725).
- SnapshotSource (app/gui/snapshot_source.py) is the single aggregation
  point feeding AppSnapshot for the GUI, already taking controller,
  paper_engine, research_service, feature_sink, model_loader, and
  outcome_tracker as separate read-only inputs
  (start_backend.py:193-200). This is the existing seam for adding new
  read-only GUI surfaces (Phase 4's "Autonomous Intelligence" page) without
  touching the live decision path.

### Assessment

The codebase already has the right shape for bolting on an "Autonomous
Intelligence" subsystem without touching the live shadow-paper execution
path: ResearchService is a separate background thread/process concern from
DelayedPaperEngine, gated by an explicit, tested, multi-stage
registry/approval chain before anything reaches
ObserveOnlyModelLoader/_apply_ml_policy. The existing PaperExecutionGateway
convention ("the ONLY gateway research/replay/shadow may touch") is the
correct pattern to extend.

### Recommendation

- New autonomous research/strategy-discovery work (Phase 3) should be a new
  service analogous to ResearchService -- its own background thread/state
  directory/claim registry -- that writes candidate artifacts through the
  existing registry/approval pipeline (challenger_pipeline.py ->
  registry.py) rather than any new bypass path. Do not give it any
  reference to DelayedPaperEngine or PaperExecutionGateway beyond
  read-only status.
- ML_VETO_PROBABILITY_THRESHOLD and other live-policy constants must remain
  untouched by anything in the new subsystem; any proposed threshold change
  must go through the existing model governance process. Note:
  docs/model_governance_and_shadow_policy.md appears as an untracked file in
  git status at the time of this audit -- its contents were not read here
  and should be confirmed by whoever implements Phase 3.
- The Phase 4 GUI page should be wired the same way existing screens read
  research/paper state today: through SnapshotSource reading a new
  read-only provider (e.g., an autonomous_intelligence field alongside
  research_service), never a live object reference into
  DelayedPaperEngine.
- Phase 5's "restart-safe bounded report jobs" should reuse the
  ClaimRegistry pattern from ResearchService
  (app/research/research_service.py) rather than inventing a new
  claim/checkpoint mechanism.

---

## 4. Existing risks, dead code, ownership ambiguity

- AutomaticRuntimeController.record_setup_decision / ShadowExecutionStub --
  effectively dead on the production path (only reachable from
  tools/start_prototype.py and tests), but lives inside the class that the
  real backend also depends on for session/regime/health state. See
  Section 1. This is the single highest-value cleanup: it is a structural
  confusion risk, not just unused code.
- Two GUI windows -- app/gui/app_window.py::AppWindow (production, 8-screen,
  Graphify community 5/64) and app/gui/main_window.py::MainWindow (legacy,
  13-tab, Graphify community 11/20; MainWindow is itself a "God Node" with
  165 edges per GRAPH_REPORT.md line 286) are both still present and both
  referenced by tests (test_gui_main_window.py, test_app_window.py) and by
  different launchers (tools/start_prototype.py for MainWindow,
  tools/start_assistant.py/tools/start_backend.py for AppWindow). This is
  documented mission-context (Phase 2 explicitly says preserve both), but is
  worth flagging as ongoing dual-maintenance surface: any GUI-facing
  behavior change (e.g., a new safety indicator) must be replicated in both
  windows or explicitly scoped to one.
- PrototypeInstanceLock (tools/start_prototype.py:170-206) is a materially
  weaker lock than the production SingletonLock (no staleness detection, no
  identity/fingerprint check, no guard file) -- acceptable for a legacy
  prototype tool, but a hazard if any future automation (including a Phase 7
  desktop shortcut) is pointed at start_prototype.py by mistake. Flagged as
  an unresolved verification requirement in Section 2.
- Unverified gating dependency: I did not confirm the exact call site where
  the receiver thread checks controller.decisions_allowed (or an
  equivalent) before calling into DelayedPaperEngine.ingest/on_market_event
  in tools/start_assistant.py::_run_receiver_thread. If no such gate exists,
  AutomaticRuntimeController's decisions_allowed state (session/regime/
  contract-derived) may be informational-only for the GUI, while
  DelayedPaperEngine independently gates via its own warmup/state machine
  (STATE_WAITING/STATE_WARMING/STATE_EVALUATING, streaming_engine.py:55-58).
  This must be verified by reading _run_receiver_thread in full
  (tools/start_assistant.py, approx line 701+) before any future refactor
  assumes a coupling that may not exist.
- God nodes / high fan-in: MarketState (305 edges), MainWindow (165),
  MarketSessionRecorder (120), DelayedPaperEngine (99) per GRAPH_REPORT.md
  lines 285-294. DelayedPaperEngine and MarketState being god nodes is
  expected given their central role; MainWindow's 165 edges given its
  "legacy" status is worth Phase 2's attention re: how much behavior still
  genuinely depends on it versus how much is test-only scaffolding -- not
  independently verified in this audit.

---

## 5. Concrete recommendations by phase

- Phase 1 (this audit): Done. Follow-up action item: verify the
  decisions_allowed/receiver-thread gating question in Section 4 before
  Phase 3/6 rely on any assumption about it.
- Phase 2 (UI redesign): Preserve both AppWindow and MainWindow per mission
  directive; audit MainWindow's actual live call sites (not just test
  references) before assuming it needs full production-state wiring parity
  with AppWindow.
- Phase 3 (autonomous ML/strategy/risk research): New service modeled on
  ResearchService; all artifacts flow through the existing
  challenger/registry/approval chain; zero new references from the new
  subsystem into DelayedPaperEngine or PaperExecutionGateway beyond
  read-only status snapshots.
- Phase 4 (Autonomous Intelligence GUI page): Wire through SnapshotSource as
  a new read-only provider field, following the research_service/paper_engine
  pattern already in start_backend.py:193-200.
- Phase 5 (Reports tab): Reuse ClaimRegistry for restart-safe bounded jobs
  rather than a new mechanism.
- Phase 6 (safety review): Specifically re-verify (a) the decisions_allowed
  gating question above, and (b) that no new Phase 3/4/5 code path can reach
  _apply_ml_policy's veto threshold or PaperExecutionGateway other than
  through the existing tested call sites.
- Phase 7 (desktop launcher): Build strictly on
  backend_supervisor.ensure_supervisor/request_stop; do not introduce a
  parallel lock/heartbeat mechanism; confirm the launcher never targets
  tools/start_prototype.py.
- Phase 8 (verification): Add the boundary regression test proposed in
  Section 1 (no production call sites for
  AutomaticRuntimeController.record_setup_decision) to the final acceptance
  checklist.

---

## Verification ledger (per operating constraints)

**Files inspected (Read/get_symbols_overview):**
app/runtime/controller.py, app/paper/streaming_engine.py,
tools/backend_supervisor.py, tools/start_backend.py (lines 1-260),
tools/start_assistant.py (lines 477-546, 728-798),
tools/start_prototype.py (lines 160-220), app/runtime/shutdown.py,
app/research/research_service.py (lines 220-300),
app/gui/snapshot_source.py (overview only), docs/GOAL_C_AUTONOMOUS_MISSION.md,
graphify-out/GRAPH_REPORT.md (full).

**Serena symbols inspected:** AutomaticRuntimeController (class + all
methods, depth=1), DelayedPaperEngine (class + all methods, depth=1),
find_referencing_symbols on both AutomaticRuntimeController and
DelayedPaperEngine (full reference enumeration across app/, tests/, and
tools/), find_referencing_symbols on
AutomaticRuntimeController/record_setup_decision.

**References found:** AutomaticRuntimeController referenced from
tests/test_assistant_wiring.py, tests/test_automatic_runtime.py,
tests/test_drop_attribution.py, tests/test_prototype_mode.py,
tests/test_research_service.py, tests/test_shutdown.py,
tools/pipeline_loadtest.py, tools/soak.py, tools/start_assistant.py,
tools/start_backend.py, tools/start_prototype.py. DelayedPaperEngine
referenced from roughly 15 test files plus tools/pipeline_loadtest.py,
tools/soak.py, tools/start_assistant.py, tools/start_backend.py,
tools/verify_final_acceptance.py. record_setup_decision referenced only
from tests/test_automatic_runtime.py and tools/start_prototype.py.

**Graphify evidence consulted:** graphify query (narrow, approx 7 queries:
"AutomaticRuntimeController lifecycle", "DelayedPaperEngine order flow",
"backend supervisor process lifecycle duplicate prevention",
"AppWindow snapshot source runtime status", "who instantiates
AutomaticRuntimeController", "who instantiates DelayedPaperEngine",
"record_setup_decision setups.py evaluate_setup strategy prototype",
"SnapshotSource controller paper_engine build AppSnapshot");
graphify explain (backend_supervisor.py, process_files.py);
graphify path (AppWindow to backend_supervisor.py); full read of
graphify-out/GRAPH_REPORT.md (community structure, god nodes, knowledge
gaps).

**Commands executed:** git status, git log --oneline -10, ls graphify-out,
graphify --help, the graphify query/explain/path commands listed above,
mkdir -p docs/audits. No production processes launched, no files modified
other than this report.

**Test results:** None run (not requested for this audit).

**Uncertainties / blockers requiring further verification:**
1. RESOLVED (lead agent, post-audit, 2026-07-29): confirmed via
   docs/AUTOMATIC_PAPER_PIPELINE.md ("The regression this documents") that
   `decisions_allowed` intentionally gates broker/shadow decisions only
   (delayed data must never route an order) and does NOT gate paper
   evaluation -- DelayedPaperEngine evaluates every validated event causally
   regardless of decisions_allowed ("paper evaluation: ALWAYS ON"). This was
   a deliberate fix (commit f81486e) for a prior regression where the two
   questions ("may this reach a broker" vs "may this be evaluated on paper")
   were conflated into one flag. So AutomaticRuntimeController.decisions_allowed
   and DelayedPaperEngine's own warmup/state machine are correctly
   independent by design, not an unverified coupling risk.
2. Whether MainWindow's 165 graph edges reflect genuine live production
   usage or largely test/legacy scaffolding -- not independently verified.
3. Contents of docs/model_governance_and_shadow_policy.md (untracked file
   per git status) were not read in this audit; Section 3's reference to it
   should be confirmed by whoever implements Phase 3.

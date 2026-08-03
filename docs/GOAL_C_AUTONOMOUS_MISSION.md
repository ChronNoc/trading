# Goal C — Autonomous Execution Mission (verbatim record)

**Status:** In progress
**Branch:** `feature/automatic-runtime`
**Purpose of this document:** record the full mission directive verbatim so that any future
session (including a fresh one with no conversation history) can resume this work correctly.
This document must be kept in sync with `docs/AUTONOMOUS_HANDOFF.md` (current phase status) and
`docs/autonomous_learning_worklog.md` (detailed execution log).

## Mission directive (verbatim)

> Continue the existing MNQ project from its current repository state.
> You are the lead implementation agent. Use Serena and Graphify directly, make the final
> engineering decisions, edit the repository, run tests, and integrate all verified work.
> Do not ask me questions. Do not stop after planning. Do not invoke the claude-api skill.
> Do not use mnq-executor or mnq-coding as a subagent model. Preserve all valid unfinished
> changes. Never use destructive Git commands. Keep the application strictly shadow-only.

### CONTEXT CONTROL (governs the entire mission)

1. Never run more than one subagent at a time.
2. Subagents are reviewers/researchers only; the lead agent remains responsible for all
   implementation.
3. Each subagent must save its full report to a repository file.
4. Each subagent must return at most 10 summary lines to the conversation.
5. Never print complete Git diffs, Graphify JSON, reports, or test logs in chat.
6. Store detailed evidence in repository documents.
7. Use targeted Graphify queries; never load the entire `graphify-out/graph.json`.
8. Keep `docs/AUTONOMOUS_HANDOFF.md` updated after every major phase.
9. Continue autonomously after every subagent finishes (no idle waiting).

### FIRST ACTIONS

1. Run `git status`, inspect existing unfinished changes.
2. Use Serena to inspect current GUI, autonomous research, reporting, launcher, and ML symbols.
3. Inspect `graphify-out/GRAPH_REPORT.md` and use targeted Graphify queries.
4. Create/update `docs/GOAL_C_AUTONOMOUS_MISSION.md`, `docs/autonomous_learning_worklog.md`,
   `docs/AUTONOMOUS_HANDOFF.md`.
5. Record the complete mission text inside `docs/GOAL_C_AUTONOMOUS_MISSION.md` for resumability.
6. Continue directly into implementation.

### PHASE 1 — Runtime architecture audit

Dispatch `runtime-architect` subagent. Output: `docs/audits/runtime_architecture.md`.

### PHASE 2 — UI premium glass-morphism redesign

Dispatch `ui-product-designer` subagent. Outputs: `docs/audits/ui_product_design.md`,
`docs/ui_page_parity_matrix.md`, updates to `docs/ui_redesign_system.md`. Preserve all 8
existing `AppWindow` screens and both Qt windows (`AppWindow`, legacy `MainWindow`).

### PHASE 3 — Autonomous ML/strategy/risk research system

Dispatch `ml-strategy-researcher` subagent. Outputs: `docs/audits/ml_strategy_research.md`,
`docs/autonomous_intelligence_architecture.md`.

Explicit prohibitions (must never be violated by any implementation in this mission):
- No look-ahead leakage.
- No random time-series splits.
- No in-sample promotion.
- No bypassing feature contracts, registry, or approval gates.
- No autonomous alteration of safety limits.
- No live execution.

### PHASE 4 — New "Autonomous Intelligence" GUI page

New top-level page backed by real persisted state (not mock data), including candidate-strategy
detail views.

### PHASE 5 — Reports tab inside Autonomous Intelligence

Index real report categories with search/filter/sort/compare; restart-safe bounded report jobs.
Document in `docs/autonomous_reporting_system.md`.

### PHASE 6 — Safety review

Dispatch `safety-risk-reviewer` subagent. Output: `docs/audits/safety_risk_review.md`. Fix valid
issues; reject invalid findings only with recorded repository evidence.

### PHASE 7 — Full Windows desktop application experience

Icon assets, `.ico`, launcher with duplicate-backend prevention/readiness-wait/log
preservation/clean shutdown, `tools/install_desktop_shortcut.ps1` with removal support,
`docs/desktop_application_and_shortcut.md`. Never start Claude Code/OmniRoute. Never enable live
trading or auto-enable broker execution.

### PHASE 8 — Verification

Targeted tests after each phase, then a full final acceptance pass with a 15-item checklist,
updating `docs/review_packet.md`.

### Closing constraints

- Do not claim profitability.
- Do not declare completion without implementation, runtime wiring, tests, artifacts, reports,
  and direct evidence.

## Execution log pointer

See `docs/autonomous_learning_worklog.md` for the phase-by-phase execution log and
`docs/AUTONOMOUS_HANDOFF.md` for current status / what a fresh session should do next.

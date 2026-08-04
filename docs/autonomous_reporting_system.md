# Autonomous Reporting System

This document describes the **Reports** page and how the GUI surfaces the
repository's generated reports. It is deliberately honest about what exists
today versus the fuller reporting vision in `docs/GOAL_C_AUTONOMOUS_MISSION.md`.

## What exists today

### Report generation (backend, already running)

Reports are produced by the existing runtime pipeline and written under
`data/reports/`:

| Category | Producer | Location |
|---|---|---|
| Session report | session finalization | `data/reports/<date>/session_*/` |
| Daily paper-trading report | `app/paper/daily_report.py` | `data/reports/paper/<date>/` |
| Daily learning report | `app/machine_learning/daily_learning.py` | `data/reports/daily_learning/<date>/` |

Generation runs off the capture hot path (in the research/report services), so
it never blocks Bookmap capture.

### Reports page (GUI, this change)

`app/gui/autonomous_view.py` reads `data/reports/` **live and off the Qt thread**
into an immutable `AutonomousSnapshot`, and `ReportsScreen`
(`app/gui/screens.py`) renders it. For every report it surfaces:

- **category** (session / daily_learning / paper / …),
- **covered range** (the date embedded in the path),
- **provenance** — `REAL/UNVERIFIED`, `DELAYED`, `REPLAY`, or `SYNTHETIC`,
  derived from the path and file head so decorative colour can never imply
  proven results,
- **modified time** and **file path** (for opening the artifact).

The page shows totals (count, categories, delayed vs synthetic) and the newest
reports first. It reads real files — there is no mock data. When no reports
exist it shows an honest empty state.

**Honesty invariant:** the Reports page never states that profitability has been
proven. Provenance is always labelled, and synthetic/delayed reports are called
out explicitly.

### Off-thread, deterministic

The read runs on a `QThreadPool` worker (`_AutonomousReader` in `screens.py`) and
delivers an immutable snapshot to the GUI thread via a queued signal — no file
I/O on the Qt thread, and it never blocks capture. Reads are driven by a timer
(interval > 0) so offscreen visual-regression captures stay deterministic.

## What is NOT built yet (honest gaps vs GOAL C)

The full GOAL C reporting vision is larger than the current Reports page. Not
yet implemented:

- A dedicated, queued, restart-safe **report-generation service** that emits new
  report categories (strategy-discovery, drift, calibration, walk-forward,
  champion-vs-challenger, rollback, etc.) automatically on lifecycle events.
- In-page **filtering/search/compare/export** controls (the page currently
  lists and labels; filtering is a follow-up).
- Integrity **hashes** per report entry.

The backend now **proposes** shadow-only autonomous candidates and runs the
**first governed gate** (`app/research/autonomous_proposer.py`, wired into
`start_backend` behind `autonomous_enabled`), so the Autonomous Intelligence page
shows real candidates advancing `PROPOSED → DATA_VALIDATED`, with `gate_passed`
activity. The `DATA_VALIDATED` gate uses the real gate primitives
(`evaluate_requirements` + `apply_gate`) and a fenced store lease, checking the
`eligible_sessions` metric against a preregistered minimum.

Because the service is intentionally *fail-closed* (a candidate whose next gate
has no runner is failed), gate advancement is done with **scoped** helpers that
only ever process the current gate's state — so they can never fail a candidate
at an unwired later gate.

The **OFFLINE_TRAINED** gate (`advance_offline_training`) is now wired: it trains
a challenger at the candidate's target/stop geometry via the existing challenger
pipeline (`build_validated_challenger`) and requires the model to have produced
out-of-sample predictions (`oos_predictions >= 1`). It does NOT require beating
the baseline — that belongs to a later gate. Two conditions leave the candidate
at `DATA_VALIDATED` to retry rather than failing it (never a false pass, never a
false failure):

- a **training error** (transient dataset/build problem), and
- **too few out-of-sample predictions** — this means there is not yet enough
  model-eligible recorded data to run even one walk-forward fold. That is a data
  shortage, not a bad candidate, so the candidate is *deferred* (logged as
  `gate_deferred`) and will pass automatically once enough eligible sessions
  accumulate. It is never moved to `FAILED_REQUIRES_REWORK` for lack of data.

Because training rebuilds datasets from raw and is heavy, it is **opt-in**
(`autonomous_training_enabled`, default off) and bounded to one candidate per
cycle so it never competes with live capture. The trainer is injected, so the
gate logic is fully unit-tested without running ML.

> **Observed on the current dataset:** with today's small pool of
> model-eligible sessions, a real challenger train finishes in seconds and
> yields **0** out-of-sample predictions, so the candidate is honestly
> *deferred* at `DATA_VALIDATED`. This is the fail-*open*-on-missing-data,
> fail-*closed*-on-bad-result design working as intended: the loop will not
> fabricate a trained model it cannot actually evaluate.

The **WALK_FORWARD_VALIDATED** gate (`advance_walk_forward_validation`) is now
wired. The walk-forward evaluation (out-of-sample expectancy after costs vs. the
take-everything baseline) already ran during OFFLINE_TRAINED, so this gate reads
the *carried-forward* metric (`beats_baseline_after_costs`, stored on the
OFFLINE_TRAINED evidence) and **never re-trains**. Its outcomes are deliberately
asymmetric with OFFLINE_TRAINED:

- **beats baseline → advances** to `WALK_FORWARD_VALIDATED` (`gate_passed`).
- **does not beat baseline → `REJECTED`** (terminal, `gate_rejected`). Unlike a
  data shortage, a fully evaluated model that loses to the baseline is a real,
  final answer — the governed lifecycle is *allowed* to conclude a candidate does
  not work. That is the point of the gate.
- **metric absent** (e.g. trained before it was recorded) → *deferred*, never
  rejected.

Because it only reads stored evidence, walk-forward validation is cheap and runs
every cycle (it advances any `OFFLINE_TRAINED` candidate left by this or an
earlier cycle), independent of the heavy, opt-in training step.

The **STABILITY_VALIDATED** gate (`advance_stability_validation`) is now wired. A
model can beat the baseline *in aggregate* while the edge is really carried by one
lucky day. The walk-forward evaluator therefore now scores **each** out-of-sample
day individually and records `stable_fold_fraction` — the fraction of independent
OOS days on which the model beat the baseline by itself. This gate reads that
carried-forward metric (no re-training) and requires it to be at least
`STABILITY_MIN_FOLD_FRACTION` (0.6). Outcomes mirror WALK_FORWARD_VALIDATED:
consistent edge → advances; evaluated but inconsistent → terminal `REJECTED`;
metric absent → deferred.

The remaining gates (cost, shadow stages) are still pending. Until they are
wired, candidates rest honestly at `STABILITY_VALIDATED`, `WALK_FORWARD_VALIDATED`,
`OFFLINE_TRAINED`, or `DATA_VALIDATED` (per how far the data lets them progress),
or terminally at `REJECTED`. The Reports page reflects the reports the existing
pipeline produces.

## Tests

`tests/test_autonomous_gui.py` covers: the view layer categorises, dates, and
provenance-tags real reports; an absent autonomous store is reported honestly
(not a placeholder); the Reports page reads real reports and never claims
profitability; and all eight original screens are preserved alongside the two
new pages. Visual baselines are re-blessed to include the new pages.

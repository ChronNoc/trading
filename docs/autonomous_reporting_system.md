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

These depend on the autonomous intelligence **service being wired into the
runtime** (it currently exists as a tested module but is not instantiated by
`start_backend`), which is tracked separately. Until then the Reports page
honestly reflects the reports that the existing pipeline produces.

## Tests

`tests/test_autonomous_gui.py` covers: the view layer categorises, dates, and
provenance-tags real reports; an absent autonomous store is reported honestly
(not a placeholder); the Reports page reads real reports and never claims
profitability; and all eight original screens are preserved alongside the two
new pages. Visual baselines are re-blessed to include the new pages.

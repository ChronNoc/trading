# UI Redesign System

_Last updated: 2026-07-28. This document records verified repository state as of the current working tree. It is a reference for the GUI's design system, screen inventory, and data model — not a claim about market edge or profitability._

## Purpose

This is the single reference for the GUI's current visual system, component library, screen inventory, and the exact data model each screen renders — the prerequisite map before any premium-dashboard visual upgrade. It records what exists today, what is live vs. dead code, and what to build on rather than replace.

## Production entry point vs. preserved legacy

There are two Qt main windows in this repository, and they are **not** interchangeable:

- **`app/gui/app_window.py::AppWindow`** — the production entry point. `tools/start_assistant.py:745-756` imports and constructs it for the headless/production assistant startup path; `tools/capture_gui_screenshots.py` also targets it for GUI snapshot review. `AppWindow` uses the 8-screen `build_screens()` navigation model (`app/gui/screens.py`), the `Card`/`StatTile`/`StatusBadge`/`MetricMeter`/`EvidenceTable` component library (`app/gui/widgets.py`), and `HistoryChart`/`AggressorBar`/`ChartPanel` (`app/gui/charts.py`) — all styled via `design_tokens.py`.
- **`app/gui/main_window.py::MainWindow`** — a preserved legacy window (2914 lines, 13-tab layout). `tools/start_prototype.py:388-394` is its only production entry point, used for the separate prototype-mode tool. It is still tested (`tests/test_gui_main_window.py`) and must not be deleted or broken, but it is not the assistant's primary GUI and is out of scope for the premium visual upgrade described below.

Both windows import from `app/gui/theme.py`, so that module's active constants (`DASHBOARD_DARK`/`DASHBOARD_LIGHT`, and separately `APP_STYLESHEET`) cannot be deleted without checking both call sites.

## Two theme systems (confirmed, not aspirational)

### 1. Global window-level QSS — `app/gui/theme.py`

`theme.py`'s own docstring states this directly: *"This module is being migrated to design_tokens.py for the new premium glass morphism aesthetic. DASHBOARD_DARK and DASHBOARD_LIGHT themes are the current active stylesheets; APP_STYLESHEET is legacy."*

Confirmed by source:

| Constant | Status | Consumer |
|---|---|---|
| `DASHBOARD_DARK` / `DASHBOARD_LIGHT` (via `DASHBOARD_THEMES = {"dark": ..., "light": ...}`, `theme.py:169`) | **Active** | `AppWindow.apply_theme()` (`app_window.py:82,277`): `THEMES = DASHBOARD_THEMES`, `self.setStyleSheet(THEMES[self._theme])` |
| `APP_STYLESHEET` (`theme.py:12-101`) | **Active, but legacy-window-only** | `MainWindow.__init__` (`main_window.py:56,266`): `self.setStyleSheet(APP_STYLESHEET)` |
| `BANNER_STYLE`, `WATCHDOG_OK_STYLE`, `WATCHDOG_INFO_STYLE`, `WATCHDOG_WARNING_STYLE`, `AUTOMATION_BANNER_STYLE`, `COACH_STYLE` | Legacy-window helper styles (slate/blue terminal palette, `theme.py:103-111`) | `main_window.py` only, not investigated further this pass — low priority since `MainWindow` is not the upgrade target |

`DASHBOARD_DARK`/`DASHBOARD_LIGHT` use a slate-navy palette (`#0b1017` base, `#121923` cards, `#4aa8ff` accent) with `QFrame[role=...]`/`QLabel[state=...]` Qt dynamic-property selectors — this is the base/system-widget-default layer, not the premium per-widget token layer.

**Dead code cleanup complete (Task #2):** the `_DARK`/`_LIGHT` QSS string constants formerly duplicated in `app_window.py` have been deleted. `THEMES = DASHBOARD_THEMES` (`app_window.py:43`) is the sole indexed theme source `apply_theme()` uses — confirmed via full-file read, no other references remain.

### 2. Per-widget premium token system — `app/gui/design_tokens.py`

This is the actively and comprehensively adopted premium styling layer, confirmed used by every reusable widget/chart class:

- `app/gui/widgets.py`: `Card`, `StatTile`, `StatusBadge`, `MetricMeter`, `CapabilityEmptyState`, `EvidenceTable` all import `design_tokens as dt` for theme-neutral layout/spacing tokens. `Card` specifically no longer builds an inline `setStyleSheet(...)` background/border from `dt` — its color, gradient, and border now come solely from `theme.py`'s `QFrame[role="card"]` QSS selector (via `setProperty("role", "card")`), while `dt.CardStyle()` supplies only `padding`/`shadow_blur_radius`/`shadow_offset_y`/`shadow_alpha` for layout and the `QGraphicsDropShadowEffect`. See `docs/glass_morphism_upgrade.md` for the full before/after of this split.
- `app/gui/charts.py`: `HistoryChart.paintEvent`, `AggressorBar.paintEvent` both import `design_tokens as dt` for every paint color (`dt.COLOR_TEXT_TERTIARY`, `dt.COLOR_PRIMARY`, `dt.COLOR_GLASS_BORDER`, `dt.COLOR_SUCCESS`/`dt.COLOR_ERROR` for bid/ask).
- `app/gui/screens.py`: `_heading()` helper uses `design_tokens` for section headline styling.

`design_tokens.py` is a from-scratch dark/glass-morphism token catalogue (deep navy/black base, electric blue/cyan/violet/emerald accents), fully cataloged below.

**Coexistence is intentional, not accidental drift**: `AppWindow.apply_theme()`'s own comment states *"NOTE: Premium token-based styling is applied per-widget in widgets.py. This global stylesheet provides base colors and system widget defaults."* — `DASHBOARD_THEMES` is the base/fallback layer (applies to any raw Qt widget without a token-styled wrapper); `design_tokens.py` is the deliberate per-component override layer. Any visual upgrade should extend this same pattern — do not collapse the two into one QSS blob.

## Full design token catalogue (`app/gui/design_tokens.py`)

### Color palette

| Group | Tokens |
|---|---|
| Base layers (navy→black) | `COLOR_BASE_DARKEST #0a0e1a`, `COLOR_BASE_DARKER #0f1419`, `COLOR_BASE_DARK #151922`, `COLOR_BASE_MID #1e2633`, `COLOR_BASE_LIGHT #2a3544` |
| Glass overlays | `COLOR_GLASS_OVERLAY rgba(255,255,255,0.03)`, `COLOR_GLASS_BORDER rgba(255,255,255,0.08)`, `COLOR_GLASS_HOVER rgba(255,255,255,0.06)` |
| Primary (electric blue) | `COLOR_PRIMARY #0ea5e9`, `_LIGHT #38bdf8`, `_DARK #0284c7`, `_GLOW rgba(14,165,233,0.25)` |
| Accent (cyan) | `COLOR_ACCENT #22d3ee`, `_LIGHT #67e8f9`, `_DARK #06b6d4`, `_GLOW rgba(34,211,238,0.25)` |
| Highlight (violet) | `COLOR_HIGHLIGHT #8b5cf6`, `_LIGHT #a78bfa`, `_DARK #7c3aed`, `_GLOW rgba(139,92,246,0.25)` |
| Success (emerald) | `COLOR_SUCCESS #10b981`, `_LIGHT #34d399`, `_DARK #059669`, `_GLOW rgba(16,185,129,0.25)` |
| Warning (amber) | `COLOR_WARNING #f59e0b`, `_LIGHT #fbbf24`, `_DARK #d97706` (no glow variant) |
| Error (red) | `COLOR_ERROR #ef4444`, `_LIGHT #f87171`, `_DARK #dc2626`, `_GLOW rgba(239,68,68,0.25)` |
| Text | `COLOR_TEXT_PRIMARY #f8fafc`, `_SECONDARY #cbd5e1`, `_TERTIARY #94a3b8`, `_DISABLED #64748b` |
| Chart/data (accessible, high-contrast) | `COLOR_CHART_BLUE #3b82f6`, `_CYAN #06b6d4`, `_VIOLET #8b5cf6`, `_PINK #ec4899`, `_EMERALD #10b981`, `_AMBER #f59e0b`, `_ORANGE #f97316` |

### Spacing (8px grid)

`SPACE_XXS 4`, `SPACE_XS 8`, `SPACE_SM 12`, `SPACE_MD 16`, `SPACE_LG 24`, `SPACE_XL 32`, `SPACE_XXL 48`, `SPACE_HUGE 64`.

### Typography

- Families: `FONT_FAMILY_SANS` (system UI stack: `-apple-system`, `Segoe UI`, `Roboto`, …), `FONT_FAMILY_MONO` (`SF Mono`, `Cascadia Code`, `Fira Code`, `Consolas`, …).
- Sizes: `FONT_SIZE_XS 11`, `_SM 13`, `_MD 15`, `_LG 18`, `_XL 24`, `_XXL 32`, `_HUGE 48`.
- Weights: `FONT_WEIGHT_NORMAL 400`, `_MEDIUM 500`, `_SEMIBOLD 600`, `_BOLD 700`.
- Line heights: `LINE_HEIGHT_TIGHT 1.2`, `_NORMAL 1.5`, `_RELAXED 1.75`.

### Radius, shadow, border, transition, z-index

- Radius: `RADIUS_SM 4` (badges), `_MD 6` (buttons/inputs), `_LG 8` (cards), `_XL 12` (large containers), `_FULL 9999` (pills).
- Shadow (elevation): `SHADOW_SM`/`_MD`/`_LG`/`_XL` (increasing blur/offset `rgba(0,0,0,...)`), plus glow variants `SHADOW_GLOW_PRIMARY`/`_ACCENT`/`_SUCCESS`/`_ERROR` (built from the corresponding `_GLOW` color).
- Border widths: `BORDER_WIDTH_THIN 1`, `_MEDIUM 2`, `_THICK 3`.
- Transitions: `TRANSITION_FAST 150ms`, `_NORMAL 250ms`, `_SLOW 400ms`, `TRANSITION_EASE cubic-bezier(0.4,0,0.2,1)`.
- Z-index layers: `Z_INDEX_BASE 0` → `_DROPDOWN 100` → `_STICKY 200` → `_OVERLAY 300` → `_MODAL 400` → `_POPOVER 500` → `_TOOLTIP 600`.

### Component-specific style dataclasses

- `CardStyle` (frozen, **theme-neutral only**): `padding=SPACE_MD`, `shadow_blur_radius=16`, `shadow_offset_y=4`, `shadow_alpha=80`. It carries no color/background/border fields — those are owned exclusively by `theme.py`'s `QFrame[role="card"]` QSS selector for each palette. See `docs/glass_morphism_upgrade.md`.
- `ButtonStyle` (frozen): `background`, `background_hover`, `text`, optional `border`/`glow`, `padding_x=SPACE_MD`, `padding_y=SPACE_SM`, `font_weight=FONT_WEIGHT_MEDIUM`, `border_radius=RADIUS_MD`. Instances: `BUTTON_PRIMARY` (electric blue + glow), `BUTTON_SECONDARY` (neutral + glass border), `BUTTON_DANGER` (red + glow), `BUTTON_GHOST` (transparent + glass hover).
- `BadgeStyle` (frozen): `background`, `text`, optional `border`/`glow`, `padding_x=SPACE_XS`, `padding_y=SPACE_XXS`, `font_size=FONT_SIZE_XS`, `font_weight=FONT_WEIGHT_SEMIBOLD`, `border_radius=RADIUS_SM`. Instances: `BADGE_SUCCESS`/`_WARNING`/`_ERROR`/`_INFO`/`_NEUTRAL`/`_LOCKED` (a distinct amber `BadgeStyle`, reconciled in Task #1 below).

**Reconciled (Task #1):** `StatusBadge.set_status()` no longer builds its own inline color dict. It now sets only `setProperty("state", ...)` and the theme QSS `QLabel[state="ok"|"warn"|"fail"|"locked"|"neutral"]` selectors (`theme.py`) own the color/border/background for each state — a single source of truth. The `"locked"` state uses the amber safety-gate palette (`#49391b`/`#ffd98c` dark, matching `design_tokens.py`'s `BADGE_LOCKED`/`COLOR_LOCKED_*` constants) rather than the generic blue `BADGE_INFO`/`COLOR_PRIMARY_DARK` pair it was previously conflated with, so the visual "this is a hard safety lock" signal is now distinct from an ordinary informational badge.

## Component library (`app/gui/widgets.py`, `app/gui/charts.py`)

| Component | File | Responsibility |
|---|---|---|
| `Card` | `widgets.py` | Titled glass-morphism container; token-styled background/border/radius. |
| `StatTile` | `widgets.py` | Single metric display; `set_value(value, detail="")`. |
| `StatusBadge` | `widgets.py` | Text+color status chip; `set_status(text, state)`, `state ∈ {ok,warn,fail,locked,neutral}`; always text-labeled, never color-only. |
| `MetricMeter` | `widgets.py` | Fractional progress meter; `set_fraction(fraction, text)`. |
| `CapabilityEmptyState` | `widgets.py` | Explicit "not available, here's why" placeholder; `set_reason(title, reason)` — used instead of faking data when a capability/evidence source is unavailable. |
| `EvidenceTable` | `widgets.py` | Row-oriented evidence table; `set_rows(rows)`. |
| `stat_grid(tiles, columns=3)` | `widgets.py` | Layout helper arranging `StatTile`s in a responsive grid. |
| `HistoryChart` | `charts.py` | Time-series line chart; `table_text()` returns a non-visual numeric summary (samples/first/low/high/latest) — accessibility/copy-paste equivalent. |
| `AggressorBar` | `charts.py` | Bid/ask aggressor volume bar; success/error token colors for buy/sell. |
| `ChartPanel` | `charts.py` | Wraps a chart with a visible `QLabel` text summary (`chart.table_text()`) kept in sync via `set_history()` — every chart has a parallel textual equivalent rendered alongside it, not hidden in an alt-text attribute. |

**Design principle confirmed repeatedly across every widget**: status/state is never communicated by color alone. Every `StatusBadge` pairs a text label with its color; every chart (`HistoryChart` via `ChartPanel`) renders a parallel text summary next to the visual. Any new component added during the premium upgrade must preserve this pattern.

## Screen inventory (`app/gui/screens.py`, via `build_screens()`)

`build_screens()` (`screens.py:484-491`) constructs exactly 8 screens, keyed by navigation destination constant:

| Navigation key | Screen class | Base class |
|---|---|---|
| `OVERVIEW` | `OverviewScreen` (`screens.py:135`) | `Screen` (direct) |
| `LIVE_ORDER_FLOW` | `LiveOrderFlowScreen` (`screens.py:302`) | `DashboardScreen` |
| `PAPER_TRADING` | `PaperTradingScreen` (`screens.py:331`) | `DashboardScreen` |
| `SESSIONS_REPLAY` | `SessionsReplayScreen` (`screens.py:365`) | `DashboardScreen` |
| `RESEARCH_HEALTH` | `ResearchHealthScreen` (`screens.py:380`) | `DashboardScreen` |
| `RISK_LUCID` | `RiskLucidScreen` (`screens.py:415`) | `DashboardScreen` |
| `EXECUTION` | `ExecutionScreen` (`screens.py:430`) | `DashboardScreen` |
| `DIAGNOSTICS` | `DiagnosticsScreen` (`screens.py:466`) | `DashboardScreen` |

`DashboardScreen` (`screens.py:105`) is a shared abstract base class providing common layout/scaffolding for 7 of the 8 screens — it is never itself instantiated as a navigation destination and does not appear in `build_screens()`'s dict. `OverviewScreen` is the one screen that subclasses `Screen` directly instead.

Each screen consumes a typed slice of `AppSnapshot` (see below) and is composed from the `Card`/`StatTile`/`StatusBadge`/`MetricMeter`/`CapabilityEmptyState`/`EvidenceTable`/`ChartPanel` component library — no screen hand-rolls its own raw Qt styling outside these tokens.

## Data model (`app/gui/view_models.py`, 450 lines)

`AppSnapshot` is the single top-level object every screen renders from, assembled once per refresh cycle by the (non-Qt-thread) `SnapshotWorker` and delivered via `AppWindow.submit_snapshot()`. Full sub-object catalogue:

| Type | Kind | Notes |
|---|---|---|
| `Health` | enum | Status severity levels used by `ComponentHealth` and computed health properties throughout. |
| `Capability` | enum | Named system capabilities gating `CapabilityEmptyState` rendering. |
| `ComponentHealth` | dataclass | Per-component health + detail text. |
| `MarketHistoryPoint` | dataclass | One point in a market history series (feeds `HistoryChart`). |
| `PipelineStageRow` | dataclass | One row describing a research/discovery pipeline stage's status. |
| `PipelineSnapshot` | dataclass | Aggregate of `PipelineStageRow`s. |
| `MarketSnapshot` | dataclass (+ `provenance_text` property) | Current market state plus a human-readable data-provenance string (source/delay/handshake). |
| `CaptureSnapshot` | dataclass (+ `queue_pressure`, `health` properties) | Live capture/session-recording health, including bounded-queue pressure. |
| `SetupCheck` | dataclass | One strategy-setup evaluation result. |
| `ProgressGateRow` | dataclass | One row in a promotion/progress gate table (e.g. walk-forward gates). |
| `ProfitabilitySnapshot` | dataclass (+ `summary` property) | Aggregate P&L/expectancy evidence — paper-only. |
| `TradeRow` | dataclass (+ `label` property) | One paper trade row for display. |
| `PaperSnapshot` | dataclass (+ `flat`, `empty_reason` properties) | Aggregate paper-trading state; `empty_reason` surfaces *why* there's nothing to show instead of a blank table. |
| `ResearchSnapshot` | dataclass | Research/discovery pipeline evidence aggregate. |
| `ExecutionSnapshot` | dataclass (+ `live_health` property, **always `Health.LOCKED`**) | Execution-path status; `live_health` is a hard-coded `LOCKED` constant, not a computed value — this is the GUI-level expression of the no-live-trading safety boundary. |
| `ModelSnapshot` | dataclass (+ `outcome_coverage_fraction`, `abstention_rate` properties) | Registry/challenger/loader state, prediction/outcome coverage evidence. |
| `AppSnapshot` | dataclass (+ `plain_state` property) | Top-level container aggregating all of the above; `plain_state` provides a non-visual/plain-text equivalent of the entire snapshot. |

`ExecutionSnapshot.live_health` always returning `Health.LOCKED` is worth calling out explicitly: it means the GUI itself cannot display a "live" health state under any snapshot input, independent of and in addition to the backend `live_enabled: false` config gate documented in `docs/model_governance_and_shadow_policy.md`.

## Responsive behavior (`AppWindow`)

- Sidebar: `QListWidget`, 208px normal width / 72px compact width.
- `_update_responsive_layout()` switches to the compact sidebar below a 1180px window-width breakpoint.
- `reset_layout()` restores a canonical 1280×720 window, dark theme, 1.0 GUI scale.
- `set_gui_scale(scale)` is a separate, independently-testable scale factor from the responsive breakpoint.
- Snapshot delivery always runs through `SnapshotWorker` (background thread) in production (`start_timer=True` path) — `refresh_from_snapshot()` never calls the snapshot provider synchronously on the Qt UI thread, avoiding UI stalls from slow snapshot construction.
- Trust strip (persistent, always visible): `_source_badge` ("SOURCE UNKNOWN" default), `_integrity_badge` ("CAPTURE IDLE" default), `_drops_badge` ("SESSION DROPS 0", ok by default), `_live_badge` ("LIVE LOCKED", locked — matches `ExecutionSnapshot.live_health`). The status bar also always appends "LIVE LOCKED" text regardless of snapshot content.

## Recommendations for the premium visual upgrade

1. **Do not replace `design_tokens.py`'s color/spacing/radius scale** — it is already a coherent, comprehensively-adopted dark/glass-morphism system matching the requested aesthetic (deep navy/black base, electric blue/cyan/violet/emerald accents, glow shadows). Extend it (e.g. richer glass blur/gradient tokens, additional chart palette entries) rather than starting over.
2. ~~Delete the dead `_DARK`/`_LIGHT` constants in `app_window.py`~~ — **done (Task #2).** Confirmed via source grep: no `_DARK`/`_LIGHT` QSS constants or references remain in `app_window.py`; `THEMES = DASHBOARD_THEMES` is the sole indexed theme source.
3. ~~Reconcile `StatusBadge`'s badge color logic with `design_tokens.py`'s `BadgeStyle` instances~~ — **done (Task #1).** `StatusBadge.set_status()` now only sets `setProperty("state", ...)` and relies on `theme.py`'s `QLabel[state="..."]` QSS selectors as the single source of truth; the old inline `style_map` dict is gone.
4. **Preserve every existing screen, component, and accessibility pairing** (`table_text()`, `ChartPanel` summaries, `CapabilityEmptyState`, `empty_reason`, text+color badges) — these encode the project's evidence-honesty and accessibility principles and must not be dropped for a purely visual refresh.
5. **New panels should be added only where they materially improve evidence visibility** per the standing mandate: registry/dataset lineage, shadow-vs-rules comparison, drift/validation/approval evidence, risk-control observability, decision auditability — not for decoration.
6. **`MainWindow`/`APP_STYLESHEET` are out of scope** for the premium upgrade; do not modify unless a specific regression is found, since `tools/start_prototype.py` and `tests/test_gui_main_window.py` still depend on it.

## Related documents

- `docs/model_governance_and_shadow_policy.md` — ML lifecycle, approval gates, and the backend `live_enabled`/`live_mode` safety boundary this GUI's `ExecutionSnapshot.live_health`/trust-strip badges visually reflect.
- `docs/AUTONOMOUS_HANDOFF.md` — current verified repository state and unresolved eligibility gap.
- `docs/autonomous_learning_worklog.md` — phase-by-phase investigation and fix narrative.

## Phase 2 premium polish addendum

A follow-on design review (2026-07-29) evaluated the glass-morphism pass documented above against trading-dashboard UX standards (hierarchy, decision support, accessibility, safety-state visibility) and produced verdicts on `docs/glass_morphism_upgrade.md`'s "Future Enhancements" list plus additional recommendations and a full screen/tab parity matrix. See:

- `docs/audits/ui_product_design.md` — full review, per-item verdicts, and prioritized implementation plan.
- `docs/ui_page_parity_matrix.md` — screen-by-screen (AppWindow) and tab-by-tab (MainWindow) confirmation that nothing existing is removed, merged, or replaced.

This section intentionally does not restate the architecture inventory above, which remains the historical baseline.

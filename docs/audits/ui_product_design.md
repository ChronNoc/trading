# UI Product Design Review — Premium Polish Phase 2

_Reviewer: read-only UI product design agent. Date: 2026-07-29. Branch: feature/automatic-runtime._
_Scope: AppWindow (app/gui/app_window.py) 8-screen production GUI, plus MainWindow (app/gui/main_window.py) legacy 13-tab window for parity-preservation purposes only. No files modified._

## 1. User goal, operating context, time-critical decisions

The operator's goal is rapid, trustworthy situational awareness of a **shadow/paper-only** trading assistant: is the feed live and undelayed, is capture healthy, is a setup being accepted/rejected and why, is the paper account inside risk limits, and — critically — is LIVE broker execution locked. There is no live-trading decision to make in this build (`ExecutionSnapshot.live_health` is hard-coded `Health.LOCKED`, `app/gui/view_models.py`), so the single highest-stakes UI job is **never implying** live capability exists. Time-critical reads are: source/provenance (delayed vs undelayed), capture drops, setup accept/reject, and the LIVE LOCKED state — all four already live in the persistent trust strip (`AppWindow.__init__`, `app/gui/app_window.py:92-103`).

## 2. Current information architecture (verified)

- `AppWindow` (`app/gui/app_window.py:46`) — `QListWidget` sidebar (208px / 72px compact, breakpoint 1180px logical width, `_update_responsive_layout` at `app_window.py:160-170`) + `QStackedWidget` of 8 screens (`SCREEN_ORDER`, `app/gui/screens.py:39-42`) + a persistent trust strip (4 `StatusBadge`s: source, integrity, drops, live) + status bar.
- Screens are `Card`/`StatTile`/`StatusBadge`/`MetricMeter`/`EvidenceTable`/`ChartPanel` compositions (`app/gui/screens.py`), all snapshot-rendered from `AppSnapshot` (`app/gui/view_models.py`), no screen owns raw ad hoc QSS beyond the `_heading()` helper.
- Refresh: background `SnapshotWorker` at 150ms (5-10Hz, `app_window.py:41`), never synchronous on the Qt thread in production — good, prevents UI stalls.
- Accessibility: GUI-scale zoom (`set_gui_scale`, `app_window.py:241-262`, clamped 0.8-2.0, with the documented descendant-font-repropagation fix), every `StatusBadge` pairs text + color (never color-only, confirmed by `StatusBadge.set_status`, `widgets.py:105-113`), every chart has a `table_text()` twin rendered next to it (`ChartPanel`, `charts.py:172-198`).
- Empty/unavailable states: `CapabilityEmptyState` (`widgets.py:152-174`) explicitly explains *why* a capability is unavailable rather than showing fabricated zeros — used today only on Sessions & Replay (`screens.py:371`). `PaperSnapshot.empty_reason` (`view_models.py`) is threaded through Overview/Paper Trading text.

This is an unusually disciplined foundation for a safety-critical tool: text-paired status, explicit empty-state honesty, and a hard-coded LOCKED execution health are load-bearing product decisions, not decoration, and the addendum work must not regress any of them.

## 3. Verdicts on the 4 listed "Future Enhancements" (docs/glass_morphism_upgrade.md)

### 3.1 Animated glass-overlay on card hover — **DO NOT DO (as literally stated); do a reduced, purposeful version instead**

Verdict: hover-only visual feedback on `Card` (a `QFrame`, not a button) is a **decorative novelty risk**, not a comprehension aid, for two reasons confirmed by code: (a) `Card` (`widgets.py:23-55`) has no click/interactive semantics at all — it is a static section container, so hovering it and having it visually react implies interactivity that doesn't exist, which is an anti-pattern for a trust-critical tool (never signal affordance where none exists); (b) Qt has no native `:hover` transition/animation primitive for `QFrame` background — achieving "animated" would require a `QPropertyAnimation` driving a custom property + `enterEvent`/`leaveEvent` overrides, real but nontrivial engineering for a widget that isn't actionable.

Recommendation: **do not add hover state to plain `Card`.** Reserve hover/glow feedback exclusively for widgets that are genuinely interactive — the 3 `QPushButton`s in `ExecutionScreen` (`screens.py:456`) and any future actionable card. If a "hoverable card" pattern is wanted later (e.g., a card that expands or is clickable), gate it explicitly behind a `Card(title, object_name, interactive=True)` constructor flag so static cards are never mistaken for interactive ones — do not make all 30 card instances hoverable by default.

### 3.2 Subtle glow effect on interactive cards (execution, risk gate) — **WORTH DOING, with a correction to the target**

Verdict: partially correct instinct, wrong target. "Execution" and "Risk Lucid" cards (`ExecutionScreen`/`RiskLucidScreen`, `screens.py:448-460`, `433-445`) are **not interactive** — they are read-only evidence panels containing a few real buttons. Glow is valuable, but it should be applied to the **actual safety-relevant elements**, which already have unused design tokens waiting: `dt.SHADOW_GLOW_ERROR`/`BADGE_LOCKED` for the `"locked"` `StatusBadge` state (LIVE LOCKED badges: `overview_live_locked`, `execution_live_locked`, trust-strip `_live_badge`), and `dt.SHADOW_GLOW_WARNING`-equivalent (none currently defined — see 4.1) for the `"fail"`/`"warn"` states. This turns "glow" into a genuine severity signal (a hard safety lock literally has a subtle red/amber halo) rather than a generic premium card treatment applied irrespective of content.

Recommendation: add a `QGraphicsDropShadowEffect`-based glow **only** to `StatusBadge` instances whose `state == "locked"` or `state == "fail"`, applied in `StatusBadge.set_status()` (`widgets.py:105-113`) by attaching/removing a `QGraphicsDropShadowEffect` colored from `dt.COLOR_ERROR_GLOW`/a new `COLOR_LOCKED_GLOW` token, not via QSS (QSS still can't do box-shadow). Concretely: in `set_status`, when `effective_state in ("locked", "fail")`, call `self.setGraphicsEffect(QGraphicsDropShadowEffect(...))` with `blurRadius=12`, offset `(0,0)`, color from token; otherwise `self.setGraphicsEffect(None)`. This is cheap (one label, not 30 cards), semantically correct (the LOCKED state — the entire point of this app's safety story — gets the strongest visual weight in the interface), and testable via `graphicsEffect() is not None`.

### 3.3 Chart panel frosted-glass backgrounds with data-driven gradient accents — **DO NOT DO as "data-driven gradient," DO a restrained fixed background**

Verdict: charts (`HistoryChart`/`AggressorBar`, `charts.py`) currently paint on a fully transparent `QWidget` background sitting directly inside a `Card`'s body — they already inherit the card's glass gradient underneath them, so a *second*, chart-specific gradient risks visual noise competing with the data line itself (the line, grid, and latest-value label are the actual content that must stay legible at a glance). "Data-driven gradient accents" (i.e., gradient color/intensity that changes based on live values) is explicitly the kind of decorative-novelty-over-comprehension pattern this review is asked to reject for a safety-critical dashboard — a gradient that shifts with price/CVD adds a second, ambiguous visual channel next to the line itself with no labeled meaning, which violates the codebase's own confirmed principle ("status is never communicated by color alone").

Recommendation: skip the data-driven variant entirely. If chart-panel depth is wanted, add one static, restrained treatment: paint a very subtle vertical gradient fill *under* the line path (from `line` color at ~15% alpha at the line's y-position down to 0% alpha at the chart bottom — a classic "area chart" tint), implemented in `HistoryChart.paintEvent` (`charts.py:47-103`) via `QLinearGradient` + `painter.fillPath()` before stroking the line path, using the same `dt.COLOR_PRIMARY` already used for the line (color consistency, not a new hue). This adds depth without adding a second ambiguous data channel and requires no new tokens.

### 3.4 Sidebar glass separator between sections — **LOW PRIORITY, DO ONLY IF SECTIONS EXIST**

Verdict: the current sidebar (`app_window.py:77-86`) is a flat, ungrouped list of the 8 screen names — there is no "sections" concept in the data or code today (`SCREEN_ORDER` is one flat tuple, `screens.py:39-42`). Adding a "glass separator between sections" presumes a grouping (e.g., Monitoring vs Execution vs Diagnostics) that does not exist and was not requested by any other constraint. Building a separator without first deciding on/justifying groups is solving a problem that hasn't been posed.

Recommendation: defer. If a future iteration groups the 8 screens (e.g., "Market" / "Trading" / "System"), a `QFrame` divider with `border-top: 1px solid rgba(255,255,255,0.08)` (reusing `dt.COLOR_GLASS_BORDER`) between `QListWidgetItem` groups is trivial QSS/layout work at that time — not before. Do not implement speculative grouping now.

## 4. Additional premium-polish recommendations (beyond the 4 listed), scoped to QSS + QGraphicsEffect only

### 4.1 Wire up the dead `ButtonStyle`/`BadgeStyle` token system to the 3 real buttons — HIGH PRIORITY, HIGH VALUE

Verified via `graphify query "ButtonStyle usage"` and direct read: `BUTTON_PRIMARY`, `BUTTON_SECONDARY`, `BUTTON_DANGER`, `BUTTON_GHOST` (`design_tokens.py:229-255`) are defined but have **zero consumers** anywhere in the codebase (no `find_referencing_symbols` hits, no import site). Meanwhile the 3 real interactive buttons in the entire AppWindow — `execution_connect_demo`, `execution_sync_now`, `execution_disconnect` (`screens.py:456`) — all render identically via the single generic `QPushButton` QSS rule in `theme.py:138-140`/`168-169`. This means the one screen with real user actions (Execution) gives **no visual distinction between a benign action (Connect/Sync) and a more consequential one (Disconnect)** — a real usability gap for a safety-conscious tool, not a cosmetic one.

Recommendation: add `QPushButton[variant="primary"|"secondary"|"danger"|"ghost"]` QSS selectors to `theme.py`'s `DASHBOARD_DARK`/`DASHBOARD_LIGHT` (mirroring the existing `role`/`state` property pattern), sourcing colors from the *already-defined* `dt.BUTTON_*` dataclasses' `.background`/`.background_hover`/`.text`/`.border` fields (Python still owns the constant, QSS still owns the palette — same split already established for `Card`/`StatusBadge`). Then in `ExecutionScreen.__init__` (`screens.py:456`), call `self.disconnect_button.setProperty("variant", "danger")`, `self.connect_button.setProperty("variant", "primary")`, `self.sync_button.setProperty("variant", "secondary")`. This is the single highest-value polish item in this review because it closes an existing token/adoption gap using code that already exists, rather than adding anything new.

### 4.2 Focus-visible ring consistency for keyboard navigation — MEDIUM PRIORITY (accessibility)

`theme.py:140`/`169` already defines `QPushButton:focus, QTableWidget:focus, QListWidget:focus { border: 2px solid ... }` — good, but it omits the sidebar itself's row-level focus indicator distinct from selection color, and omits any focus ring for `StatTile`/`Card` (neither is focusable today, confirmed — no `setFocusPolicy` call in `widgets.py` for `Card`/`StatTile`, only `HistoryChart`/`AggressorBar`/`EvidenceTable` set `StrongFocus`). Given `HistoryChart`/`AggressorBar` are keyboard-focusable (`charts.py:24`, `115`) but have no `:focus` QSS rule of their own, a keyboard user tabbing through Live Order Flow gets no visible indication which chart is focused. Recommend adding `QWidget#flow_price_chart:focus, QWidget#flow_cvd_chart:focus, QWidget#flow_aggressor_bar:focus { border: 2px solid <focus color>; border-radius: 6px; }`-style rules, or a generic `[accessibleFocusable="true"]` property-based selector applied to all three chart object names, matching the existing focus-ring color token per theme.

### 4.3 WCAG contrast — verified compliant, call this out as a finding not a gap

I computed WCAG 2.x contrast ratios for the dark palette's key text/background pairs directly from the hex values in `theme.py`: badge text/background pairs range 8.26:1-9.59:1 (locked, ok, warn, fail, neutral all pass AAA for normal text, >=7:1), sidebar item text on sidebar background 10.28:1, caption/muted text on card background 6.07:1 (passes AA large text and is very close to AAA), headline text 17.91:1. This is a genuine strength worth stating explicitly in the addendum rather than silently assuming — do not let future polish work (e.g., new glow colors, gradient overlays) regress these ratios; any new token added per 3.2/4.1 should be contrast-checked the same way before merging.

### 4.4 Card border-radius token drift — LOW PRIORITY, cleanup only

`theme.py`'s `QFrame[role="card"]` hardcodes `border-radius:10px` (both palettes) while `design_tokens.py` defines `RADIUS_LG=8` (cards) and `RADIUS_XL=12` (large containers) — 10px matches neither named token. Not a visible bug, but if `4.1`'s QSS additions are done by copying values out of `dt.*`, this is a good moment to align the card radius to one of the two named constants (`RADIUS_LG` is documented as "Cards, panels" — the literal comment in `design_tokens.py:137` — so 10px should likely become 8px, or the token should be renamed/added as `RADIUS_CARD=10` if 10 is intentional). Flag for the implementer to decide; not a blocker.

## 5. Screen-by-screen and MainWindow-tab-by-tab parity matrix

See `docs/ui_page_parity_matrix.md` (separate file) for the full matrix. Summary: **nothing is proposed for removal**; both `AppWindow` (8 screens) and `MainWindow` (13 tabs) are preserved per the Phase 2 mission constraint, and `MainWindow`/`APP_STYLESHEET` remain explicitly out of scope for this polish pass (consistent with `docs/ui_redesign_system.md`'s own recommendation #6).

## 6. Prioritized, actionable implementation plan for the lead agent

1. **(Highest value, lowest risk)** Wire `BUTTON_PRIMARY/SECONDARY/DANGER/GHOST` tokens into `theme.py` QSS + apply `setProperty("variant", ...)` to the 3 `ExecutionScreen` buttons (`screens.py:456`). See 4.1.
2. **(High value, safety-signal correctness)** Add glow `QGraphicsDropShadowEffect` to `StatusBadge` only for `state in ("locked", "fail")`, inside `set_status()` (`widgets.py:105-113`). Requires one new token, e.g. `COLOR_LOCKED_GLOW = "rgba(165, 123, 37, 0.35)"` in `design_tokens.py` alongside existing `COLOR_LOCKED_*` constants. See 3.2.
3. **(Medium value, accessibility)** Add `:focus` QSS rules for the 3 focusable chart/bar object names (`flow_price_chart`, `flow_cvd_chart`, `flow_aggressor_bar`, plus any other `HistoryChart`/`AggressorBar` instances across screens) in both `DASHBOARD_DARK`/`DASHBOARD_LIGHT`. See 4.2.
4. **(Medium value, restrained visual depth)** Add a static area-gradient fill under the `HistoryChart` line path in `paintEvent` (`charts.py:82-92`), using existing `dt.COLOR_PRIMARY` at low alpha via `QLinearGradient`. See 3.3.
5. **(Low priority, do only if requested)** Sidebar section grouping + separator — defer until a grouping scheme is actually proposed. See 3.4.
6. **(Skip)** Do not add generic hover-glass-overlay to all `Card` instances — no interactive semantics exist on `Card` today; would create a false interactivity affordance. See 3.1.
7. **(Cleanup, optional)** Reconcile card `border-radius:10px` against `RADIUS_LG`/`RADIUS_XL` tokens while touching `theme.py` for item 1. See 4.4.

Every change above should be verified against the existing `tests/test_app_window.py` suite (47 tests per `docs/glass_morphism_upgrade.md`) plus the new `tests/test_gui_visual_regression.py`/`tools/bless_gui_baselines.py` (both present as untracked files in git status) before considering it complete.

## Blockers and unverifiable assumptions

- No GUI was run or screenshots captured (read-only reviewer role); all visual claims above are derived from QSS/Python source and computed WCAG contrast math, not rendered screenshots. Actual on-screen glow/gradient appearance should be visually spot-checked by the implementer after coding, e.g. via `tools/capture_gui_screenshots.py`.
- Whether `MainWindow`'s 165 graph edges (per `docs/audits/runtime_architecture.md` Section 4) reflect genuine day-to-day operator use of the legacy window (vs. mostly test scaffolding) was flagged as unverified in the runtime-architecture audit and remains unverified here; this review treats `MainWindow` as preserved-but-out-of-scope regardless, consistent with the mission constraint.
- The WCAG numbers above are computed from the literal hex values in `theme.py` (the correct and standard way to verify a palette), not from a screen-capture pixel measurement.

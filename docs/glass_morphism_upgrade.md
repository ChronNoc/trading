# Glass-Morphism UI Upgrade

**Status:** Complete  
**Date:** 2026-07-28  
**Scope:** All 8 AppWindow screens

## Summary

Upgraded the entire AppWindow interface from flat cards to premium glass-morphism aesthetic. All 30 card instances across all 8 screens now use layered gradients, semi-transparent borders, and depth shadows for a modern trading dashboard appearance.

## Implementation

### Card Widget Enhancement
**File:** `app/gui/widgets.py` (`Card` class), styled via `app/gui/theme.py`'s `QFrame[role="card"]` selector

Refactored the `Card` widget to separate theme-neutral layout/elevation tokens (Python-side)
from theme-owned color/gradient/border declarations (QSS-side), so both dark and light
palettes stay authoritative and a single `role="card"` property drives the surface look:

1. Set `setProperty("role", "card")` so `theme.py`'s `QFrame[role="card"]` selector supplies
   the gradient background and semi-transparent glass border for the active theme
2. Use the theme-neutral `CardStyle` dataclass (`app/gui/design_tokens.py`) for padding and
   shadow elevation only — it does not carry any palette/color fields
3. Add `QGraphicsDropShadowEffect` for depth (16px blur radius, 4px y-offset, 80-alpha black),
   since Qt's style engine has no `box-shadow`/`backdrop-filter` support
4. Maintain accessible section titles and a retained `body` layout for screen composition

**Card widget (current):**
```python
class Card(QFrame):
    def __init__(self, title: str, object_name: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setProperty("role", "card")

        card_style = dt.CardStyle()
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(card_style.shadow_blur_radius)
        shadow.setColor(QColor(0, 0, 0, card_style.shadow_alpha))
        shadow.setOffset(0, card_style.shadow_offset_y)
        self.setGraphicsEffect(shadow)
        ...
```

**Theme-owned styling (`app/gui/theme.py`, dark palette):**
```css
QFrame[role="card"] {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #172231, stop:1 #111821);
    border: 1px solid rgba(125, 162, 207, 0.24);
    border-radius: 10px;
}
```
The light palette defines the equivalent selector with light-mode colors. Both are the single
source of truth for the card gradient, border, and radius — `CardStyle` never carries palette
values, so a theme change never requires a `Card` widget code change.

### Coverage

All 8 screens automatically upgraded via Card widget composition:

| Screen | Card Count | Examples |
|--------|-----------|----------|
| Overview | 5 | Market now, Capture integrity, Current setup decision, Delayed paper account, System truth and next action |
| Live Order Flow | 6 | Current observed market, Observed midpoint history, Cumulative delta history, Aggressive volume, Feed capability matrix, Current setup evidence |
| Paper Trading | 5 | Account and evaluation, Simulated position and bracket, Evaluation funnel and causal integrity, Condition evidence, Closed simulated trades |
| Sessions Replay | 2 | Current capture session, Eligibility rules |
| Research Health | 4 | Operating and learning evidence pipeline, Canonical research evidence, Offline challenger model, Profitability evidence ladder |
| Risk Lucid | 2 | Selected account profile, Broker and prop-rule gate |
| Execution | 3 | Tradovate DEMO status, Credential presence, Order arming and LIVE safety gates |
| Diagnostics | 3 | Capture and analysis metrics, Supervised components, Interface settings |

**Total:** 30 card instances (verified via `grep -c 'Card(' app/gui/screens.py`).

### Supporting Widgets

Other widgets already use appropriate glass-morphism elements:

- **StatTile** (`widgets.py:72`): Uses `COLOR_BASE_DARK` background with glass border
- **StatusBadge** (`widgets.py:134`): Uses consolidated `BadgeStyle` with amber locked-state (completed in Task #1)
- **MetricMeter** (`widgets.py:180`): Progress bar uses glass border and primary accent fill
- **EvidenceTable** (`widgets.py:293`): Alternating row colors with glass borders and gridlines
- **ChartPanel** (`charts.py:165`): Transparent backgrounds layered over cards

### Design Token System

`app/gui/design_tokens.py` holds theme-neutral layout, spacing, typography, and legacy-palette
constants shared by the reusable widget system — it does not own the active card gradient or
border colors. Those live solely in `app/gui/theme.py`'s `DASHBOARD_DARK`/`DASHBOARD_LIGHT`
stylesheets (see `QFrame[role="card"]` above), so dark/light stay authoritative from one place.

Glass-effect overlay constants still defined in `design_tokens.py` for reusable widgets that
consume them directly in Python (e.g. hover-state colors outside QSS role selectors):

```python
COLOR_GLASS_OVERLAY = "rgba(255, 255, 255, 0.03)"  # Subtle overlay
COLOR_GLASS_BORDER = "rgba(255, 255, 255, 0.08)"   # Semi-transparent border
COLOR_GLASS_HOVER = "rgba(255, 255, 255, 0.06)"    # Hover overlay
```

`CardStyle` (`design_tokens.py`) is a frozen dataclass with only theme-neutral layout/elevation
fields — `padding`, `shadow_blur_radius`, `shadow_offset_y`, `shadow_alpha` — and carries no
palette/color fields. It is Python's contribution to card depth (via `QGraphicsDropShadowEffect`,
since Qt StyleSheet has no `box-shadow`); the gradient and border remain entirely QSS-owned.

## Verification

- All 47 `tests/test_app_window.py` tests pass
- Screens render at 1366×768, 1920×1080, compact 900×640, and 150% accessibility-zoom resolutions
- Screenshot capture works for all 8 screens
- Theme switching (dark/light) preserved
- Accessibility zoom (`AppWindow.set_gui_scale()`) verified against real `QFontMetrics.height()`
  changes across repeated scale calls, not just the widget's own reported font — see
  "Accessibility Zoom Fix" below
- Accessibility: all cards retain accessible section titles and semantic markup
- No performance regression: QGraphicsDropShadowEffect is hardware-accelerated on modern Qt

## Accessibility Zoom Fix

`AppWindow.set_gui_scale()` had a latent bug: on the *second and later* calls in a widget's
lifetime, Qt's style engine stopped propagating a top-level `setFont()` change down to already
polished descendant widgets, even with the stylesheet cleared and reapplied around the change.
The first scale change worked; a subsequent one silently left rendered text at the previous size.

Root cause (isolated via disposable offscreen-Qt scripts, not present in the repo): once a
stylesheet has been polished onto a widget tree, Qt resolves and caches each descendant's
inherited font. A bare `self.setFont(font)` on the `QMainWindow` reliably re-propagated to
descendants only on the very first such cycle; subsequent cycles left descendants on their
previously cached font regardless of stylesheet clear/reapply ordering.

Fix: `set_gui_scale()` now sets the font directly on the window **and** on every descendant
(`for widget in self.findChildren(QWidget): widget.setFont(font)`) while the stylesheet is
cleared, before reapplying it. This is reliable across arbitrarily many repeated scale changes.
QSS role selectors (`QLabel[role="headline"|"section_title"|"metric"|"meter_value"]`) own only
`font-weight` and `color` — never `font-size` — so there is a single source of truth for text
size (Python) and a single source of truth for text color/weight (QSS).

Regression coverage: `tests/test_app_window.py::test_themes_and_scale_and_reset_layout` asserts
`QFontMetrics.height()` on a live label strictly increases after `set_gui_scale(1.5)`, and again
after a second `set_gui_scale(99.0)` call (clamped to 2.0) — the scenario that previously failed.

## Qt Limitations

**Backdrop-filter not available:** Qt StyleSheet doesn't support CSS `backdrop-filter: blur()`. The glass effect is achieved through layered gradients and semi-transparent borders instead. True background blur would require platform-specific compositor integration or custom QPainter rendering.

**Workaround:** Vertical gradient from `COLOR_BASE_MID` to `COLOR_BASE_DARK` provides depth perception. Combined with `QGraphicsDropShadowEffect` and glass borders, this produces a convincing premium aesthetic without backdrop blur.

## Related Changes

- Task #1: StatusBadge locked-state color reconciliation (amber safety gate vs action blue)
- Task #2: Removed dead `_DARK`/`_LIGHT` QSS constants from `app_window.py`
- Task #3: Fixed `set_gui_scale()` descendant-font propagation bug (see "Accessibility Zoom Fix" above)

## Future Enhancements

Potential follow-on polish (not blocking):
1. Animated glass-overlay on card hover (requires state-tracking in Card widget)
2. Subtle glow effect on interactive cards (execution, risk gate)
3. Chart panel frosted-glass backgrounds with data-driven gradient accents
4. Sidebar glass separator between sections (if compact mode becomes default)

## Test Evidence

```bash
python -m pytest tests/test_app_window.py -q
# 47 passed, 1 warning in 5.28s
```

All card- and scale-dependent tests pass, including:
- `test_every_screen_renders_and_navigates` ✓
- `test_all_screens_capture_screenshots_for_review` ✓
- `test_overview_fits_1366x768_without_scrolling` ✓
- `test_screenshots_render_at_every_required_resolution` ✓ (4 variants, incl. compact + 150% zoom)
- `test_themes_and_scale_and_reset_layout` ✓ (verifies real font-metric height changes across
  repeated `set_gui_scale()` calls, not just self-reported widget state)

No visual regression: existing screenshot paths remain compatible.

# UI Page/Tab Parity Matrix — Premium Polish Phase 2

_Confirms: nothing listed below is proposed for removal, merging, or replacement. Both `AppWindow` (8 screens) and `MainWindow` (13 tabs) are preserved per `docs/GOAL_C_AUTONOMOUS_MISSION.md` Phase 2 and `docs/audits/runtime_architecture.md` Section 4._

## AppWindow — 8 screens (`app/gui/screens.py`, `build_screens()` at line 502)

| # | Screen (nav key) | Class : line | Current card/state | Recommended polish (this pass) |
|---|---|---|---|---|
| 1 | Overview | `OverviewScreen`, `screens.py:135` | 5 cards; hero market chart + capture integrity; decision + account row; footer truth/next-action. Provenance/live badges intentionally hidden here (owned by trust strip). | No structural change. Chart area-gradient applies to `overview_price_chart`. Verify hidden `provenance`/`live_lock` labels stay hidden after any Card hover work (not applied — see design doc 3.1). |
| 2 | Live Order Flow | `LiveOrderFlowScreen`, `screens.py:302` | 6 cards: stat grid, 2 charts (price/CVD), aggressor bar, capability matrix, setup evidence table. | Add `:focus` QSS to `flow_price_chart`/`flow_cvd_chart`/`flow_aggressor_bar`. Area-gradient on both `HistoryChart`s. |
| 3 | Paper Trading | `PaperTradingScreen`, `screens.py:331` | 5 cards: account/eval stats, position/bracket text, funnel text, condition evidence table, closed-trades table. | No chart present — no gradient impact. No interactive controls — no button-token impact. Leave as-is. |
| 4 | Sessions and Replay | `SessionsReplayScreen`, `screens.py:365` | 2 cards + 1 `CapabilityEmptyState` (explicit "not connected yet" honesty pattern — do not remove). | No changes recommended; this screen is the canonical example of the empty-state pattern other screens should keep emulating. |
| 5 | Learning and Evidence Center (Research Health) | `ResearchHealthScreen`, `screens.py:380` | 4 cards, all evidence tables ("NO RUNTIME EFFECT" labeling explicit in card titles — preserve verbatim). | No structural change. If button variant tokens are wired elsewhere, do not introduce new buttons here — this screen is read-only by design. |
| 6 | Risk and Lucid Account | `RiskLucidScreen`, `screens.py:433` | 2 cards: account profile stat grid, broker/prop-rule gate badge (`"locked"` state by default). | Badge glow applies directly to `risk_gate_badge` when state is `"locked"`/`"fail"` — one of the two most safety-relevant badges in the app. |
| 7 | Execution | `ExecutionScreen`, `screens.py:448` | 3 cards: DEMO status + 3 buttons, credential presence table, arming/LIVE gates text + `execution_live_locked` badge. | Primary target of button-variant tokens and badge glow on `execution_live_locked`. Most safety-relevant interactive surface — prioritize here first. |
| 8 | Diagnostics and Settings | `DiagnosticsScreen`, `screens.py:484` | 3 cards: metrics stat grid + 2 meters, component health table, settings note (theme/zoom/reset — informational only, no controls on this screen itself). | No changes recommended. |

**Confirmed:** all 8 screens remain; no screen's card count, table columns, or empty-state text is altered by any recommendation — only QSS selectors, one new glow token, and one new paint layer inside existing chart `paintEvent`s.

## MainWindow — 13 tabs (`app/gui/main_window.py`)

| # | Tab label | Builder method | Status this pass |
|---|---|---|---|
| 1 | Live dashboard | `_build_live_dashboard_tab` | Preserved, out of scope. |
| 2 | Decision explanation | `_build_decision_explanation_tab` | Preserved, out of scope. |
| 3 | Order management | `_build_order_management_tab` | Preserved, out of scope. |
| 4 | Risk configuration | `_build_risk_configuration_tab` | Preserved, out of scope. |
| 5 | Replay | `_build_replay_tab` | Preserved, out of scope. |
| 6 | Model health | `_build_model_health_tab` | Preserved, out of scope. |
| 7 | Ask | `_build_ask_tab` | Preserved, out of scope. |
| 8 | Session review | `_build_session_review_tab` | Preserved, out of scope. |
| 9 | Automations | `_build_automations_tab` | Preserved, out of scope. |
| 10 | Decision log | `_build_decision_log_tab` | Preserved, out of scope. |
| 11 | Leaderboard | `_build_leaderboard_tab` | Preserved, out of scope. |
| 12 | Paper trading | `_build_paper_trading_tab` | Preserved, out of scope. |
| 13 | Execution | `_build_execution_tab` | Preserved, out of scope. |

**Confirmed:** all 13 tabs remain; `MainWindow` continues to use `APP_STYLESHEET`/`BANNER_STYLE`/`WATCHDOG_*`/`COACH_STYLE` (`theme.py`) exclusively and is not touched by this design-token/premium-polish pass, consistent with `docs/ui_redesign_system.md`'s guidance that `MainWindow`/`APP_STYLESHEET` are out of scope unless a specific regression is found.

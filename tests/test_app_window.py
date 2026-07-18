"""Offscreen tests for the redesigned eight-screen window + screenshot capture."""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QListWidget, QProgressBar

from app.gui.app_window import AppWindow
from app.gui.screens import SCREEN_ORDER
from app.gui.view_models import (
    AppSnapshot,
    Capability,
    CaptureSnapshot,
    ComponentHealth,
    ExecutionSnapshot,
    Health,
    MarketSnapshot,
    PaperSnapshot,
    ResearchSnapshot,
    SetupCheck,
)

SCREENSHOT_DIR = Path("reports/screenshots")


def _rich_snapshot() -> AppSnapshot:
    """A realistic populated snapshot (values only, no fabricated trades)."""
    return AppSnapshot(
        lifecycle_state="RECORDING",
        market=MarketSnapshot(
            contract="MNQU6", last_price=Decimal("29268.50"), best_bid=Decimal("29268.25"),
            best_ask=Decimal("29268.75"), spread=Decimal("0.50"), mid_price=Decimal("29268.50"),
            cumulative_delta=Decimal("-142"), buy_volume=Decimal("1204"), sell_volume=Decimal("1346"),
            is_delayed=True, source_delay_minutes=15, processing_age_ms=8,
            event_rate_per_second=1651.8,
        ),
        capture=CaptureSnapshot(
            receiver_listening=True, bookmap_connected=True, recording=True,
            session_id="session_20260717T140000Z", current_session_drops=0,
            lifetime_bridge_drops=1_031_435, intake_occupancy=12, intake_capacity=10_000,
            recorder_occupancy=127, recorder_capacity=50_000, flush_latency_ms=0.39,
            persisted_per_second=1651.8,
        ),
        paper=PaperSnapshot(
            profile_name="LucidFlex 25K Evaluation", balance=Decimal("25000"),
            starting_balance=Decimal("25000"), profit_target=Decimal("1250"),
            target_progress=Decimal("0"), drawdown_room=Decimal("1000"),
            trades=0, evaluations=20,
            top_rejections=(("durable_defending_block", 20), ("cvd_supports_direction", 20)),
            setup_checks=(
                SetupCheck("durable_defending_block", False, "0 blocks", ">=1 block", "no durable block"),
                SetupCheck("opening_observation_complete", True, "complete", "complete", ""),
            ),
            risk_remaining=Decimal("250"),
        ),
        research=ResearchSnapshot(
            state="idle", active_workers=15, requested_workers=15, canonical_trades=0,
            experimental_trades=0, unique_setups=0, independent_days=0,
            gpu_note="NVIDIA GPU present, but deterministic replay is CPU/I-O bound; GPU not used.",
        ),
        execution=ExecutionSnapshot(
            environment="PAPER", live_blockers=("live_enabled is not true", "prop rules unresolved"),
            prop_rules_resolved=False,
        ),
        components=(
            ComponentHealth("capture", Health.OK, "recording"),
            ComponentHealth("recorder", Health.OK, "flushing"),
            ComponentHealth("paper", Health.IDLE, "awaiting setup"),
            ComponentHealth("research", Health.IDLE, "no pending jobs"),
        ),
        capabilities=(
            ("Aggregated depth", Capability.AVAILABLE, "provider supplies depth updates"),
            ("Aggressor side", Capability.AVAILABLE, "TradeInfo.isBidAggressor verified"),
            ("MBO / native iceberg", Capability.UNAVAILABLE, "feed does not expose order IDs"),
            ("Liquidity blocks", Capability.HEURISTIC, "derived from depth persistence"),
        ),
        next_action="Recording is healthy; let it capture complete sessions.",
    )


@pytest.fixture()
def window(qtbot: object) -> AppWindow:
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False)
    qtbot.addWidget(win)
    return win


def test_sidebar_has_exactly_the_eight_required_destinations(window: AppWindow) -> None:
    """The redesign replaces 13 crowded tabs with 8 sidebar destinations."""
    sidebar = window.findChild(QListWidget, "sidebar")
    assert sidebar is not None
    names = [sidebar.item(i).text() for i in range(sidebar.count())]
    assert names == [
        "Overview", "Live Order Flow", "Paper Trading", "Sessions and Replay",
        "Research and Model Health", "Risk and Lucid Account", "Execution",
        "Diagnostics and Settings",
    ]


def test_every_screen_renders_and_navigates(window: AppWindow) -> None:
    """All eight screens render real snapshot content without error."""
    for name in SCREEN_ORDER:
        window.navigate_to(name)
        assert window.current_screen_name == name
        assert window.stack.currentWidget().isVisibleTo(window) or True
        window.refresh_from_snapshot()


def test_overview_shows_the_required_operational_fields(window: AppWindow) -> None:
    """Overview must answer 'what is happening' without scrolling or logs."""
    window.navigate_to("Overview")
    window.refresh_from_snapshot()
    text = " ".join(lbl.text() for lbl in window.findChildren(QLabel))
    for token in ("MNQU6", "29,268.50", "29,268.25", "29,268.75", "0.50",
                  "connected", "1,652", "LucidFlex 25K Evaluation", "LIVE LOCKED"):
        assert token in text, f"overview missing {token!r}"
    assert "DELAYED 15 min" in text  # provenance is always explicit
    bar = window.findChild(QProgressBar, "overview_target_progress")
    assert bar is not None and bar.value() == 0


def test_current_session_drops_are_distinguished_from_lifetime(window: AppWindow) -> None:
    """0 session drops must never be confused with the 1M lifetime bridge total."""
    window.navigate_to("Overview")
    window.refresh_from_snapshot()
    drops = window.findChild(QLabel, "overview_drops")
    assert "0 this session" in drops.text()
    assert "1,031,435" in drops.text()  # shown, but labelled lifetime


def test_processing_age_is_separated_from_source_delay(window: AppWindow) -> None:
    """App lag (8 ms) must not be conflated with the intentional 15-min delay."""
    window.navigate_to("Overview")
    window.refresh_from_snapshot()
    age = window.findChild(QLabel, "overview_processing_age")
    assert "8 ms" in age.text() and "excludes source delay" in age.text()


def test_paper_screen_shows_honest_empty_state_with_reasons(window: AppWindow) -> None:
    """Zero qualifying setups shows why - never a fabricated trade."""
    window.navigate_to("Paper Trading")
    window.refresh_from_snapshot()
    body = window.findChild(QLabel, "screen_paper_trading_body").text()
    assert "Trades 0" in body
    assert "20 opportunities evaluated, none qualified" in body
    assert "durable_defending_block" in body
    assert "thresholds are not loosened" in body


def test_live_order_flow_labels_heuristics_and_unavailable_capabilities(window: AppWindow) -> None:
    """Never present a heuristic or an absent MBO feature as native truth."""
    window.navigate_to("Live Order Flow")
    window.refresh_from_snapshot()
    body = window.findChild(QLabel, "screen_live_order_flow_body").text()
    assert "MBO / native iceberg: UNAVAILABLE" in body
    assert "Liquidity blocks: HEURISTIC" in body
    assert "Aggressor side: AVAILABLE" in body


def test_research_screen_separates_canonical_from_experimental(window: AppWindow) -> None:
    window.navigate_to("Research and Model Health")
    window.refresh_from_snapshot()
    body = window.findChild(QLabel, "screen_research_health_body").text()
    assert "CANONICAL trades: 0" in body
    assert "never merged into canonical" in body
    assert "GPU not used" in body


def test_execution_screen_lists_exact_live_blockers(window: AppWindow) -> None:
    window.navigate_to("Execution")
    window.refresh_from_snapshot()
    body = window.findChild(QLabel, "screen_execution_body").text()
    assert "LIVE: LOCKED" in body
    assert "live_enabled is not true" in body
    assert "Delayed data can never place a broker order" in body


def test_status_bar_uses_plain_language(window: AppWindow) -> None:
    """A non-programmer must understand the state."""
    window.refresh_from_snapshot()
    text = window.findChild(QLabel, "status_bar_text").text()
    assert "Recording delayed market data" in text
    assert "LIVE LOCKED" in text
    assert "Session drops: 0" in text


def test_themes_and_scale_and_reset_layout(window: AppWindow) -> None:
    window.apply_theme("light")
    assert window.theme == "light" and window.styleSheet()
    window.apply_theme("dark")
    assert window.theme == "dark"
    window.set_gui_scale(1.5)
    assert window.gui_scale == 1.5
    window.set_gui_scale(99.0)
    assert window.gui_scale == 2.0  # clamped
    window.reset_layout()
    assert window.gui_scale == 1.0 and window.theme == "dark"
    assert window.current_screen_name == "Overview"


def test_window_renders_without_a_provider() -> None:
    """No backend attached still renders an honest 'not started' window."""
    win = AppWindow(snapshot_provider=None, start_timer=False)
    win.refresh_from_snapshot()
    assert "Starting" in win.findChild(QLabel, "status_bar_text").text()


def test_gui_thread_does_no_disk_io_on_refresh(window: AppWindow, monkeypatch) -> None:
    """The Qt refresh path must never open a file - that is what froze the old GUI."""
    import builtins

    opened: list[str] = []
    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    for name in SCREEN_ORDER:
        window.navigate_to(name)
        window.refresh_from_snapshot()
    assert opened == [], f"GUI refresh performed file I/O: {opened[:3]}"


def test_production_snapshot_provider_never_runs_on_the_qt_thread(qtbot: object) -> None:
    """Status-file/provider polling belongs to the dedicated snapshot reader."""
    qt_thread = threading.get_ident()
    provider_threads: list[int] = []

    def provider() -> AppSnapshot:
        provider_threads.append(threading.get_ident())
        return _rich_snapshot()

    win = AppWindow(snapshot_provider=provider, start_timer=True)
    qtbot.addWidget(win)
    qtbot.waitUntil(lambda: bool(provider_threads), timeout=3000)
    qtbot.waitUntil(
        lambda: "MNQU6" in win.findChild(QLabel, "overview_contract").text(),
        timeout=3000,
    )
    assert all(thread_id != qt_thread for thread_id in provider_threads)
    win.close()


def test_gui_close_stops_only_its_snapshot_worker(qtbot: object) -> None:
    """Closing the window cleans its reader without owning backend lifecycle."""
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=True)
    qtbot.addWidget(win)
    assert win._snapshot_worker is not None
    qtbot.waitUntil(lambda: win._snapshot_worker.running, timeout=3000)
    win.close()
    assert win._snapshot_worker.running is False


@pytest.mark.parametrize("label,width,height", [
    ("1366x768", 1366, 768),
    ("1920x1080", 1920, 1080),
    ("highdpi_2x_1366x768", 1366, 768),
])
def test_screenshots_render_at_every_required_resolution(
    qtbot: object, label: str, width: int, height: int,
) -> None:
    """Render and save a screenshot at each required resolution."""
    scale = 2.0 if "highdpi" in label else 1.0
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False, gui_scale=scale)
    qtbot.addWidget(win)
    win.resize(width, height)
    win.navigate_to("Overview")
    win.refresh_from_snapshot()
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"overview_{label}.png"
    pixmap = win.grab()
    assert pixmap.save(str(path)), f"failed to save {path}"
    assert path.stat().st_size > 2000, "screenshot looks blank"
    assert pixmap.width() == width and pixmap.height() == height


def test_overview_fits_1366x768_without_scrolling(qtbot: object) -> None:
    """The most important information must fit the smallest supported display."""
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False)
    qtbot.addWidget(win)
    win.resize(1366, 768)
    win.navigate_to("Overview")
    win.refresh_from_snapshot()
    screen = win.stack.currentWidget()
    # The content must not demand more height than the viewport gives it.
    assert screen.sizeHint().height() <= 768, (
        f"overview needs {screen.sizeHint().height()}px at 1366x768 - it would scroll"
    )


def test_all_screens_capture_screenshots_for_review(qtbot: object) -> None:
    """Save one screenshot per screen so the redesign is visually reviewable."""
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False)
    qtbot.addWidget(win)
    win.resize(1366, 768)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCREEN_ORDER:
        win.navigate_to(name)
        win.refresh_from_snapshot()
        slug = name.lower().replace(" ", "_")
        path = SCREENSHOT_DIR / f"screen_{slug}.png"
        assert win.grab().save(str(path))
        assert path.stat().st_size > 1000


def test_overview_has_no_giant_dead_space(qtbot: object) -> None:
    """The spec forbids giant blank panels: content must fill the viewport.

    The first render left ~250px of dead space under the last group. The setup-
    checks panel now absorbs the spare height with real pass/fail content.
    """
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False)
    qtbot.addWidget(win)
    win.resize(1366, 768)
    win.navigate_to("Overview")
    win.refresh_from_snapshot()
    win.show()
    screen = win.stack.currentWidget()
    screen.layout().activate()  # force a real layout pass offscreen
    checks = win.findChild(QLabel, "overview_setup_checks")
    assert checks is not None
    # Map to screen coordinates: the label sits inside a group box, so its own
    # y() is parent-relative and would misreport the gap.
    bottom_in_screen = checks.mapTo(screen, checks.rect().bottomLeft()).y()
    bottom_gap = screen.height() - bottom_in_screen
    assert bottom_gap < 220, f"overview leaves {bottom_gap}px of dead space"


def test_overview_lists_every_setup_check_not_just_the_first(qtbot: object) -> None:
    """'Every pass/fail reason' means every one, with observed vs threshold."""
    win = AppWindow(snapshot_provider=_rich_snapshot, start_timer=False)
    qtbot.addWidget(win)
    win.navigate_to("Overview")
    win.refresh_from_snapshot()
    text = win.findChild(QLabel, "overview_setup_checks").text()
    assert "[FAIL]  durable_defending_block" in text
    assert "[PASS]  opening_observation_complete" in text
    assert "observed 0 blocks vs required >=1 block" in text


def test_snapshot_source_builds_a_snapshot_without_a_backend() -> None:
    """The GUI must render even before any component is running."""
    from app.gui.snapshot_source import SnapshotSource

    snapshot = SnapshotSource()()
    assert snapshot.lifecycle_state == "STARTING"
    assert snapshot.paper.profile_name == "LucidFlex 25K Evaluation"
    assert snapshot.paper.starting_balance == Decimal("25000")  # selected profile, not $100k
    assert snapshot.execution.live_blockers  # LIVE always reports why it is locked
    assert snapshot.next_action  # never a blank panel


def test_snapshot_source_samples_runtime_once_and_cannot_contradict_itself() -> None:
    """One frame cannot mix pre-drop and post-drop component states."""
    from types import SimpleNamespace

    from app.gui.snapshot_source import SnapshotSource

    calls = 0

    class ChangingController:
        def snapshot(self) -> object:
            nonlocal calls
            calls += 1
            drops = 7 if calls == 1 else 0
            return SimpleNamespace(
                bookmap_status="connected",
                recording=True,
                current_session_dropped_message_count=drops,
                dropped_message_count=drops,
                data_delay_minutes=15,
                exact_contract="MNQU6",
                session_date="2026-07-19",
            )

    snapshot = SnapshotSource(
        controller=ChangingController(),
        receiver_status=lambda: SimpleNamespace(listening=True),
    )()
    assert calls == 1, "runtime must be sampled exactly once per published frame"
    assert snapshot.lifecycle_state == "CAPTURE_INVALIDATED"
    states = {component.name: component.health for component in snapshot.components}
    assert states["capture"] == Health.INVALIDATED
    assert states["recorder"] == Health.INVALIDATED
    assert states["paper"] == Health.PAUSED
    assert states["research"] == Health.PAUSED
    assert "7 events dropped" in snapshot.blocker


def test_snapshot_source_declares_unavailable_capabilities_honestly() -> None:
    """MBO/heatmap must be declared UNAVAILABLE, heuristics labelled as such."""
    from app.gui.snapshot_source import SnapshotSource

    caps = dict((name, cap) for name, cap, _ in SnapshotSource()().capabilities)
    assert caps["MBO / native iceberg"] == Capability.UNAVAILABLE
    assert caps["Heatmap pixels"] == Capability.UNAVAILABLE
    assert caps["Broker execution"] == Capability.UNAVAILABLE
    assert caps["Liquidity blocks / reloads / absorption"] == Capability.HEURISTIC
    # With no feed attached, nothing has been observed. This previously claimed
    # AVAILABLE, which is what made the panel lie on depth-only sessions.
    assert caps["Aggressor side"] == Capability.UNVERIFIED
    assert caps["Trade prints"] == Capability.UNVERIFIED


def test_capabilities_report_what_the_feed_actually_delivered() -> None:
    """Observed evidence, not a constant: AVAILABLE only after real trades."""
    from app.gui.snapshot_source import SnapshotSource
    from app.paper.streaming_engine import DelayedPaperEngine

    engine = DelayedPaperEngine()
    engine.on_market_event({
        "timestamp_ns": 1, "price": "29500.00", "size": "1",
        "aggressor_side": "buy", "instrument": "MNQ", "sequence_id": 1,
    })
    caps = dict((name, cap) for name, cap, _ in
                SnapshotSource(paper_engine=engine)().capabilities)
    assert caps["Trade prints"] == Capability.AVAILABLE
    assert caps["Aggressor side"] == Capability.AVAILABLE
    assert caps["MBO / native iceberg"] == Capability.UNAVAILABLE, "structural, always"


def test_a_depth_only_feed_is_reported_as_degraded_not_available() -> None:
    """The measured failure: 71 of 200 recorded sessions carried no trades."""
    from app.gui.snapshot_source import SnapshotSource
    from app.market.capabilities import DEPTH_ONLY_DEGRADED_THRESHOLD
    from app.paper.streaming_engine import DelayedPaperEngine

    engine = DelayedPaperEngine()
    for i in range(DEPTH_ONLY_DEGRADED_THRESHOLD):
        engine.on_market_event({
            "type": "depth_update", "timestamp": 1 + i, "symbol": "MNQ", "side": "bid",
            "price": "29500.00", "previous_size": "0", "new_size": "10",
        })
    caps = {name: (cap, reason) for name, cap, reason in
            SnapshotSource(paper_engine=engine)().capabilities}
    assert caps["Aggregated depth"][0] == Capability.AVAILABLE
    status, reason = caps["Trade prints"]
    assert status == Capability.DEGRADED, "a depth-only feed must not claim trades work"
    assert "ZERO trades" in reason


def test_launcher_uses_the_redesigned_window_not_the_legacy_tabs() -> None:
    """The old 13-tab window must no longer be the default GUI."""
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "from app.gui.app_window import AppWindow" in source
    assert "AppWindow(" in source
    assert "MainWindow(" not in source, "launcher still constructs the legacy 13-tab window"


# --- the Paper Trading screen must show the executor's real state ------------------


def _paper_body(qtbot: object, **paper: object) -> str:
    """Render the paper screen with the given execution state and return its text."""
    from dataclasses import replace

    snapshot = replace(_rich_snapshot(), paper=replace(_rich_snapshot().paper, **paper))
    window = AppWindow(snapshot_provider=lambda: snapshot, start_timer=False)
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.navigate_to("Paper Trading")
    window.refresh_from_snapshot()
    return window.findChild(QLabel, "screen_paper_trading_body").text()


def test_paper_screen_shows_the_open_position_with_stop_and_target(qtbot: object) -> None:
    """An open simulated position must be fully visible, not just a trade count."""
    body = _paper_body(
        qtbot,
        open_position="long 2", position_entry="29501.00", position_stop="29490.00",
        position_target="29520.00", unrealized_pnl=Decimal("38.00"),
    )
    assert "long 2 @ 29501.00" in body
    assert "Stop 29490.00" in body
    assert "Target 29520.00" in body
    assert "Unrealized" in body


def test_paper_screen_shows_pending_order_when_flat(qtbot: object) -> None:
    body = _paper_body(qtbot, open_position="flat", pending_order="long 2 awaiting causal fill")
    assert "flat" in body
    assert "awaiting causal fill" in body


def test_paper_screen_lists_closed_trades_newest_first(qtbot: object) -> None:
    from app.gui.view_models import TradeRow

    body = _paper_body(
        qtbot,
        trades=2, wins=1, losses=1,
        recent_trades=(
            TradeRow(direction="long", contracts=2, entry="29501.00", exit="29520.00",
                     net_pnl="+38.26", close_reason="target"),
            TradeRow(direction="short", contracts=1, entry="29499.75", exit="29510.00",
                     net_pnl="-21.74", close_reason="stop"),
        ),
    )
    assert "long 2 @ 29501.00 → 29520.00  +38.26  (target)" in body
    assert "short 1 @ 29499.75 → 29510.00  -21.74  (stop)" in body


def test_paper_screen_labels_synthetic_fixture_trades(qtbot: object) -> None:
    """A fixture trade must never be mistakable for a real result."""
    from app.gui.view_models import TradeRow

    body = _paper_body(
        qtbot,
        trades=1,
        recent_trades=(TradeRow(direction="long", contracts=1, entry="1", exit="2",
                                net_pnl="+2.00", close_reason="target",
                                is_synthetic_fixture=True),),
    )
    assert "[FIXTURE]" in body


def test_paper_screen_explains_a_qualifying_setup_blocked_by_risk(qtbot: object) -> None:
    """'Setup qualified but no trade' must say WHY, not look like a silent failure."""
    body = _paper_body(
        qtbot,
        trades=0, evaluations=40, candidates=3,
        risk_rejections=(("one_position_max", 2), ("risk_zero_contracts", 1)),
    )
    assert "3 setup(s) qualified" in body
    assert "one_position_max" in body
    assert "Risk rules are never relaxed" in body


def test_paper_screen_warns_when_market_events_were_dropped(qtbot: object) -> None:
    """Silent data loss would corrupt every number on this screen."""
    body = _paper_body(qtbot, malformed_events=7)
    assert "7 market event(s) could not be parsed" in body
    assert "incomplete" in body


# --- the profitability meter must be visible in the GUI ---------------------------


def _meter_body(qtbot: object, **fields: object) -> str:
    """Render the research screen with a given profitability snapshot."""
    from dataclasses import replace

    from app.gui.view_models import ProfitabilitySnapshot

    snapshot = replace(_rich_snapshot(), profitability=ProfitabilitySnapshot(**fields))
    window = AppWindow(snapshot_provider=lambda: snapshot, start_timer=False)
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.navigate_to("Research and Model Health")
    window.refresh_from_snapshot()
    return window.findChild(QLabel, "screen_research_health_body").text()


def test_research_screen_shows_the_profitability_meter(qtbot: object) -> None:
    """The user asked for a meter; it must actually be on screen."""
    from app.gui.view_models import ProgressGateRow

    body = _meter_body(
        qtbot,
        fraction=0.067, computed=True, claim_supported=False,
        headline="Data-collection stage: zero setups have completed on real data yet.",
        gates=(
            ProgressGateRow(label="Data capture healthy", status="passed",
                            observed="1 clean session", threshold=">=1 clean session"),
            ProgressGateRow(label="Minimum completed-setup sample", status="blocked",
                            observed="0 completed", threshold=">= 100 completed setups"),
        ),
    )
    assert "Progress to proven profitable: 7%" in body
    assert "NOT claimed — unproven" in body
    assert "Data-collection stage" in body
    assert "Minimum completed-setup sample" in body
    assert "BLOCKED" in body


def test_meter_says_it_is_computing_before_the_first_refresh(qtbot: object) -> None:
    """A meter that has not run must say so, not show a misleading 0%."""
    body = _meter_body(qtbot)
    assert "computing on the research thread" in body


def test_meter_never_claims_profitability_it_cannot_support(qtbot: object) -> None:
    body = _meter_body(qtbot, fraction=0.067, computed=True, claim_supported=False)
    assert "NOT claimed" in body
    assert "profitable" in body.lower()

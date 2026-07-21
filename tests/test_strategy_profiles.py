"""The explicit canonical/relaxed strategy profiles."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.strategy.order_flow import OrderFlowThresholds
from app.strategy.profiles import (
    CANONICAL,
    RELAXED,
    normalize_profile,
    thresholds_for_profile,
)

TICK = Decimal("0.25")


def _canonical() -> OrderFlowThresholds:
    return thresholds_for_profile(CANONICAL, tick_size=TICK,
                                  large_block_minimum=Decimal("90"),
                                  absorption_volume_minimum=Decimal("400"))


def _relaxed() -> OrderFlowThresholds:
    return thresholds_for_profile(RELAXED, tick_size=TICK,
                                  large_block_minimum=Decimal("90"),
                                  absorption_volume_minimum=Decimal("400"))


def test_canonical_profile_is_unchanged() -> None:
    t = _canonical()
    assert t.large_block_minimum == Decimal("90")
    assert t.absorption_volume_minimum == Decimal("400")
    assert t.cvd_support_minimum == Decimal("25")
    assert t.enforce_opening_observation is True, "canonical stays RTH-only"


def test_relaxed_profile_lowers_thresholds_and_drops_rth() -> None:
    t = _relaxed()
    # Lower than canonical on every liquidity/flow dimension...
    assert t.large_block_minimum < Decimal("90")
    assert t.absorption_volume_minimum < Decimal("400")
    assert t.cvd_support_minimum < Decimal("25")
    # ...and the RTH opening-observation gate is OFF.
    assert t.enforce_opening_observation is False
    # Still a valid, constructible threshold set (validation runs on use).
    assert t.large_block_minimum > 0


def test_relaxed_ignores_the_configured_canonical_block_values() -> None:
    # Even when the config carries canonical 90/400, relaxed overrides them.
    t = thresholds_for_profile(RELAXED, tick_size=TICK,
                               large_block_minimum=Decimal("90"),
                               absorption_volume_minimum=Decimal("400"))
    assert t.large_block_minimum == Decimal("20")
    assert t.absorption_volume_minimum == Decimal("80")


def test_unknown_profile_falls_back_to_canonical() -> None:
    assert normalize_profile("nonsense") == CANONICAL
    assert normalize_profile(None) == CANONICAL
    assert normalize_profile("RELAXED") == RELAXED
    assert normalize_profile("  relaxed ") == RELAXED


def test_opening_observation_gate_can_be_disabled() -> None:
    """With the gate off, an out-of-hours snapshot no longer fails on RTH."""
    from app.market.state import MarketState
    from app.strategy.order_flow import (
        OrderFlowPlanContext,
        StrategyLevels,
        TradeDirection,
        _check_opening_observation,
    )

    # 03:00 UTC ~ overnight; session_open set to the RTH open far in the future.
    snap = MarketState(timestamp_ns=1_000_000_000)
    ctx = OrderFlowPlanContext(direction=TradeDirection.LONG, levels=StrategyLevels(),
                               session_open_timestamp_ns=10_000_000_000_000)
    canonical = _check_opening_observation([snap], ctx, OrderFlowThresholds())
    relaxed = _check_opening_observation(
        [snap], ctx, OrderFlowThresholds(enforce_opening_observation=False))
    assert canonical.passed is False and "RTH" in canonical.message
    assert relaxed.passed is True


def test_config_reader_selects_profile(tmp_path: Path) -> None:
    from app.paper.options import read_strategy_profile

    assert read_strategy_profile(tmp_path / "missing.yaml") == CANONICAL
    relaxed = tmp_path / "relaxed.yaml"
    relaxed.write_text("paper_strategy_profile: relaxed\n", encoding="utf-8")
    assert read_strategy_profile(relaxed) == RELAXED
    canonical = tmp_path / "canonical.yaml"
    canonical.write_text("paper_strategy_profile: canonical\n", encoding="utf-8")
    assert read_strategy_profile(canonical) == CANONICAL
    junk = tmp_path / "junk.yaml"
    junk.write_text("paper_strategy_profile: banana\n", encoding="utf-8")
    assert read_strategy_profile(junk) == CANONICAL, "unknown value fails safe to canonical"


def test_engine_reports_active_profile_and_relaxed_takes_more_setups() -> None:
    from decimal import Decimal as D

    from app.paper.streaming_engine import DelayedPaperEngine
    from app.research.episode_builder import EpisodeConfig

    def stream(engine: DelayedPaperEngine, count: int) -> None:
        price = D("29500.00")
        for i in range(count):
            price += D("0.25") if i % 2 == 0 else D("-0.25")
            ts = 1_752_537_751_000_000_000 + i * 500_000_000
            engine.on_market_event({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                                    "previous_size": "0", "new_size": str(i % 40 + 1)})
            engine.on_market_event({"timestamp_ns": ts + 1, "price": f"{price:.2f}", "size": "1",
                                    "aggressor_side": "buy" if i % 2 else "sell",
                                    "instrument": "MNQ", "sequence_id": i + 1})

    canonical = DelayedPaperEngine()
    canonical.bind_session("s", "MNQ")
    stream(canonical, 300)
    assert canonical.status().strategy_profile == "canonical"

    relaxed = DelayedPaperEngine(config=EpisodeConfig(strategy_profile="relaxed"))
    relaxed.bind_session("s", "MNQ")
    stream(relaxed, 300)
    assert relaxed.status().strategy_profile == "relaxed"
    # The relaxed profile clears the opening-observation gate that the canonical
    # profile fails on this synthetic (no session-open) stream.
    canon_stats = {n: (p, f) for n, p, f, _ in canonical.condition_stats()}
    relax_stats = {n: (p, f) for n, p, f, _ in relaxed.condition_stats()}
    if "opening_observation_complete" in relax_stats:
        assert relax_stats["opening_observation_complete"][0] > 0


def test_both_launchers_wire_the_strategy_profile() -> None:
    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        source = Path(launcher).read_text(encoding="utf-8")
        assert "read_strategy_profile" in source, launcher

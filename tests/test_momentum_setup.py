"""The OPTIONAL momentum-continuation setup: off by default, honest when on."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.market.state import MarketState
from app.research.episode_builder import EpisodeConfig
from app.strategy.momentum import (
    MOMENTUM_STRATEGY_VERSION,
    DerivedMomentumContext,
    MomentumThresholds,
    derive_momentum_context,
    evaluate_momentum_plan,
)
from app.strategy.order_flow import OrderFlowThresholds, StrategyLevels, TradeDirection


class _StubTracker:
    """Levels provider with no known levels and no session open."""

    def levels_at(self, _ts: int) -> StrategyLevels:
        return StrategyLevels(psychological_interval=None)

    def session_open_timestamp_ns(self, _ts: int) -> None:
        return None


def _snapshots(mid_ticks: list[int]) -> tuple[MarketState, ...]:
    """One snapshot per second; each step also prints a 2-contract buy."""
    state = MarketState()
    snapshots: list[MarketState] = []
    base = 601_000_000_000
    prev: tuple[Decimal, Decimal] | None = None
    for i, ticks in enumerate(mid_ticks):
        ts = base + i * 1_000_000_000
        bid = Decimal("100.00") + Decimal("0.25") * ticks
        ask = bid + Decimal("0.25")
        if prev is not None and prev != (bid, ask):
            state = state.update({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                                  "side": "bid", "price": f"{prev[0]:.2f}",
                                  "previous_size": "10", "new_size": "0"})
            state = state.update({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                                  "side": "ask", "price": f"{prev[1]:.2f}",
                                  "previous_size": "10", "new_size": "0"})
        state = state.update({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                              "side": "bid", "price": f"{bid:.2f}",
                              "previous_size": "0", "new_size": "10"})
        state = state.update({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                              "side": "ask", "price": f"{ask:.2f}",
                              "previous_size": "0", "new_size": "10"})
        state = state.update({"timestamp_ns": ts + 1, "price": f"{ask:.2f}", "size": "2",
                              "aggressor_side": "buy", "instrument": "MNQ",
                              "sequence_id": i + 1})
        prev = (bid, ask)
        snapshots.append(state)
    return tuple(snapshots)


def _clean_long_momentum() -> tuple[MarketState, ...]:
    """12 ticks of upward progress, then a contained 2-tick pullback."""
    return _snapshots([min(12, i // 10) for i in range(130)] + [10] * 20)


def _condition(result: object, key: str) -> object:
    return next(c for c in result.conditions if c.key == key)  # type: ignore[attr-defined]


def test_momentum_plan_accepts_a_clean_continuation() -> None:
    snapshots = _clean_long_momentum()
    derived = derive_momentum_context(
        snapshots, TradeDirection.LONG, _StubTracker(), OrderFlowThresholds(),
        MomentumThresholds(), stop_buffer_points=Decimal("1"),
    )
    assert derived.progress_ticks == Decimal("12")
    assert derived.pullback_ticks == Decimal("2")
    assert derived.swing_price is not None
    assert derived.context.stop_price == derived.swing_price - Decimal("1")

    # No known levels means no DOL target - supply one explicitly, as a
    # populated levels tracker would, then the full checklist must pass.
    with_target = DerivedMomentumContext(
        context=replace(derived.context, target_price=Decimal("104.00")),
        progress_ticks=derived.progress_ticks,
        pullback_ticks=derived.pullback_ticks,
        swing_price=derived.swing_price,
    )
    result = evaluate_momentum_plan(
        snapshots, with_target, OrderFlowThresholds(), MomentumThresholds(),
    )
    failed = [c.key for c in result.conditions if not c.passed]
    assert result.accepted is True, f"unexpected failures: {failed}"
    assert result.setup_name == f"{MOMENTUM_STRATEGY_VERSION}_long"
    # Every condition is momentum-prefixed so statistics never mix with the plan.
    assert all(c.key.startswith("momentum_") for c in result.conditions)


def test_momentum_without_a_target_is_rejected_not_invented() -> None:
    snapshots = _clean_long_momentum()
    derived = derive_momentum_context(
        snapshots, TradeDirection.LONG, _StubTracker(), OrderFlowThresholds(),
        MomentumThresholds(), stop_buffer_points=Decimal("1"),
    )
    result = evaluate_momentum_plan(
        snapshots, derived, OrderFlowThresholds(), MomentumThresholds(),
    )
    assert result.accepted is False
    assert _condition(result, "momentum_clear_target").passed is False


def test_no_momentum_means_no_trade() -> None:
    """A flat tape has nothing to join - the reason names the missing progress."""
    snapshots = _snapshots([0, 1] * 75)
    derived = derive_momentum_context(
        snapshots, TradeDirection.LONG, _StubTracker(), OrderFlowThresholds(),
        MomentumThresholds(), stop_buffer_points=Decimal("1"),
    )
    result = evaluate_momentum_plan(
        snapshots, derived, OrderFlowThresholds(), MomentumThresholds(),
    )
    condition = _condition(result, "momentum_progress")
    assert condition.passed is False
    assert "no momentum to join" in condition.message


def test_entering_at_the_extreme_is_rejected_as_chasing() -> None:
    snapshots = _snapshots([min(12, i // 10) for i in range(150)])
    derived = derive_momentum_context(
        snapshots, TradeDirection.LONG, _StubTracker(), OrderFlowThresholds(),
        MomentumThresholds(), stop_buffer_points=Decimal("1"),
    )
    assert derived.pullback_ticks == Decimal("0")
    result = evaluate_momentum_plan(
        snapshots, derived, OrderFlowThresholds(), MomentumThresholds(),
    )
    condition = _condition(result, "momentum_pullback")
    assert condition.passed is False
    assert "chasing" in condition.message


def test_a_deep_pullback_is_rejected() -> None:
    snapshots = _snapshots([min(12, i // 10) for i in range(130)] + [3] * 20)
    derived = derive_momentum_context(
        snapshots, TradeDirection.LONG, _StubTracker(), OrderFlowThresholds(),
        MomentumThresholds(), stop_buffer_points=Decimal("1"),
    )
    assert derived.pullback_ticks == Decimal("9")
    result = evaluate_momentum_plan(
        snapshots, derived, OrderFlowThresholds(), MomentumThresholds(),
    )
    assert _condition(result, "momentum_pullback").passed is False


# -- engine integration ---------------------------------------------------------------


def _stream(engine: object, count: int) -> None:
    price = Decimal("29500.00")
    for i in range(count):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        ts = 1_752_537_751_000_000_000 + i * 500_000_000
        engine.on_market_event({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",  # type: ignore[attr-defined]
                                "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                                "previous_size": "0", "new_size": str(i % 40 + 1)})
        engine.on_market_event({"timestamp_ns": ts + 1, "price": f"{price:.2f}", "size": "1",  # type: ignore[attr-defined]
                                "aggressor_side": "buy" if i % 2 else "sell",
                                "instrument": "MNQ", "sequence_id": i + 1})


def test_momentum_is_off_by_default_and_reported_as_disabled() -> None:
    from app.paper.streaming_engine import DelayedPaperEngine

    engine = DelayedPaperEngine()
    engine.bind_session("s", "MNQ")
    _stream(engine, 300)
    status = engine.status()
    assert status.momentum_enabled is False
    assert any(name == "momentum-v1" for name, _ in status.disabled_setups)
    assert all(not r.strategy_version.startswith("momentum")
               for r in engine.recent_evaluations(100))
    assert all(not name.startswith("momentum_")
               for name, _p, _f, _m in engine.condition_stats())


def test_momentum_evaluates_beside_the_canonical_plan_when_enabled() -> None:
    from app.paper.streaming_engine import DelayedPaperEngine

    engine = DelayedPaperEngine(config=EpisodeConfig(momentum_enabled=True))
    engine.bind_session("s", "MNQ")
    _stream(engine, 300)
    status = engine.status()
    assert status.momentum_enabled is True
    assert not any(name == "momentum-v1" for name, _ in status.disabled_setups)
    versions = {r.strategy_version for r in engine.recent_evaluations(100)}
    assert MOMENTUM_STRATEGY_VERSION in versions, "momentum must be evaluated"
    assert any(not v.startswith("momentum") for v in versions), (
        "the canonical plan must STILL be evaluated - beside, never instead"
    )
    assert any(name.startswith("momentum_")
               for name, _p, _f, _m in engine.condition_stats())


def test_research_default_stays_canonical() -> None:
    assert EpisodeConfig().momentum_enabled is False


def test_config_reader_fails_closed(tmp_path: Path) -> None:
    from app.paper.options import read_momentum_enabled

    missing = tmp_path / "nope.yaml"
    assert read_momentum_enabled(missing) is False
    on = tmp_path / "on.yaml"
    on.write_text("paper_momentum_setup_enabled: true\n", encoding="utf-8")
    assert read_momentum_enabled(on) is True
    off = tmp_path / "off.yaml"
    off.write_text("paper_momentum_setup_enabled: false\n", encoding="utf-8")
    assert read_momentum_enabled(off) is False
    garbage = tmp_path / "garbage.yaml"
    garbage.write_text("{{{ not yaml", encoding="utf-8")
    assert read_momentum_enabled(garbage) is False
    stringy = tmp_path / "stringy.yaml"
    stringy.write_text('paper_momentum_setup_enabled: "true"\n', encoding="utf-8")
    assert read_momentum_enabled(stringy) is False, "only a real boolean true enables it"


def test_both_launchers_wire_the_momentum_option() -> None:
    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        source = Path(launcher).read_text(encoding="utf-8")
        assert "read_momentum_enabled" in source, launcher

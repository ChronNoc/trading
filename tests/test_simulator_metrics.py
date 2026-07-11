"""Tests for simulator performance metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.simulator.metrics import (
    CompletedSimulatedTrade,
    TradingDayBoundary,
    compute_simulator_metrics,
    split_trades_by_trading_day,
)


def test_compute_simulator_metrics_for_completed_trades() -> None:
    """Completed trades produce P&L, drawdown, excursion, and grouped metrics."""
    trades = _sample_trades()

    metrics = compute_simulator_metrics(trades)

    assert metrics.trade_count == 4
    assert metrics.net_profit_after_costs == Decimal("5")
    assert metrics.average_win == Decimal("25.5")
    assert metrics.average_loss == Decimal("-23")
    assert metrics.profit_factor == Decimal("51") / Decimal("46")
    assert metrics.max_drawdown == Decimal("46")
    assert metrics.max_consecutive_losses == 2
    assert metrics.mae == Decimal("6")
    assert metrics.mfe == Decimal("18")
    assert metrics.slippage_sensitivity.current_slippage_cost == Decimal("5")
    assert metrics.slippage_sensitivity.net_profit_without_slippage == Decimal("10")
    assert metrics.slippage_sensitivity.net_profit_with_double_slippage == Decimal("0")
    assert metrics.performance_by_hour[9].trade_count == 2
    assert metrics.performance_by_hour[9].net_profit == Decimal("-6")
    assert metrics.performance_by_hour[10].net_profit == Decimal("-23")
    assert metrics.performance_by_hour[11].net_profit == Decimal("34")
    assert metrics.performance_by_direction["long"].net_profit == Decimal("-6")
    assert metrics.performance_by_direction["short"].net_profit == Decimal("11")


def test_compute_simulator_metrics_handles_empty_trade_list() -> None:
    """An empty trade list returns neutral metrics."""
    metrics = compute_simulator_metrics(())

    assert metrics.trade_count == 0
    assert metrics.net_profit_after_costs == Decimal("0")
    assert metrics.average_win is None
    assert metrics.average_loss is None
    assert metrics.profit_factor == Decimal("0")
    assert metrics.max_drawdown == Decimal("0")
    assert metrics.max_consecutive_losses == 0
    assert metrics.mae == Decimal("0")
    assert metrics.mfe == Decimal("0")
    assert metrics.performance_by_hour == {}
    assert metrics.performance_by_direction == {}


def test_split_trades_by_trading_day_uses_whole_day_boundaries() -> None:
    """Train, validation, and test splits are assigned by whole trading day."""
    trades = _sample_trades()

    splits = split_trades_by_trading_day(
        trades,
        train_end_timestamp_ns=_ns(2026, 7, 2, 0),
        validation_end_timestamp_ns=_ns(2026, 7, 3, 0),
    )

    assert [trade.trade_id for trade in splits.train] == ["trade-1", "trade-2"]
    assert [trade.trade_id for trade in splits.validation] == ["trade-3"]
    assert [trade.trade_id for trade in splits.test] == ["trade-4"]


def test_split_trades_by_trading_day_supports_custom_day_boundary() -> None:
    """Custom day starts assign pre-boundary trades to the previous trading day."""
    trades = (
        _trade("pre-boundary", "long", _ns(2026, 7, 2, 16), _ns(2026, 7, 2, 16, 30), "100", "101"),
        _trade("post-boundary", "long", _ns(2026, 7, 2, 18), _ns(2026, 7, 2, 18, 30), "100", "101"),
    )

    splits = split_trades_by_trading_day(
        trades,
        train_end_timestamp_ns=_ns(2026, 7, 2, 17),
        validation_end_timestamp_ns=_ns(2026, 7, 3, 17),
        day_boundary=TradingDayBoundary(hour=17),
    )

    assert [trade.trade_id for trade in splits.train] == ["pre-boundary"]
    assert [trade.trade_id for trade in splits.validation] == ["post-boundary"]
    assert splits.test == ()


def test_split_trades_by_trading_day_refuses_midday_split() -> None:
    """Split timestamps must be exactly on the configured trading-day boundary."""
    with pytest.raises(ValueError, match="trading-day boundary"):
        split_trades_by_trading_day(
            _sample_trades(),
            train_end_timestamp_ns=_ns(2026, 7, 2, 12),
            validation_end_timestamp_ns=_ns(2026, 7, 3, 0),
        )


def test_compute_simulator_metrics_rejects_invalid_completed_trade() -> None:
    """Invalid completed-trade values are rejected before metrics are computed."""
    bad_trade = _trade(
        "bad",
        "long",
        _ns(2026, 7, 1, 9),
        _ns(2026, 7, 1, 10),
        "100",
        "101",
        quantity=0,
    )

    with pytest.raises(ValueError, match="quantity"):
        compute_simulator_metrics((bad_trade,))


def _sample_trades() -> tuple[CompletedSimulatedTrade, ...]:
    return (
        _trade(
            "trade-1",
            "long",
            _ns(2026, 7, 1, 9),
            _ns(2026, 7, 1, 9, 30),
            "100",
            "101",
            commission="2",
            slippage_cost="1",
            mae="4",
            mfe="22",
        ),
        _trade(
            "trade-2",
            "long",
            _ns(2026, 7, 1, 10),
            _ns(2026, 7, 1, 10, 30),
            "101",
            "100",
            commission="2",
            slippage_cost="1",
            mae="8",
            mfe="5",
        ),
        _trade(
            "trade-3",
            "short",
            _ns(2026, 7, 2, 9),
            _ns(2026, 7, 2, 9, 30),
            "99",
            "100",
            commission="2",
            slippage_cost="1",
            mae="10",
            mfe="6",
        ),
        _trade(
            "trade-4",
            "short",
            _ns(2026, 7, 3, 11),
            _ns(2026, 7, 3, 11, 30),
            "100",
            "99",
            quantity=2,
            commission="4",
            slippage_cost="2",
            mae="2",
            mfe="39",
        ),
    )


def _trade(
    trade_id: str,
    direction: str,
    entry_timestamp_ns: int,
    exit_timestamp_ns: int,
    entry_price: str,
    exit_price: str,
    *,
    quantity: int = 1,
    commission: str = "0",
    exchange_fees: str = "0",
    slippage_cost: str = "0",
    mae: str = "0",
    mfe: str = "0",
) -> CompletedSimulatedTrade:
    return CompletedSimulatedTrade(
        trade_id=trade_id,
        symbol="MNQ",
        direction=direction,  # type: ignore[arg-type]
        quantity=quantity,
        entry_timestamp_ns=entry_timestamp_ns,
        exit_timestamp_ns=exit_timestamp_ns,
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        point_value=Decimal("20"),
        commission=Decimal(commission),
        exchange_fees=Decimal(exchange_fees),
        slippage_cost=Decimal(slippage_cost),
        max_adverse_excursion=Decimal(mae),
        max_favorable_excursion=Decimal(mfe),
    )


def _ns(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int = 0,
) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()
    return int(timestamp * 1_000_000_000)

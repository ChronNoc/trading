"""Performance metrics for completed simulated trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Sequence, TypeAlias

TradeDirection: TypeAlias = Literal["long", "short"]
NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class CompletedSimulatedTrade:
    """A completed simulated trade with enough data for performance metrics."""

    trade_id: str
    symbol: str
    direction: TradeDirection
    quantity: int
    entry_timestamp_ns: int
    exit_timestamp_ns: int
    entry_price: Decimal
    exit_price: Decimal
    point_value: Decimal
    commission: Decimal = Decimal("0")
    exchange_fees: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    max_adverse_excursion: Decimal = Decimal("0")
    max_favorable_excursion: Decimal = Decimal("0")

    @property
    def gross_pnl(self) -> Decimal:
        """Return gross P&L before costs."""
        if self.direction == "long":
            price_delta = self.exit_price - self.entry_price
        elif self.direction == "short":
            price_delta = self.entry_price - self.exit_price
        else:
            raise ValueError("direction must be long or short")

        return price_delta * Decimal(self.quantity) * self.point_value

    @property
    def total_costs(self) -> Decimal:
        """Return total costs applied to the trade."""
        return self.commission + self.exchange_fees + self.slippage_cost

    @property
    def net_pnl(self) -> Decimal:
        """Return net P&L after costs."""
        return self.gross_pnl - self.total_costs


@dataclass(frozen=True, slots=True)
class PerformanceBucket:
    """Aggregated performance for one group of trades."""

    trade_count: int
    net_profit: Decimal
    average_win: Decimal | None
    average_loss: Decimal | None


@dataclass(frozen=True, slots=True)
class SlippageSensitivity:
    """Sensitivity of net profit to the recorded slippage costs."""

    current_slippage_cost: Decimal
    net_profit_without_slippage: Decimal
    net_profit_with_double_slippage: Decimal
    profit_change_per_slippage_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class SimulatorMetrics:
    """Performance metrics for a completed simulated trade list."""

    trade_count: int
    net_profit_after_costs: Decimal
    average_win: Decimal | None
    average_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal
    max_consecutive_losses: int
    mae: Decimal
    mfe: Decimal
    slippage_sensitivity: SlippageSensitivity
    performance_by_hour: dict[int, PerformanceBucket]
    performance_by_direction: dict[TradeDirection, PerformanceBucket]


@dataclass(frozen=True, slots=True)
class TradingDayBoundary:
    """UTC time-of-day that starts a trading day."""

    hour: int = 0
    minute: int = 0
    second: int = 0


@dataclass(frozen=True, slots=True)
class TradeSplits:
    """Train, validation, and test splits grouped by whole trading days."""

    train: tuple[CompletedSimulatedTrade, ...]
    validation: tuple[CompletedSimulatedTrade, ...]
    test: tuple[CompletedSimulatedTrade, ...]


def compute_simulator_metrics(trades: Sequence[CompletedSimulatedTrade]) -> SimulatorMetrics:
    """Compute performance metrics from completed simulated trades."""
    completed_trades = tuple(_validate_trade(trade) for trade in trades)
    net_pnls = tuple(trade.net_pnl for trade in completed_trades)
    winning_pnls = tuple(pnl for pnl in net_pnls if pnl > Decimal("0"))
    losing_pnls = tuple(pnl for pnl in net_pnls if pnl < Decimal("0"))

    net_profit = sum(net_pnls, Decimal("0"))
    total_wins = sum(winning_pnls, Decimal("0"))
    total_losses = sum(losing_pnls, Decimal("0"))
    total_slippage_cost = sum((trade.slippage_cost for trade in completed_trades), Decimal("0"))

    return SimulatorMetrics(
        trade_count=len(completed_trades),
        net_profit_after_costs=net_profit,
        average_win=_average(winning_pnls),
        average_loss=_average(losing_pnls),
        profit_factor=_profit_factor(total_wins, total_losses),
        max_drawdown=_max_drawdown(completed_trades),
        max_consecutive_losses=_max_consecutive_losses(completed_trades),
        mae=_average(
            tuple(trade.max_adverse_excursion for trade in completed_trades),
        )
        or Decimal("0"),
        mfe=_average(
            tuple(trade.max_favorable_excursion for trade in completed_trades),
        )
        or Decimal("0"),
        slippage_sensitivity=SlippageSensitivity(
            current_slippage_cost=total_slippage_cost,
            net_profit_without_slippage=net_profit + total_slippage_cost,
            net_profit_with_double_slippage=net_profit - total_slippage_cost,
            profit_change_per_slippage_multiplier=-total_slippage_cost,
        ),
        performance_by_hour=_performance_by_hour(completed_trades),
        performance_by_direction=_performance_by_direction(completed_trades),
    )


def split_trades_by_trading_day(
    trades: Sequence[CompletedSimulatedTrade],
    *,
    train_end_timestamp_ns: int,
    validation_end_timestamp_ns: int,
    day_boundary: TradingDayBoundary = TradingDayBoundary(),
) -> TradeSplits:
    """Split trades into train, validation, and test sets by whole trading day."""
    _validate_day_boundary(day_boundary)
    if train_end_timestamp_ns >= validation_end_timestamp_ns:
        raise ValueError("train_end_timestamp_ns must be before validation_end_timestamp_ns")
    if not _is_boundary_timestamp(train_end_timestamp_ns, day_boundary):
        raise ValueError("train split timestamp must be exactly on a trading-day boundary")
    if not _is_boundary_timestamp(validation_end_timestamp_ns, day_boundary):
        raise ValueError("validation split timestamp must be exactly on a trading-day boundary")

    train_end_day = _trading_day_for_timestamp(train_end_timestamp_ns, day_boundary)
    validation_end_day = _trading_day_for_timestamp(validation_end_timestamp_ns, day_boundary)
    train: list[CompletedSimulatedTrade] = []
    validation: list[CompletedSimulatedTrade] = []
    test: list[CompletedSimulatedTrade] = []

    for trade in sorted((_validate_trade(trade) for trade in trades), key=lambda item: item.entry_timestamp_ns):
        trade_day = _trading_day_for_timestamp(trade.entry_timestamp_ns, day_boundary)
        if trade_day < train_end_day:
            train.append(trade)
        elif trade_day < validation_end_day:
            validation.append(trade)
        else:
            test.append(trade)

    return TradeSplits(
        train=tuple(train),
        validation=tuple(validation),
        test=tuple(test),
    )


def _performance_by_hour(trades: tuple[CompletedSimulatedTrade, ...]) -> dict[int, PerformanceBucket]:
    grouped: dict[int, list[CompletedSimulatedTrade]] = {}
    for trade in trades:
        hour = _datetime_from_ns(trade.entry_timestamp_ns).hour
        grouped.setdefault(hour, []).append(trade)

    return {
        hour: _bucket_for_trades(tuple(hour_trades))
        for hour, hour_trades in sorted(grouped.items())
    }


def _performance_by_direction(
    trades: tuple[CompletedSimulatedTrade, ...],
) -> dict[TradeDirection, PerformanceBucket]:
    grouped: dict[TradeDirection, list[CompletedSimulatedTrade]] = {"long": [], "short": []}
    for trade in trades:
        grouped[trade.direction].append(trade)

    return {
        direction: _bucket_for_trades(tuple(direction_trades))
        for direction, direction_trades in grouped.items()
        if direction_trades
    }


def _bucket_for_trades(trades: tuple[CompletedSimulatedTrade, ...]) -> PerformanceBucket:
    pnls = tuple(trade.net_pnl for trade in trades)
    wins = tuple(pnl for pnl in pnls if pnl > Decimal("0"))
    losses = tuple(pnl for pnl in pnls if pnl < Decimal("0"))
    return PerformanceBucket(
        trade_count=len(trades),
        net_profit=sum(pnls, Decimal("0")),
        average_win=_average(wins),
        average_loss=_average(losses),
    )


def _max_drawdown(trades: tuple[CompletedSimulatedTrade, ...]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for trade in sorted(trades, key=lambda item: item.exit_timestamp_ns):
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    return max_drawdown


def _max_consecutive_losses(trades: tuple[CompletedSimulatedTrade, ...]) -> int:
    current = 0
    maximum = 0
    for trade in sorted(trades, key=lambda item: item.exit_timestamp_ns):
        if trade.net_pnl < Decimal("0"):
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0

    return maximum


def _profit_factor(total_wins: Decimal, total_losses: Decimal) -> Decimal | None:
    if total_losses == Decimal("0"):
        return None if total_wins > Decimal("0") else Decimal("0")

    return total_wins / abs(total_losses)


def _average(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _validate_trade(trade: CompletedSimulatedTrade) -> CompletedSimulatedTrade:
    if not trade.trade_id:
        raise ValueError("trade_id is required")
    if trade.direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    if trade.quantity <= 0:
        raise ValueError("quantity must be greater than 0")
    if trade.entry_timestamp_ns < 0 or trade.exit_timestamp_ns < 0:
        raise ValueError("timestamps must be non-negative")
    if trade.exit_timestamp_ns < trade.entry_timestamp_ns:
        raise ValueError("exit_timestamp_ns must be at or after entry_timestamp_ns")
    for field_name, value in (
        ("entry_price", trade.entry_price),
        ("exit_price", trade.exit_price),
        ("point_value", trade.point_value),
    ):
        _require_positive(field_name, value)
    for field_name, value in (
        ("commission", trade.commission),
        ("exchange_fees", trade.exchange_fees),
        ("slippage_cost", trade.slippage_cost),
        ("max_adverse_excursion", trade.max_adverse_excursion),
        ("max_favorable_excursion", trade.max_favorable_excursion),
    ):
        _require_non_negative(field_name, value)

    return trade


def _validate_day_boundary(day_boundary: TradingDayBoundary) -> None:
    if day_boundary.hour < 0 or day_boundary.hour > 23:
        raise ValueError("day boundary hour must be between 0 and 23")
    if day_boundary.minute < 0 or day_boundary.minute > 59:
        raise ValueError("day boundary minute must be between 0 and 59")
    if day_boundary.second < 0 or day_boundary.second > 59:
        raise ValueError("day boundary second must be between 0 and 59")


def _is_boundary_timestamp(timestamp_ns: int, day_boundary: TradingDayBoundary) -> bool:
    timestamp = _datetime_from_ns(timestamp_ns)
    return (
        timestamp.hour == day_boundary.hour
        and timestamp.minute == day_boundary.minute
        and timestamp.second == day_boundary.second
        and timestamp.microsecond == 0
        and timestamp_ns % NANOSECONDS_PER_SECOND == 0
    )


def _trading_day_for_timestamp(timestamp_ns: int, day_boundary: TradingDayBoundary) -> date:
    timestamp = _datetime_from_ns(timestamp_ns)
    boundary_today = timestamp.replace(
        hour=day_boundary.hour,
        minute=day_boundary.minute,
        second=day_boundary.second,
        microsecond=0,
    )
    if timestamp < boundary_today:
        return (timestamp - timedelta(days=1)).date()
    return timestamp.date()


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    if timestamp_ns < 0:
        raise ValueError("timestamp_ns must be non-negative")
    seconds, nanoseconds = divmod(timestamp_ns, NANOSECONDS_PER_SECOND)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=nanoseconds // 1_000,
    )


def _require_non_negative(name: str, value: Decimal) -> None:
    if value < Decimal("0"):
        raise ValueError(f"{name} must be non-negative")


def _require_positive(name: str, value: Decimal) -> None:
    if value <= Decimal("0"):
        raise ValueError(f"{name} must be greater than 0")

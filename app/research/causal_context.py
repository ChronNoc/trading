"""Causal strategy context derived only from market data already observed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Sequence
from zoneinfo import ZoneInfo

from app.market.state import MarketState
from app.strategy.order_flow import (
    BookSide,
    DirectionOfLiquidity,
    LiquidityBlock,
    OrderFlowPlanContext,
    OrderFlowThresholds,
    StrategyLevels,
    TradeDirection,
    detect_liquidity_blocks,
    find_direction_of_liquidity,
)

NEW_YORK = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
OVERNIGHT_START = time(18, 0)


@dataclass(slots=True)
class CausalLevelTracker:
    """Track prior-day and overnight levels without looking into the future."""

    _rth_levels: dict[date, tuple[Decimal, Decimal]] = field(default_factory=dict)
    _overnight_trading_day: date | None = None
    _overnight_high: Decimal | None = None
    _overnight_low: Decimal | None = None

    def observe(self, timestamp_ns: int, price: Decimal | None) -> None:
        """Apply one already-observed reference price to causal session levels."""
        if price is None:
            return
        local = _datetime_from_ns(timestamp_ns).astimezone(NEW_YORK)
        trading_day = _trading_day(local)
        if trading_day != self._overnight_trading_day:
            self._overnight_trading_day = trading_day
            self._overnight_high = None
            self._overnight_low = None

        if local.timetz().replace(tzinfo=None) >= OVERNIGHT_START or local.timetz().replace(tzinfo=None) < RTH_OPEN:
            self._overnight_high = price if self._overnight_high is None else max(self._overnight_high, price)
            self._overnight_low = price if self._overnight_low is None else min(self._overnight_low, price)

        local_time = local.timetz().replace(tzinfo=None)
        if RTH_OPEN <= local_time < RTH_CLOSE:
            prior = self._rth_levels.get(local.date())
            if prior is None:
                self._rth_levels[local.date()] = (price, price)
            else:
                self._rth_levels[local.date()] = (max(prior[0], price), min(prior[1], price))
            self._prune_rth_levels(local.date())

    def levels_at(self, timestamp_ns: int) -> StrategyLevels:
        """Return only higher-timeframe levels known by ``timestamp_ns``."""
        local = _datetime_from_ns(timestamp_ns).astimezone(NEW_YORK)
        trading_day = _trading_day(local)
        prior_dates = [session_date for session_date in self._rth_levels if session_date < trading_day]
        prior = self._rth_levels[max(prior_dates)] if prior_dates else (None, None)
        overnight_current = self._overnight_trading_day == trading_day
        return StrategyLevels(
            prior_day_high=prior[0],
            prior_day_low=prior[1],
            overnight_high=self._overnight_high if overnight_current else None,
            overnight_low=self._overnight_low if overnight_current else None,
            manual_levels=(),
            psychological_interval=Decimal("100"),
        )

    def session_open_timestamp_ns(self, timestamp_ns: int) -> int:
        """Return the New York RTH open for the event's trading day."""
        local = _datetime_from_ns(timestamp_ns).astimezone(NEW_YORK)
        trading_day = _trading_day(local)
        opening = datetime.combine(trading_day, RTH_OPEN, tzinfo=NEW_YORK)
        return _timestamp_ns(opening.astimezone(UTC))

    def _prune_rth_levels(self, current_date: date) -> None:
        cutoff = current_date - timedelta(days=10)
        self._rth_levels = {
            session_date: levels
            for session_date, levels in self._rth_levels.items()
            if session_date >= cutoff
        }


@dataclass(frozen=True, slots=True)
class DerivedContext:
    """Strategy context plus the observed evidence used to construct it."""

    context: OrderFlowPlanContext
    defending_block: LiquidityBlock | None
    direction_of_liquidity: DirectionOfLiquidity | None

    def evidence(self) -> dict[str, object]:
        """Return JSON-compatible context lineage for a decision record."""
        levels = self.context.levels
        block = self.defending_block
        dol = self.direction_of_liquidity
        return {
            "prior_day_high": _decimal_text(levels.prior_day_high),
            "prior_day_low": _decimal_text(levels.prior_day_low),
            "overnight_high": _decimal_text(levels.overnight_high),
            "overnight_low": _decimal_text(levels.overnight_low),
            "psychological_interval": _decimal_text(levels.psychological_interval),
            "defended_level_price": _decimal_text(self.context.defended_level_price),
            "stop_price": _decimal_text(self.context.stop_price),
            "target_price": _decimal_text(self.context.target_price),
            "session_open_timestamp_ns": self.context.session_open_timestamp_ns,
            "defending_block": (
                None
                if block is None
                else {
                    "side": block.side.value,
                    "price": str(block.price),
                    "peak_size": str(block.peak_size),
                    "latest_size": str(block.latest_size),
                    "observations": block.observations,
                    "reload_count": block.reload_count,
                    "vanished_by_end": block.vanished_by_end,
                }
            ),
            "direction_of_liquidity": (
                None
                if dol is None
                else {
                    "target_price": str(dol.target_price),
                    "source": dol.source,
                    "distance_ticks": str(dol.distance_ticks),
                }
            ),
        }


def derive_strategy_context(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    level_tracker: CausalLevelTracker,
    thresholds: OrderFlowThresholds,
    *,
    stop_buffer_points: Decimal,
    news_lockout_active: bool = False,
) -> DerivedContext:
    """Build a complete context from past snapshots and configured stop buffer."""
    if not snapshots:
        raise ValueError("snapshots must not be empty")
    latest = snapshots[-1]
    levels = level_tracker.levels_at(latest.timestamp_ns)
    block = _select_observed_defending_block(snapshots, direction, thresholds)
    defended_price = block.price if block is not None else None
    if defended_price is None:
        stop_price = None
    elif direction == TradeDirection.LONG:
        stop_price = defended_price - stop_buffer_points
    else:
        stop_price = defended_price + stop_buffer_points

    base_context = OrderFlowPlanContext(
        direction=direction,
        levels=levels,
        defended_level_price=defended_price,
        stop_price=stop_price,
        session_open_timestamp_ns=level_tracker.session_open_timestamp_ns(latest.timestamp_ns),
        news_lockout_active=news_lockout_active,
    )
    dol = find_direction_of_liquidity(snapshots, base_context, thresholds)
    context = OrderFlowPlanContext(
        direction=direction,
        levels=levels,
        defended_level_price=defended_price,
        target_price=dol.target_price if dol is not None else None,
        stop_price=stop_price,
        session_open_timestamp_ns=base_context.session_open_timestamp_ns,
        news_lockout_active=news_lockout_active,
    )
    return DerivedContext(context=context, defending_block=block, direction_of_liquidity=dol)


def trading_day_for_timestamp(timestamp_ns: int) -> str:
    """Return the CME-style trading day in New York time."""
    local = _datetime_from_ns(timestamp_ns).astimezone(NEW_YORK)
    return _trading_day(local).isoformat()


def _select_observed_defending_block(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> LiquidityBlock | None:
    blocks = detect_liquidity_blocks(snapshots, thresholds)
    side = BookSide.BID if direction == TradeDirection.LONG else BookSide.ASK
    current_price = _reference_price(snapshots[-1])
    candidates = tuple(
        block
        for block in blocks
        if block.side == side
        and block.is_durable(thresholds)
        and not block.vanished_by_end
        and current_price is not None
        and (
            direction == TradeDirection.LONG and block.price <= current_price
            or direction == TradeDirection.SHORT and block.price >= current_price
        )
    )
    if not candidates:
        return None
    return max(candidates, key=lambda block: (block.latest_size, block.observations, block.peak_size))


def _reference_price(state: MarketState) -> Decimal | None:
    if state.mid_price is not None:
        return state.mid_price
    if state.best_bid is not None:
        return state.best_bid
    return state.best_ask


def _trading_day(local: datetime) -> date:
    if local.timetz().replace(tzinfo=None) >= OVERNIGHT_START:
        return local.date() + timedelta(days=1)
    return local.date()


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1_000)


def _timestamp_ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000 + value.microsecond * 1_000


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)

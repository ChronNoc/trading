"""Causal simulated execution: orders, fills, positions, stops, targets, P&L.

The whole point is that nothing here may see the future. An order created from
event N is only fillable from event **N+1 or later** (``fill_eligible_from``);
the event that produced the signal can never also fill it. Stops and targets are
resolved from events strictly after the position opened.

Every ambiguous situation resolves **against** the trade:

* stop and target both reachable in the same event -> ``AMBIGUOUS``, booked at
  the stop price. We cannot know which printed first, so we never take the win.
* a gap through the stop fills at the *gapped* price, not the stop price - a real
  stop-market does not fill at a price that never traded.
* a crossed/stale/missing book blocks new entries rather than guessing.

No broker module is imported here (enforced by test), so delayed data is
structurally incapable of routing an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.paper.models import (
    MNQ_TICK_SIZE,
    MNQ_TICK_VALUE,
    CloseReason,
    Direction,
    Fill,
    OrderStatus,
    PaperOrder,
    PaperOrderIntent,
    PaperPosition,
    PaperTrade,
    RiskDecision,
    points_to_dollars,
)

# Reason codes are stable and machine-readable; the GUI shows the text.
REASON_NO_STOP = "invalid_stop"
REASON_RISK_ZERO_SIZE = "risk_zero_contracts"
REASON_DAILY_ENTRIES = "daily_entry_lock"
REASON_DAILY_LOSSES = "daily_loss_lock"
REASON_DRAWDOWN = "drawdown_lock"
REASON_POSITION_OPEN = "one_position_max"
REASON_COOLDOWN = "setup_cooldown"
REASON_DUPLICATE = "duplicate_setup_occurrence"
REASON_STALE_BOOK = "stale_or_crossed_book"
REASON_CONTRACT_UNRESOLVED = "contract_unresolved"
REASON_APPROVED = "approved"
REASON_STOP_TOO_WIDE = "stop_exceeds_fixed_risk_cap"
REASON_POOR_REWARD_RISK = "reward_risk_below_minimum"
REASON_LIMIT_UNFILLED = "limit_entry_unfilled_cancelled"


def align_to_tick(price: Decimal, *, round_up: bool) -> Decimal:
    """Snap a price to the MNQ 0.25 grid.

    MNQ trades only in 0.25 increments, so a fill at 29500.875 is fiction: no
    such price exists. Callers pass the ADVERSE direction (up when buying, down
    when selling) so rounding can never invent a better price than reality.
    """
    ticks = price / MNQ_TICK_SIZE
    rounded = ticks.to_integral_value(rounding=ROUND_CEILING if round_up else ROUND_FLOOR)
    return rounded * MNQ_TICK_SIZE


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Explicit, inspectable simulation assumptions."""

    commission_per_contract: Decimal = Decimal("1.24")   # round turn
    entry_slippage_ticks: Decimal = Decimal("1")         # adverse, always
    stop_slippage_ticks: Decimal = Decimal("1")          # adverse, always
    cooldown_ns: int = 300 * 1_000_000_000               # 5 min between entries
    time_stop_ns: int = 900 * 1_000_000_000              # 15 min
    max_entries_per_day: int = 3
    max_losses_per_day: int = 3
    # --- dynamic stop management (all 0 = disabled) ---------------------------
    # Break-even: once price has moved this many ticks in favour, move the stop
    # to entry +/- lock ticks. After it triggers the trade cannot lose (beyond
    # the small lock offset) - genuinely less risk on trades that work.
    break_even_trigger_ticks: Decimal = Decimal("0")
    break_even_lock_ticks: Decimal = Decimal("0")
    # Trailing: once price has moved this many ticks in favour, trail the stop
    # this many ticks behind the best price seen. Locks in more as it runs.
    trail_activation_ticks: Decimal = Decimal("0")
    trail_distance_ticks: Decimal = Decimal("0")
    # Fixed-size PAPER sizing (0 = disabled -> the original dynamic 1%-account/
    # 3-trades risk sizing in size_intent() is unchanged). A PER-TRADE risk
    # cap, not a daily loss cap. See EpisodeConfig.fixed_contracts and
    # config/production_config.yaml's paper_fixed_contracts /
    # paper_max_risk_per_trade_usd for the full explanation.
    fixed_contracts: int = 0
    max_risk_per_trade_usd: Decimal = Decimal("0")
    # Minimum reward:risk a setup must offer to be taken (0 = disabled). The
    # strategy's real target/stop are never altered - a setup below this bar is
    # simply skipped, so the engine stops taking trades whose target is too close
    # to the stop to be worth the risk after costs.
    min_reward_risk: Decimal = Decimal("0")
    # --- entry order type (market taker vs passive limit maker) ----------------
    # "market" (default): crosses the spread and pays it PLUS entry_slippage_ticks
    # - a liquidity-taking entry, filled immediately on the next causal event.
    # "limit": rests a passive maker order at the near touch (joining the bid to
    # buy / the ask to sell), fills AT that price when the market trades to it -
    # no spread paid, no adverse entry slippage - and is CANCELLED unfilled if the
    # price runs away or the timeout elapses, so a missed scalp is never counted
    # as a trade. This mirrors live limit-scalping on Tradovate, where entries are
    # passive; it turns the ~1.5-tick taker entry cost into a maker fill, which is
    # decisive for scalps whose target is only a few ticks.
    entry_order_type: str = "market"
    # How far behind the near touch the resting limit sits, in ticks (more
    # passive = better price, fewer fills). 0 joins the touch; buy = bid - offset,
    # sell = ask + offset.
    entry_limit_offset_ticks: Decimal = Decimal("0")
    # Cancel a resting entry this long after placement (0 = no time cap).
    entry_limit_timeout_ns: int = 0
    # Cancel a resting entry once the market has moved this many ticks AWAY from
    # the limit - the setup ran without us (0 = no distance cap).
    entry_limit_cancel_ticks: Decimal = Decimal("0")
    # Require the market to trade strictly THROUGH the limit (not merely reach it)
    # before filling - the most conservative queue-position assumption. Default
    # False fills when the opposite touch reaches the limit price.
    entry_require_trade_through: bool = False

    def __post_init__(self) -> None:
        """Validate assumptions."""
        if not self.min_reward_risk.is_finite() or self.min_reward_risk < 0:
            raise ValueError("min_reward_risk must be a finite, non-negative ratio")
        if self.commission_per_contract < 0:
            raise ValueError("commission must be non-negative")
        if self.entry_slippage_ticks < 0 or self.stop_slippage_ticks < 0:
            raise ValueError("slippage must be non-negative")
        for name in ("break_even_trigger_ticks", "break_even_lock_ticks",
                     "trail_activation_ticks", "trail_distance_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.trail_activation_ticks > 0 and self.trail_distance_ticks <= 0:
            raise ValueError("trail_distance_ticks must be positive when trailing is armed")
        if self.fixed_contracts < 0:
            raise ValueError("fixed_contracts must be non-negative")
        if not self.max_risk_per_trade_usd.is_finite():
            raise ValueError("max_risk_per_trade_usd must be finite")
        if self.max_risk_per_trade_usd < 0:
            raise ValueError("max_risk_per_trade_usd must be non-negative")
        if self.fixed_contracts > 0 and self.max_risk_per_trade_usd <= 0:
            raise ValueError(
                "max_risk_per_trade_usd must be positive when fixed_contracts is set",
            )
        if self.entry_order_type not in {"market", "limit"}:
            raise ValueError("entry_order_type must be 'market' or 'limit'")
        for name in ("entry_limit_offset_ticks", "entry_limit_cancel_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.entry_limit_timeout_ns < 0:
            raise ValueError("entry_limit_timeout_ns must be non-negative")
        if (self.entry_order_type == "limit"
                and self.entry_limit_timeout_ns <= 0
                and self.entry_limit_cancel_ticks <= 0):
            # Without a cap an unfilled resting order would block every future
            # entry forever (one pending order at a time).
            raise ValueError(
                "limit entries require entry_limit_timeout_ns or "
                "entry_limit_cancel_ticks so an unfilled order cannot block forever")


@dataclass(frozen=True, slots=True)
class MarketTick:
    """The causal view of one market event the executor may act on."""

    event_index: int
    ts_ns: int
    price: Decimal
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None

    @property
    def book_usable(self) -> bool:
        """A crossed or missing book is never traded against."""
        if self.best_bid is None or self.best_ask is None:
            return True  # trade-only tick: price is still authoritative
        return self.best_bid <= self.best_ask


class PaperExecutor:
    """Owns pending orders, the open position, and the closed-trade list."""

    def __init__(
        self,
        *,
        starting_balance: Decimal,
        max_contracts: int,
        config: ExecutionConfig | None = None,
        is_synthetic_fixture: bool = False,
        tick_value: Decimal = MNQ_TICK_VALUE,
    ) -> None:
        """Create an executor for one account."""
        self._config = config or ExecutionConfig()
        self._tick_value = tick_value
        self._balance = starting_balance
        self._starting_balance = starting_balance
        self._max_contracts = max_contracts
        self._synthetic = is_synthetic_fixture
        self.pending: PaperOrder | None = None
        self.position: PaperPosition | None = None
        self.trades: list[PaperTrade] = []
        self.rejections: list[PaperOrder] = []
        # Resting limit entries that expired unfilled (missed scalps). Tracked
        # separately from risk rejections so the honest "we didn't get filled"
        # count never masquerades as a risk block or as a trade.
        self.cancellations: list[PaperOrder] = []
        self._last_entry_ts_ns: int = 0
        self._seen_setup_ids: set[str] = set()
        self._day: str = ""
        self._day_entries = 0
        self._day_losses = 0
        self._locked_out = False
        # Only fillable from an event strictly AFTER the one that created it.
        self._fill_eligible_from: int = 0

    # -- read models ----------------------------------------------------------

    @property
    def balance(self) -> Decimal:
        """Current simulated account balance."""
        return self._balance

    @property
    def realized_pnl(self) -> Decimal:
        """Total realized P&L."""
        return self._balance - self._starting_balance

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        """Open P&L at ``mark``, or zero when flat."""
        return self.position.unrealized_pnl(mark) if self.position else Decimal("0")

    # -- submission -----------------------------------------------------------

    def submit(
        self,
        intent: PaperOrderIntent,
        decision: RiskDecision,
        tick: MarketTick,
        *,
        trading_day: str,
    ) -> PaperOrder:
        """Turn an approved intent into a PENDING order (never fills it here).

        The order records ``fill_eligible_from = tick.event_index + 1``: the
        event that generated the signal can never also fill it.
        """
        self._roll_day(trading_day)
        order = PaperOrder(
            intent=intent, contracts=decision.contracts,
            status=OrderStatus.PENDING if decision.approved else OrderStatus.REJECTED_BY_RISK,
            created_event_index=tick.event_index, created_ts_ns=tick.ts_ns,
            reason_code=decision.reason_code, reason=decision.reason,
        )
        if not decision.approved:
            self.rejections.append(order)
            return order
        if self._config.entry_order_type == "limit":
            # Capture the resting price now, from the touch at signal time. The
            # order fills AT this price when the market trades to it (maker), or
            # is cancelled unfilled - never at a worse, spread-crossing price.
            order.entry_limit_price = self._resting_limit_price(intent.direction, tick)
        self.pending = order
        self._fill_eligible_from = tick.event_index + 1  # strict causality
        self._seen_setup_ids.add(intent.provenance.setup_id)
        return order

    def gate(self, intent: PaperOrderIntent, tick: MarketTick, *, trading_day: str) -> RiskDecision | None:
        """Return a rejection if any pre-risk constraint blocks this entry.

        None means "no structural blocker - ask the risk engine".
        """
        self._roll_day(trading_day)
        if not tick.book_usable:
            return RiskDecision.reject(REASON_STALE_BOOK, "market book is crossed or unusable")
        if not intent.provenance.contract or intent.provenance.contract.lower() in {"unknown", "resolving"}:
            return RiskDecision.reject(REASON_CONTRACT_UNRESOLVED, "MNQ contract is not resolved")
        if self.position is not None or self.pending is not None:
            return RiskDecision.reject(REASON_POSITION_OPEN, "one open paper position at a time")
        if intent.provenance.setup_id in self._seen_setup_ids:
            return RiskDecision.reject(REASON_DUPLICATE, "this setup occurrence already traded")
        if self._locked_out:
            return RiskDecision.reject(REASON_DRAWDOWN, "account risk lockout is active")
        # A cap of 0 means UNLIMITED (learning-stage data collection). The
        # account drawdown limit still applies as a natural floor.
        if (self._config.max_entries_per_day > 0
                and self._day_entries >= self._config.max_entries_per_day):
            return RiskDecision.reject(REASON_DAILY_ENTRIES, "daily entry limit reached")
        if (self._config.max_losses_per_day > 0
                and self._day_losses >= self._config.max_losses_per_day):
            return RiskDecision.reject(REASON_DAILY_LOSSES, "daily losing-trade limit reached")
        if self._last_entry_ts_ns and tick.ts_ns - self._last_entry_ts_ns < self._config.cooldown_ns:
            return RiskDecision.reject(REASON_COOLDOWN, "setup cooldown has not elapsed")
        if intent.risk_points <= 0:
            return RiskDecision.reject(REASON_NO_STOP, "stop distance is not positive")
        return None

    # -- the causal step ------------------------------------------------------

    def on_tick(self, tick: MarketTick) -> PaperTrade | None:
        """Advance one event: maybe fill a pending order, maybe close a position.

        Returns the closed trade when this event closed one.
        """
        if self.pending is not None and tick.event_index >= self._fill_eligible_from:
            if self._config.entry_order_type == "limit":
                self._advance_limit_entry(tick)
            else:
                self._fill_pending(tick)
        if self.position is not None:
            self.position.observe(tick.price)
            return self._manage_position(tick)
        return None

    def _entry_fill_price(self, direction: Direction, tick: MarketTick) -> Decimal:
        """Return a REAL, tick-aligned entry price for a market order.

        A buy market order lifts the offer, a sell hits the bid - it does not
        transact at the mid, which is frequently not even a tradeable price. When
        the book is unavailable the trade price is the best evidence we have.
        Adverse slippage is then added and the result snapped to the tick grid
        away from us, so a fill is never better than the tape could deliver.
        """
        is_long = direction is Direction.LONG
        touch = (tick.best_ask if is_long else tick.best_bid) or tick.price
        slip = self._config.entry_slippage_ticks * MNQ_TICK_SIZE
        raw = touch + slip if is_long else touch - slip
        return align_to_tick(raw, round_up=is_long)

    def _fill_pending(self, tick: MarketTick) -> None:
        """Fill a market (taker) entry immediately, paying the spread + slippage."""
        order = self.pending
        assert order is not None
        fill_price = self._entry_fill_price(order.intent.direction, tick)
        self._open_position(order, fill_price, tick)

    def _resting_limit_price(self, direction: Direction, tick: MarketTick) -> Decimal:
        """Return the tick-aligned price a passive maker entry rests at.

        Joins the near touch (bid to buy, ask to sell) and steps
        ``entry_limit_offset_ticks`` further back into the book. The price is
        snapped to the grid on the PASSIVE side so it is a real, joinable level
        and never better than the touch it joins. Falls back to the tick price on
        a trade-only event that carries no book.
        """
        is_long = direction is Direction.LONG
        touch = (tick.best_bid if is_long else tick.best_ask) or tick.price
        offset = self._config.entry_limit_offset_ticks * MNQ_TICK_SIZE
        raw = touch - offset if is_long else touch + offset
        return align_to_tick(raw, round_up=not is_long)

    def _advance_limit_entry(self, tick: MarketTick) -> None:
        """Fill a resting maker entry when price reaches it, else cancel/hold.

        The fill is AT the resting price (no spread, no adverse entry slippage) -
        the maker benefit. A buy resting at the bid fills when the OFFER reaches
        that price (a seller is now at our level); a sell resting at the ask fills
        when the BID reaches it. Adverse selection is not hidden: we open at the
        limit exactly as price trades to (and often through) it, so a runner that
        continues to the stop is booked as the loss it is. If the market instead
        runs away or the timeout elapses, the order is CANCELLED - a missed scalp,
        never a trade.
        """
        order = self.pending
        assert order is not None and order.entry_limit_price is not None
        limit = order.entry_limit_price
        is_long = order.intent.direction is Direction.LONG
        opposite = (tick.best_ask if is_long else tick.best_bid)
        if opposite is None:
            opposite = tick.price  # trade-only event: the print is the evidence
        if self._config.entry_require_trade_through:
            reached = opposite < limit if is_long else opposite > limit
        else:
            reached = opposite <= limit if is_long else opposite >= limit
        if reached:
            self._open_position(order, limit, tick)
            return
        if self._limit_should_cancel(order, limit, tick):
            order.status = OrderStatus.CANCELLED
            order.reason_code = REASON_LIMIT_UNFILLED
            order.reason = "resting limit entry expired unfilled (missed scalp)"
            self.cancellations.append(order)
            self.pending = None

    def _limit_should_cancel(self, order: PaperOrder, limit: Decimal, tick: MarketTick) -> bool:
        """Return whether a resting entry should be abandoned unfilled."""
        cfg = self._config
        if (cfg.entry_limit_timeout_ns > 0
                and tick.ts_ns - order.created_ts_ns >= cfg.entry_limit_timeout_ns):
            return True
        if cfg.entry_limit_cancel_ticks > 0:
            is_long = order.intent.direction is Direction.LONG
            touch = (tick.best_bid if is_long else tick.best_ask)
            if touch is not None:
                # How far the market has pulled AWAY from our resting price in the
                # direction that leaves us behind (bid rising above a buy, ask
                # falling below a sell) - the setup ran without us.
                away = (touch - limit) if is_long else (limit - touch)
                if away >= cfg.entry_limit_cancel_ticks * MNQ_TICK_SIZE:
                    return True
        return False

    def _open_position(self, order: PaperOrder, fill_price: Decimal, tick: MarketTick) -> None:
        """Turn a filled order into the open position (shared by market/limit)."""
        intent = order.intent
        order.fill = Fill(price=fill_price, quantity=order.contracts,
                          ts_ns=tick.ts_ns, event_index=tick.event_index)
        order.status = OrderStatus.FILLED
        self.position = PaperPosition(
            direction=intent.direction, contracts=order.contracts, entry_price=fill_price,
            stop=intent.stop, target=intent.target, opened_ts_ns=tick.ts_ns,
            opened_event_index=tick.event_index, provenance=intent.provenance,
        )
        self._last_entry_ts_ns = tick.ts_ns
        self._day_entries += 1
        self.pending = None

    def _manage_position(self, tick: MarketTick) -> PaperTrade | None:
        position = self.position
        assert position is not None
        if tick.event_index <= position.opened_event_index:
            return None  # never resolve from the entry event itself
        # Advance the stop FIRST (break-even / trailing) so this same event is
        # judged against the tightened stop. Only ever moves the stop toward
        # price; it can never widen risk.
        self._advance_stop(position)
        is_long = position.direction is Direction.LONG
        hit_stop = tick.price <= position.stop if is_long else tick.price >= position.stop
        hit_target = tick.price >= position.target if is_long else tick.price <= position.target

        if hit_stop and hit_target:
            # Both reachable in one event: order unknowable -> never take the win.
            return self._close(tick, self._stop_fill_price(position, tick), CloseReason.AMBIGUOUS)
        if hit_stop:
            return self._close(tick, self._stop_fill_price(position, tick), CloseReason.STOP)
        if hit_target:
            # A limit target fills at the target, not beyond it.
            return self._close(tick, position.target, CloseReason.TARGET)
        if tick.ts_ns - position.opened_ts_ns >= self._config.time_stop_ns:
            return self._close(tick, self._market_exit_price(position, tick), CloseReason.TIME_STOP)
        return None

    def _advance_stop(self, position: PaperPosition) -> None:
        """Move the stop to break-even / trail it, based on peak favourable move.

        Uses the position's MFE (peak favourable excursion), so it reacts to the
        best price seen, not a transient wick. Never loosens the stop.
        """
        cfg = self._config
        favorable_ticks = position.mfe_points / MNQ_TICK_SIZE
        is_long = position.direction is Direction.LONG
        sign = Decimal("1") if is_long else Decimal("-1")
        new_stop = position.stop

        if (cfg.break_even_trigger_ticks > 0
                and favorable_ticks >= cfg.break_even_trigger_ticks):
            break_even = position.entry_price + sign * cfg.break_even_lock_ticks * MNQ_TICK_SIZE
            new_stop = _tighter_stop(new_stop, break_even, is_long)

        if (cfg.trail_activation_ticks > 0
                and favorable_ticks >= cfg.trail_activation_ticks):
            best_price = position.entry_price + sign * position.mfe_points
            trailed = best_price - sign * cfg.trail_distance_ticks * MNQ_TICK_SIZE
            new_stop = _tighter_stop(new_stop, trailed, is_long)

        if new_stop != position.stop:
            position.stop = new_stop

    def _market_exit_price(self, position: PaperPosition, tick: MarketTick) -> Decimal:
        """Return a real tick-aligned price for a market exit (time stop / flat).

        Exiting a long sells into the bid; exiting a short buys the offer.
        """
        is_long = position.direction is Direction.LONG
        touch = (tick.best_bid if is_long else tick.best_ask) or tick.price
        return align_to_tick(touch, round_up=not is_long)

    def _stop_fill_price(self, position: PaperPosition, tick: MarketTick) -> Decimal:
        """Return the stop fill price, honouring gap-through conservatively.

        A stop-market cannot fill at a price that never traded: if the market
        gapped beyond the stop, the fill is at the worse traded price, plus
        adverse slippage.
        """
        slip = self._config.stop_slippage_ticks * MNQ_TICK_SIZE
        if position.direction is Direction.LONG:
            base = min(position.stop, tick.price)  # gap below the stop fills lower
            return align_to_tick(base - slip, round_up=False)  # selling out: round down
        base = max(position.stop, tick.price)      # gap above the stop fills higher
        return align_to_tick(base + slip, round_up=True)       # buying back: round up

    def _close(self, tick: MarketTick, exit_price: Decimal, reason: CloseReason) -> PaperTrade:
        position = self.position
        assert position is not None
        points = (exit_price - position.entry_price) * position.direction.sign
        gross = points_to_dollars(points, position.contracts, self._tick_value)
        commission = (self._config.commission_per_contract * Decimal(position.contracts)).quantize(Decimal("0.01"))
        slippage_cost = points_to_dollars(
            (self._config.entry_slippage_ticks + self._config.stop_slippage_ticks) * MNQ_TICK_SIZE,
            position.contracts, self._tick_value,
        )
        net = (gross - commission).quantize(Decimal("0.01"))
        risk_dollars = points_to_dollars(
            abs(position.entry_price - position.stop), position.contracts, self._tick_value,
        )
        r_multiple = (net / risk_dollars).quantize(Decimal("0.0001")) if risk_dollars > 0 else Decimal("0")
        self._balance = (self._balance + net).quantize(Decimal("0.01"))
        if net <= 0:
            self._day_losses += 1
        trade = PaperTrade(
            provenance=position.provenance, direction=position.direction,
            contracts=position.contracts, entry_price=position.entry_price,
            exit_price=exit_price, stop=position.stop, target=position.target,
            opened_ts_ns=position.opened_ts_ns, closed_ts_ns=tick.ts_ns,
            opened_event_index=position.opened_event_index, closed_event_index=tick.event_index,
            close_reason=reason, gross_pnl=gross, commission=commission,
            slippage_cost=slippage_cost, net_pnl=net, r_multiple=r_multiple,
            mae_points=position.mae_points, mfe_points=position.mfe_points,
            balance_after=self._balance, is_synthetic_fixture=self._synthetic,
        )
        self.trades.append(trade)
        self.position = None
        return trade

    def liquidate(self, tick: MarketTick, reason: CloseReason = CloseReason.SESSION_CLOSE) -> PaperTrade | None:
        """Flatten any open position (session close / kill switch)."""
        if self.position is None:
            self.pending = None
            return None
        return self._close(tick, self._market_exit_price(self.position, tick), reason)

    def lock_out(self) -> None:
        """Block further entries (account failure / kill switch)."""
        self._locked_out = True

    def _roll_day(self, trading_day: str) -> None:
        if trading_day and trading_day != self._day:
            self._day = trading_day
            self._day_entries = 0
            self._day_losses = 0


def _tighter_stop(current: Decimal, candidate: Decimal, is_long: bool) -> Decimal:
    """Return whichever stop is closer to price in the favourable direction.

    For a long, higher is tighter; for a short, lower is tighter. This makes
    stop advancement one-way: it can only ever reduce open risk, never widen it.
    """
    if is_long:
        return max(current, candidate)
    return min(current, candidate)


def size_intent(
    intent: PaperOrderIntent,
    *,
    balance: Decimal,
    drawdown_room: Decimal,
    max_contracts: int,
    commission_per_contract: Decimal,
    tick_value: Decimal = MNQ_TICK_VALUE,
    fixed_contracts: int = 0,
    max_risk_per_trade_usd: Decimal = Decimal("0"),
    min_reward_risk: Decimal = Decimal("0"),
) -> RiskDecision:
    """Size an intent through the PROJECT risk engine (never bypassed).

    Reuses ``app.risk.sizing.calculate_position_size`` - the same sizing the
    offline ledger uses - then applies the account's hard contract cap.

    When ``fixed_contracts > 0`` (the paper-engine's fixed-size mode), sizing
    uses ``fixed_contracts`` as a *ceiling* instead of the dynamic 1%-account/
    3-trades budget below. The strategy's real, evidence-derived stop
    (``intent.stop_distance_ticks``) is never replaced or invented. If that
    real stop would risk more than ``max_risk_per_trade_usd`` at the full
    ceiling, the position is sized DOWN to the largest contract count that
    fits the cap (the cap is never relaxed, the stop is never faked). Only if a
    single contract already exceeds the cap is the stop genuinely too wide, and
    the trade is rejected outright.
    """
    from app.risk.sizing import SizingInputs, calculate_position_size
    from app.risk.sizing import calculate_contracts, calculate_risk_per_contract

    if intent.stop_distance_ticks <= 0:
        return RiskDecision.reject(REASON_NO_STOP, "stop distance is not positive")

    # Reward:risk quality gate. The strategy's real target/stop are used as-is; a
    # setup whose reward is too small relative to its risk is skipped before any
    # sizing, so the engine stops taking tiny-target trades that costs erase.
    if min_reward_risk > 0:
        reward_risk = intent.reward_points / intent.risk_points  # risk_points > 0 above
        if reward_risk < min_reward_risk:
            return RiskDecision.reject(
                REASON_POOR_REWARD_RISK,
                f"reward:risk {reward_risk:.2f} below minimum {min_reward_risk}")

    if fixed_contracts > 0:
        ceiling = min(fixed_contracts, max_contracts)
        if ceiling <= 0:
            return RiskDecision.reject(
                REASON_RISK_ZERO_SIZE, "account contract cap permits zero contracts",
            )
        risk_per_contract = calculate_risk_per_contract(
            stop_distance_ticks=intent.stop_distance_ticks,
            tick_value=tick_value,
            estimated_commission=commission_per_contract,
            estimated_slippage=Decimal("0.50"),
        )
        # Size DOWN to the largest contract count that fits the per-trade risk
        # cap. The strategy's real, evidence-derived stop is never replaced or
        # invented, and the cap is never relaxed - we simply trade fewer
        # contracts. Only if a *single* contract already exceeds the cap is the
        # stop genuinely too wide to trade at all (then reject honestly).
        contracts = min(ceiling, calculate_contracts(max_risk_per_trade_usd, risk_per_contract))
        if contracts <= 0:
            return RiskDecision.reject(
                REASON_STOP_TOO_WIDE,
                f"natural stop implies ${risk_per_contract} risk on a single contract, "
                f"exceeds ${max_risk_per_trade_usd} per-trade cap",
            )
        total_risk = risk_per_contract * contracts
        return RiskDecision(
            approved=True, contracts=contracts, reason_code=REASON_APPROVED,
            reason=f"fixed-size approved: {contracts} contract(s), "
                   f"${total_risk} risk within ${max_risk_per_trade_usd} cap")

    sizing = calculate_position_size(
        SizingInputs(
            account_size=max(balance, Decimal("0")),
            remaining_allowable_drawdown=drawdown_room,
            stop_distance_ticks=intent.stop_distance_ticks,
            tick_value=tick_value,
            estimated_commission=commission_per_contract,
            estimated_slippage=Decimal("0.50"),
        ),
    )
    contracts = min(sizing.contracts, max_contracts)
    if contracts <= 0:
        return RiskDecision.reject(REASON_RISK_ZERO_SIZE, "risk sizing permits zero contracts")
    return RiskDecision(approved=True, contracts=contracts,
                        reason_code=REASON_APPROVED, reason="risk approved")

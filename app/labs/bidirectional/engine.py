"""Deterministic paper engine for the Bidirectional Paper Trading Lab.

PAPER ONLY. This module imports NOTHING from the live execution stack
(app.execution.*, orders, gateway, brackets, demo_service) or from the receiver
/ recorder / runtime. It consumes an immutable ``MarketEvent`` stream and
simulates a self-contained paper account. There is structurally no code path
here that can reach a real broker (a test asserts the import graph).

The primary experiment (Mode A): when an armed activation level is reached, open
a LARGE long AND a LARGE short from the SAME market event, at the same logical
timestamp, with extremely tight independent stops - then let the losing leg stop
out while the survivor runs under break-even / trailing management, and judge the
NET of both legs together.

Event pipeline (deterministic, one pass per event):
  update snapshot -> evaluate armed levels -> trigger -> ATOMICALLY create both
  legs -> manage existing legs (stops, break-even, trailing, excursions) ->
  update account -> log. A leg opened on this event is never managed on its own
  entry event, so it can never be stopped by the tick that created it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.labs.bidirectional.config import (
    AccountConfig,
    ActivationSpec,
    BreakEvenConfig,
    TrailConfig,
)
from app.labs.bidirectional.market import MarketEvent, align_to_tick

# Activation level statuses.
ARMED = "ARMED"
TRIGGERED = "TRIGGERED"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"
DISABLED = "DISABLED"


@dataclass(slots=True)
class LabEvent:
    """One high-resolution log line."""

    ts_ns: int
    seq: int
    text: str


@dataclass(slots=True)
class Leg:
    """One independently-managed side of a paired setup."""

    setup_id: int
    activation_id: str
    side: str  # "long" | "short"
    qty: int
    entry_price: Decimal
    entry_ts_ns: int
    entry_seq: int
    initial_stop: Decimal
    stop: Decimal
    break_even: BreakEvenConfig
    trailing: TrailConfig
    entry_slippage_ticks: Decimal = Decimal("0")
    break_even_active: bool = False
    trailing_active: bool = False
    best_price: Decimal | None = None  # peak favourable mark since entry
    mfe_points: Decimal = Decimal("0")
    mae_points: Decimal = Decimal("0")
    max_stop_move_points: Decimal = Decimal("0")
    open: bool = True
    exit_price: Decimal | None = None
    exit_ts_ns: int | None = None
    exit_reason: str = ""
    gross_pnl: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")

    @property
    def sign(self) -> Decimal:
        return Decimal("1") if self.side == "long" else Decimal("-1")

    def commission_round_turn(self, account: AccountConfig) -> Decimal:
        return Decimal(2) * account.commission_per_contract * Decimal(self.qty)


@dataclass(slots=True)
class PairedSetup:
    """A long+short pair created atomically from one triggering event."""

    setup_id: int
    activation_id: str
    activation_price: Decimal
    trigger_price: Decimal
    trigger_ts_ns: int
    trigger_seq: int
    overshoot_points: Decimal
    entry_bid: Decimal | None
    entry_ask: Decimal | None
    long_leg: Leg | None
    short_leg: Leg | None

    def legs(self) -> tuple[Leg, ...]:
        return tuple(leg for leg in (self.long_leg, self.short_leg) if leg is not None)

    @property
    def closed(self) -> bool:
        return all(not leg.open for leg in self.legs())

    @property
    def net_pnl(self) -> Decimal:
        return sum((leg.net_pnl for leg in self.legs()), Decimal("0"))


@dataclass(slots=True)
class _LevelState:
    """Runtime state wrapping an ActivationSpec while it is worked."""

    spec: ActivationSpec
    status: str
    armed_source: str
    armed_comparison: str
    armed_ts_ns: int
    activations: int = 0
    last_trigger_ns: int = 0
    has_left: bool = True
    max_dist_since_trigger: Decimal = Decimal("0")
    setup_ids: list[int] = field(default_factory=list)


class LabEngine:
    """Self-contained paper engine. Feed it events with :meth:`on_event`."""

    def __init__(self, account: AccountConfig | None = None) -> None:
        self.account = account or AccountConfig()
        self.tick = self.account.tick_size
        self.balance = self.account.starting_balance
        self.peak_equity = self.account.starting_balance
        self.max_drawdown = Decimal("0")
        self.levels: dict[str, _LevelState] = {}
        self.setups: list[PairedSetup] = []
        self.open_legs: list[Leg] = []
        self.closed_legs: list[Leg] = []
        self.log: list[LabEvent] = []
        self.events_seen = 0
        self._seq = 0
        self._setup_counter = 0
        self._last: MarketEvent | None = None
        self._prev_source: dict[str, Decimal] = {}

    # -- read models ----------------------------------------------------------

    @property
    def realized_pnl(self) -> Decimal:
        return self.balance - self.account.starting_balance

    def unrealized_pnl(self) -> Decimal:
        if self._last is None:
            return Decimal("0")
        return sum((self._leg_unrealized(leg) for leg in self.open_legs), Decimal("0"))

    def equity(self) -> Decimal:
        return self.balance + self.unrealized_pnl()

    def gross_exposure_contracts(self) -> int:
        return sum(leg.qty for leg in self.open_legs)

    def leg_unrealized_pnl(self, leg: Leg) -> Decimal:
        """Unrealized P&L of one open leg at the current mark (paper)."""
        return self._leg_unrealized(leg)

    # -- activation-level management (all paper) ------------------------------

    def arm(self, spec: ActivationSpec) -> _LevelState:
        """Arm (or re-add) an activation level. Auto-resolves direction from market."""
        comparison = spec.comparison
        if comparison == "auto":
            market = self._effective_price()
            # Level at/below market -> wait for price to fall INTO it; above ->
            # wait for it to rise into it. Gaps are handled by "reach" semantics.
            comparison = "at_or_below" if (market is None or spec.price <= market) else "at_or_above"
        ts = self._now()
        state = _LevelState(
            spec=spec,
            status=DISABLED if not spec.enabled else ARMED,
            armed_source=spec.source,
            armed_comparison=comparison,
            armed_ts_ns=ts,
        )
        self.levels[spec.activation_id] = state
        self._append_log(ts, f"ACTIVATION #{spec.activation_id} {state.status} @ {spec.price} "
                             f"[{spec.source} {comparison}] L{spec.long_qty}/S{spec.short_qty} "
                             f"stop {spec.stop_ticks}t")
        return state

    def cancel(self, activation_id: str) -> None:
        state = self.levels.get(activation_id)
        if state is not None and state.status in (ARMED, TRIGGERED, DISABLED):
            state.status = CANCELLED
            self._append_log(self._now(), f"ACTIVATION #{activation_id} CANCELLED")

    def cancel_all(self) -> None:
        for activation_id in list(self.levels):
            self.cancel(activation_id)

    def set_enabled(self, activation_id: str, enabled: bool) -> None:
        state = self.levels.get(activation_id)
        if state is None or state.status in (CANCELLED, COMPLETED):
            return
        state.status = ARMED if enabled else DISABLED
        self._append_log(self._now(), f"ACTIVATION #{activation_id} {'ENABLED' if enabled else 'DISABLED'}")

    def rearm(self, activation_id: str) -> None:
        state = self.levels.get(activation_id)
        if state is None or state.status not in (TRIGGERED, COMPLETED, DISABLED, CANCELLED):
            return
        state.status = ARMED
        state.has_left = True
        state.max_dist_since_trigger = Decimal("0")
        self._append_log(self._now(), f"ACTIVATION #{activation_id} REARMED")

    # -- the deterministic event step -----------------------------------------

    def on_event(self, event: MarketEvent) -> None:
        """Advance one market event through the full deterministic pipeline."""
        self._seq += 1
        self.events_seen += 1
        seq = self._seq
        stamped = MarketEvent(ts_ns=event.ts_ns, seq=seq, last=event.last,
                              bid=event.bid, ask=event.ask, kind=event.kind)
        self._last = stamped

        # 1) evaluate armed levels and ATOMICALLY create paired setups.
        for state in list(self.levels.values()):
            self._service_level(state, stamped)

        # 2) manage existing legs (never the ones opened on THIS event).
        for leg in list(self.open_legs):
            if leg.entry_seq == seq:
                continue
            self._manage_leg(leg, stamped)

        # 3) account/equity bookkeeping.
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - eq)

        # 4) remember source prices for crossing detection on the next event.
        for src in ("last", "bid", "ask", "mid"):
            value = stamped.price(src)
            if value is not None:
                self._prev_source[src] = value

    # -- manual paper controls (same paired engine as activation triggers) ----

    def enter_both(self, long_qty: int, short_qty: int, stop_ticks: Decimal, *,
                   break_even: BreakEvenConfig | None = None, trailing: TrailConfig | None = None,
                   activation_id: str = "manual") -> PairedSetup | None:
        """Manually open a paired setup from the CURRENT market event (atomic)."""
        if self._last is None:
            return None
        price = self._effective_price() or Decimal("1")
        spec = ActivationSpec(
            activation_id=activation_id, price=price, long_qty=long_qty, short_qty=short_qty,
            stop_ticks=stop_ticks, break_even=break_even or BreakEvenConfig(),
            trailing=trailing or TrailConfig(),
        )
        return self._open_paired(spec, self._last, price)

    def flatten(self, reason: str = "flatten") -> None:
        """Close every open leg at the current market (paper)."""
        if self._last is None:
            return
        for leg in list(self.open_legs):
            self._close_leg(leg, self._market_exit_price(leg, self._last), self._last, reason)

    # -- internals ------------------------------------------------------------

    def _service_level(self, state: _LevelState, ev: MarketEvent) -> None:
        if state.status != ARMED:
            return
        spec = state.spec
        if spec.expire_after_ns and ev.ts_ns - state.armed_ts_ns >= spec.expire_after_ns:
            state.status = CANCELLED
            self._append_log(ev.ts_ns, f"ACTIVATION #{spec.activation_id} EXPIRED")
            return
        cs = ev.price(state.armed_source)
        if cs is None:
            return  # this event carries no price for our source; cannot evaluate
        dist_ticks = abs(cs - spec.price) / self.tick
        if spec.cancel_if_moves_away_ticks and dist_ticks >= spec.cancel_if_moves_away_ticks:
            state.status = CANCELLED
            self._append_log(ev.ts_ns, f"ACTIVATION #{spec.activation_id} CANCELLED "
                                       f"(moved {dist_ticks:.0f}t away)")
            return

        satisfied = self._condition_satisfied(state, cs)
        if state.activations > 0:  # re-arm bookkeeping since the last trigger
            state.max_dist_since_trigger = max(state.max_dist_since_trigger, abs(cs - spec.price))
            if spec.require_leave_reenter:
                if state.max_dist_since_trigger >= spec.leave_distance_ticks * self.tick:
                    state.has_left = True
            elif not satisfied:
                state.has_left = True

        if not satisfied or not self._trigger_eligible(state, ev):
            return

        trigger_price = self._effective_price() or cs
        setup = self._open_paired(spec, ev, trigger_price)
        state.activations += 1
        state.last_trigger_ns = ev.ts_ns
        state.has_left = False
        state.max_dist_since_trigger = Decimal("0")
        if setup is not None:
            state.setup_ids.append(setup.setup_id)
        state.status = COMPLETED if (spec.one_shot or state.activations >= spec.max_activations) else ARMED

    def _condition_satisfied(self, state: _LevelState, cs: Decimal) -> bool:
        comp = state.armed_comparison
        level = state.spec.price
        ps = self._prev_source.get(state.armed_source)
        if comp == "at_or_below":
            return cs <= level
        if comp == "at_or_above":
            return cs >= level
        if comp == "cross_down":
            return ps is not None and ps > level and cs <= level
        if comp == "cross_up":
            return ps is not None and ps < level and cs >= level
        if comp == "touch":
            return abs(cs - level) < self.tick
        return False

    def _trigger_eligible(self, state: _LevelState, ev: MarketEvent) -> bool:
        spec = state.spec
        if state.activations == 0:
            return True
        if not state.has_left:
            return False
        if spec.rearm_cooldown_ns and ev.ts_ns - state.last_trigger_ns < spec.rearm_cooldown_ns:
            return False
        if spec.require_leave_reenter and state.max_dist_since_trigger < spec.leave_distance_ticks * self.tick:
            return False
        return True

    def _open_paired(self, spec: ActivationSpec, ev: MarketEvent, trigger_price: Decimal) -> PairedSetup | None:
        exposure = self.gross_exposure_contracts() + spec.long_qty + spec.short_qty
        if exposure > self.account.max_gross_contracts:
            self._append_log(ev.ts_ns, f"SETUP refused: exposure cap {self.account.max_gross_contracts}")
            return None
        self._setup_counter += 1
        setup_id = self._setup_counter
        long_leg = self._make_leg(setup_id, spec, "long", ev) if spec.long_qty > 0 else None
        short_leg = self._make_leg(setup_id, spec, "short", ev) if spec.short_qty > 0 else None
        setup = PairedSetup(
            setup_id=setup_id, activation_id=spec.activation_id, activation_price=spec.price,
            trigger_price=trigger_price, trigger_ts_ns=ev.ts_ns, trigger_seq=ev.seq,
            overshoot_points=trigger_price - spec.price, entry_bid=ev.bid, entry_ask=ev.ask,
            long_leg=long_leg, short_leg=short_leg,
        )
        self.setups.append(setup)
        self._append_log(ev.ts_ns, f"SETUP #{setup_id} CREATED from ACTIVATION #{spec.activation_id} "
                                   f"(level {spec.price}, trigger {trigger_price}, "
                                   f"overshoot {setup.overshoot_points:+} pts)")
        for leg in (long_leg, short_leg):
            if leg is None:
                continue
            self.open_legs.append(leg)
            self._append_log(ev.ts_ns, f"  {leg.side.upper()} {leg.qty} FILLED @ {leg.entry_price} "
                                       f"stop {leg.stop} (setup #{setup_id})")
        return setup

    def _make_leg(self, setup_id: int, spec: ActivationSpec, side: str, ev: MarketEvent) -> Leg:
        qty = spec.long_qty if side == "long" else spec.short_qty
        entry = self._entry_fill_price(side, ev)
        stop_distance = spec.stop_ticks * self.tick
        stop = entry - stop_distance if side == "long" else entry + stop_distance
        return Leg(
            setup_id=setup_id, activation_id=spec.activation_id, side=side, qty=qty,
            entry_price=entry, entry_ts_ns=ev.ts_ns, entry_seq=ev.seq,
            initial_stop=stop, stop=stop, best_price=entry,
            break_even=spec.break_even, trailing=spec.trailing,
            entry_slippage_ticks=self.account.entry_slippage_ticks,
        )

    def _entry_fill_price(self, side: str, ev: MarketEvent) -> Decimal:
        slip = self.account.entry_slippage_ticks * self.tick
        if side == "long":  # a buy lifts the offer
            touch = ev.ask or ev.last or ev.mid or ev.bid
            return align_to_tick(touch + slip, self.tick, round_up=True)
        touch = ev.bid or ev.last or ev.mid or ev.ask  # a sell hits the bid
        return align_to_tick(touch - slip, self.tick, round_up=False)

    def _manage_leg(self, leg: Leg, ev: MarketEvent) -> None:
        mark = self._mark_price(leg, ev)
        if mark is None:
            return
        fav = (mark - leg.entry_price) * leg.sign
        leg.mfe_points = max(leg.mfe_points, fav)
        leg.mae_points = max(leg.mae_points, -fav)
        if leg.best_price is None or (mark - leg.best_price) * leg.sign > 0:
            leg.best_price = mark
        self._advance_stop(leg, ev)  # tighten first, then judge this event
        stop_hit = mark <= leg.stop if leg.side == "long" else mark >= leg.stop
        if stop_hit:
            self._close_leg(leg, self._stop_fill_price(leg, ev), ev, "stop")

    def _advance_stop(self, leg: Leg, ev: MarketEvent) -> None:
        be = leg.break_even
        tr = leg.trailing
        fav_ticks = leg.mfe_points / self.tick
        new_stop = leg.stop
        if be.enabled and be.trigger_ticks > 0 and fav_ticks >= be.trigger_ticks:
            be_stop = leg.entry_price + leg.sign * be.offset_ticks * self.tick
            new_stop = self._tighter(new_stop, be_stop, leg.side)
            if not leg.break_even_active:
                leg.break_even_active = True
                self._append_log(ev.ts_ns, f"  {leg.side.upper()} BREAK-EVEN active (setup #{leg.setup_id})")
        if tr.enabled and (tr.immediate or fav_ticks >= tr.activation_ticks):
            best = leg.best_price if leg.best_price is not None else leg.entry_price
            trailed = best - leg.sign * tr.distance_ticks * self.tick
            candidate = self._tighter(new_stop, trailed, leg.side)
            if tr.step_ticks <= 0 or abs(candidate - leg.stop) >= tr.step_ticks * self.tick:
                new_stop = candidate
            if not leg.trailing_active:
                leg.trailing_active = True
                self._append_log(ev.ts_ns, f"  {leg.side.upper()} TRAILING active (setup #{leg.setup_id})")
        if new_stop != leg.stop:
            leg.max_stop_move_points = max(leg.max_stop_move_points, abs(new_stop - leg.initial_stop))
            leg.stop = new_stop

    def _tighter(self, current: Decimal, candidate: Decimal, side: str) -> Decimal:
        # A stop may only move in the favourable direction, never widen risk.
        return max(current, candidate) if side == "long" else min(current, candidate)

    def _close_leg(self, leg: Leg, exit_price: Decimal, ev: MarketEvent, reason: str) -> None:
        if not leg.open:
            return
        leg.open = False
        leg.exit_price = exit_price
        leg.exit_ts_ns = ev.ts_ns
        leg.exit_reason = reason
        points = (exit_price - leg.entry_price) * leg.sign
        per_point = self.account.tick_value / self.tick
        leg.gross_pnl = points * per_point * Decimal(leg.qty)
        leg.commission = leg.commission_round_turn(self.account)
        leg.net_pnl = leg.gross_pnl - leg.commission
        self.balance += leg.net_pnl
        if leg in self.open_legs:
            self.open_legs.remove(leg)
        self.closed_legs.append(leg)
        self._append_log(ev.ts_ns, f"  {leg.side.upper()} {reason.upper()} @ {exit_price} "
                                   f"net {leg.net_pnl:+.2f} (setup #{leg.setup_id})")

    def _mark_price(self, leg: Leg, ev: MarketEvent) -> Decimal | None:
        if ev.last is not None:
            return ev.last
        if ev.mid is not None:
            return ev.mid
        return (ev.bid if leg.side == "long" else ev.ask) or ev.ask or ev.bid

    def _stop_fill_price(self, leg: Leg, ev: MarketEvent) -> Decimal:
        slip = self.account.stop_slippage_ticks * self.tick
        mark = self._mark_price(leg, ev) or leg.stop
        if leg.side == "long":  # exit sells the bid; a gap fills worse than the stop
            base = min(leg.stop, ev.bid or mark)
            return align_to_tick(base - slip, self.tick, round_up=False)
        base = max(leg.stop, ev.ask or mark)
        return align_to_tick(base + slip, self.tick, round_up=True)

    def _market_exit_price(self, leg: Leg, ev: MarketEvent) -> Decimal:
        slip = self.account.exit_slippage_ticks * self.tick
        if leg.side == "long":
            touch = ev.bid or ev.last or ev.mid or ev.ask
            return align_to_tick(touch - slip, self.tick, round_up=False)
        touch = ev.ask or ev.last or ev.mid or ev.bid
        return align_to_tick(touch + slip, self.tick, round_up=True)

    def _leg_unrealized(self, leg: Leg) -> Decimal:
        if self._last is None:
            return Decimal("0")
        mark = self._market_exit_price(leg, self._last)
        points = (mark - leg.entry_price) * leg.sign
        gross = points * (self.account.tick_value / self.tick) * Decimal(leg.qty)
        return gross - leg.commission_round_turn(self.account)

    def _effective_price(self) -> Decimal | None:
        if self._last is None:
            return None
        return self._last.last or self._last.mid or self._last.bid or self._last.ask

    def _now(self) -> int:
        return self._last.ts_ns if self._last is not None else 0

    def _append_log(self, ts_ns: int, text: str) -> None:
        self.log.append(LabEvent(ts_ns=ts_ns, seq=self._seq, text=text))

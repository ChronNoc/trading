"""Causal streaming delayed-paper engine — evaluates the LIVE delayed stream.

The regression this fixes: delayed data set ``decisions_allowed = False`` and the
runtime went to RECORDING_ONLY, so nothing ever evaluated a setup while Bookmap
was streaming. Paper evaluation only happened offline, over *finalized* sessions,
which meant an actively recording session produced zero evaluations forever.

Two different questions were being conflated:

* "may this data reach a broker?" — for delayed data, **never**.
* "may this data be evaluated on paper?" — **yes, immediately**.

This engine answers the second. It is structurally simulation-only: it imports no
execution/broker module (enforced by test), so delayed data cannot reach an
order router no matter what it decides.

Causality: state advances event by event. A setup is evaluated only from the
window of events already seen; the engine never looks ahead. It reuses the SAME
``evaluate_day_trading_plan`` and ``derive_strategy_context`` that the offline
episode builder uses, so streaming and replay cannot drift into two strategies.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from app.market.state import MarketState
from app.paper.execution import ExecutionConfig, MarketTick, size_intent
from app.paper.models import (
    CloseReason,
    Direction,
    PaperOrderIntent,
    PaperTrade,
    RiskDecision,
    SetupProvenance,
)
from app.research.causal_context import (
    CausalLevelTracker,
    derive_strategy_context,
    trading_day_for_timestamp,
)
from app.research.episode_builder import STRATEGY_VERSION, EpisodeConfig
from app.strategy.order_flow import OrderFlowThresholds, TradeDirection, evaluate_day_trading_plan

# A candidate the strategy did not fully specify is never repaired with guesses.
REASON_NO_LEVELS = "strategy_levels_incomplete"
REASON_INVALID_GEOMETRY = "invalid_stop_target_geometry"

STATE_WAITING = "WAITING_FOR_DATA"
STATE_WARMING = "WARMING_UP"
STATE_EVALUATING = "EVALUATING"
STATE_ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """One deterministic sub-rule outcome, always explainable."""

    name: str
    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One evaluation of one direction at one event — full traceability."""

    session_id: str
    setup_id: str
    strategy_version: str
    direction: str
    evaluated_at_ns: int
    event_index: int
    accepted: bool
    conditions: tuple[ConditionResult, ...]
    rejection_reasons: tuple[str, ...]

    @property
    def first_failure(self) -> str:
        """Return the first failing condition name (the headline reason)."""
        for condition in self.conditions:
            if not condition.passed:
                return condition.name
        return ""


@dataclass(slots=True)
class PaperEngineStatus:
    """Live status for the GUI. Never blocks the caller."""

    state: str = STATE_WAITING
    session_id: str = ""
    contract: str = ""
    warmup_events_seen: int = 0
    warmup_events_required: int = 0
    evaluations: int = 0
    accepted_setups: int = 0
    events_seen: int = 0
    last_setup: str = ""
    last_direction: str = ""
    last_decision: str = ""
    last_reason: str = ""
    last_evaluation_ns: int = 0
    top_rejections: tuple[tuple[str, int], ...] = ()
    disabled_setups: tuple[tuple[str, str], ...] = ()
    error: str = ""
    # An event the parser rejects is DROPPED. Silently dropping market data would
    # corrupt every downstream number while the GUI still looked healthy, so the
    # count is surfaced rather than hidden behind a stale error string.
    malformed_events: int = 0
    # --- simulated execution -------------------------------------------------
    candidates: int = 0
    risk_rejected: int = 0
    orders_submitted: int = 0
    pending_order: str = ""
    open_position: str = "none"
    position_entry: str = ""
    position_stop: str = ""
    position_target: str = ""
    unrealized_pnl: str = "0.00"
    realized_pnl: str = "0.00"
    balance: str = "0.00"
    trades: int = 0
    wins: int = 0
    losses: int = 0
    last_close_reason: str = ""

    @property
    def warmup_fraction(self) -> float:
        """Return warm-up progress as 0..1 (bounded and visible)."""
        if self.warmup_events_required <= 0:
            return 1.0
        return min(1.0, self.warmup_events_seen / self.warmup_events_required)


class DelayedPaperEngine:
    """Evaluates the live delayed stream causally, event by event.

    Thread-safety: ``on_market_event`` runs on the receiver's loop; ``status()``
    is called from the GUI. A lock guards the small status block only - the hot
    path never blocks on the GUI.
    """

    def __init__(
        self,
        *,
        config: EpisodeConfig | None = None,
        thresholds: OrderFlowThresholds | None = None,
        capabilities: Mapping[str, bool] | None = None,
        account_profile: object | None = None,
        is_synthetic_fixture: bool = False,
    ) -> None:
        """Create an engine that begins evaluating as soon as warm-up completes.

        It also owns a :class:`PaperExecutor`, so a qualifying setup automatically
        becomes a risk-checked, causally-filled simulated trade - no button.
        """
        from app.paper.execution import PaperExecutor
        from app.risk.account_profile import load_selected_profile

        self._profile = account_profile or load_selected_profile()
        limits = self._profile.effective_limits()  # type: ignore[union-attr]
        self._executor = PaperExecutor(
            starting_balance=self._profile.account_size,  # type: ignore[union-attr]
            max_contracts=limits.max_contracts,
            is_synthetic_fixture=is_synthetic_fixture,
        )
        self._config = config or EpisodeConfig()
        self._thresholds = thresholds or OrderFlowThresholds(tick_size=self._config.tick_size)
        self._tracker = CausalLevelTracker()
        self._window: deque[MarketState] = deque(maxlen=self._config.window_size)
        self._state = MarketState()
        self._lock = threading.Lock()
        self._status = PaperEngineStatus(
            warmup_events_required=self._config.warmup_events,
            state=STATE_WAITING,
        )
        self._rejections: Counter[str] = Counter()
        self._event_index = 0
        self._decision_opportunities = 0
        self._evaluations: deque[EvaluationRecord] = deque(maxlen=500)
        self._exec_config = ExecutionConfig()
        self._peak_balance = self._profile.account_size  # type: ignore[union-attr]
        self._risk_rejections: Counter[str] = Counter()
        self._closed_trades: deque[PaperTrade] = deque(maxlen=500)
        self._on_trade_closed: list[object] = []
        self._sync_execution_status(None)
        # A capability the feed cannot supply disables only the setups that need
        # it - never the whole engine.
        from app.market.capabilities import CapabilityId, FeedCapabilities

        self._capabilities = FeedCapabilities()
        caps = dict(capabilities or {})
        disabled: list[tuple[str, str]] = []
        # MBO is structurally unavailable on this bridge (no per-order IDs), so
        # the setup that needs it is disabled unless a caller proves otherwise.
        if not caps.get(CapabilityId.MBO.value, caps.get("mbo", False)):
            disabled.append(("iceberg_continuation", "requires MBO (order-by-order) data"))
        self._disabled = tuple(disabled)

    def capabilities(self) -> "FeedCapabilities":  # noqa: F821 - imported in __init__
        """Return what the feed has ACTUALLY delivered this session."""
        return self._capabilities

    # -- session identity ---------------------------------------------------------

    def bind_session(self, session_id: str, contract: str) -> None:
        """Attach the active session id and resolved contract for traceability."""
        with self._lock:
            self._status.session_id = session_id
            self._status.contract = contract
            self._status.disabled_setups = self._disabled

    # -- the causal hot path --------------------------------------------------------

    def on_market_event(self, event: Mapping[str, object]) -> None:
        """Advance state with ONE event and evaluate when it is a decision point.

        Causal by construction: only events already seen are in the window, and a
        decision is taken from that window alone.
        """
        try:
            self._state = self._state.update(event)
        except Exception as error:  # noqa: BLE001 - a bad event must not kill capture
            with self._lock:
                self._status.state = STATE_ERROR
                self._status.error = f"{type(error).__name__}: {error}"
                self._status.malformed_events += 1
            return

        self._window.append(self._state)
        self._event_index += 1
        # Capabilities are OBSERVED from the real stream, never declared: a
        # depth-only feed must report trades as degraded, not available.
        self._capabilities = self._capabilities.observe_event(event)
        tick = self._tick()

        # An event first resolves exposure that already exists (fills, stops,
        # targets, time stops) and only then may create new exposure. Position
        # management runs on EVERY event, not on the decision stride: a stop is
        # not allowed to wait for the next evaluation.
        if tick is not None:
            self._advance_execution(tick)

        with self._lock:
            self._status.events_seen = self._event_index
            self._status.warmup_events_seen = min(self._event_index, self._config.warmup_events)
            if self._event_index < self._config.warmup_events:
                self._status.state = STATE_WARMING
                return
            self._status.state = STATE_EVALUATING

        # Only depth/trade events are decision opportunities; stride keeps the
        # cost bounded without ever looking ahead.
        self._decision_opportunities += 1
        if self._decision_opportunities % self._config.decision_stride != 0:
            return
        self._evaluate_now(tick)

    def _tick(self) -> MarketTick | None:
        """Build the executor's causal view of the current event, if priced."""
        price = self._state.mid_price or self._state.best_bid or self._state.best_ask
        if price is None:
            return None
        return MarketTick(
            event_index=self._event_index,
            ts_ns=self._state.timestamp_ns,
            price=price,
            best_bid=self._state.best_bid,
            best_ask=self._state.best_ask,
        )

    def _advance_execution(self, tick: MarketTick) -> None:
        """Let the executor fill/manage from this event, then publish state."""
        try:
            trade = self._executor.on_tick(tick)
        except Exception as error:  # noqa: BLE001 - never kill capture
            with self._lock:
                self._status.error = f"execution error: {type(error).__name__}: {error}"
            return
        if trade is not None:
            self._closed_trades.append(trade)
            self._peak_balance = max(self._peak_balance, self._executor.balance)
            for callback in self._on_trade_closed:
                try:
                    callback(trade)  # type: ignore[operator]
                except Exception:  # noqa: BLE001,S110 - a sink must not kill the stream
                    pass
        self._sync_execution_status(tick)

    def _evaluate_now(self, tick: MarketTick | None) -> None:
        window = tuple(self._window)
        if not window:
            return
        for direction in (TradeDirection.LONG, TradeDirection.SHORT):
            try:
                derived = derive_strategy_context(
                    window, direction, self._tracker, self._thresholds,
                    stop_buffer_points=self._config.stop_buffer_points,
                )
                evaluation = evaluate_day_trading_plan(window, derived.context, self._thresholds)
            except Exception as error:  # noqa: BLE001 - keep the stream alive
                with self._lock:
                    self._status.error = f"evaluation error: {type(error).__name__}: {error}"
                continue
            self._record(direction, evaluation, derived, tick)

    def _record(
        self,
        direction: TradeDirection,
        evaluation: object,
        derived: object | None = None,
        tick: MarketTick | None = None,
    ) -> None:
        conditions = tuple(
            ConditionResult(name=c.key, passed=c.passed, message=c.message)
            for c in evaluation.conditions  # type: ignore[attr-defined]
        )
        reasons = tuple(c.message for c in conditions if not c.passed)
        accepted = bool(evaluation.accepted)  # type: ignore[attr-defined]
        with self._lock:
            session_id = self._status.session_id
        record = EvaluationRecord(
            session_id=session_id,
            setup_id=f"{session_id}:{direction.value}:{self._event_index}",
            strategy_version=STRATEGY_VERSION,
            direction=direction.value,
            evaluated_at_ns=time.time_ns(),
            event_index=self._event_index,
            accepted=accepted,
            conditions=conditions,
            rejection_reasons=reasons,
        )
        self._evaluations.append(record)
        for condition in conditions:
            if not condition.passed:
                self._rejections[condition.name] += 1
        with self._lock:
            self._status.evaluations += 1
            self._status.last_setup = f"{STRATEGY_VERSION}_{direction.value}"
            self._status.last_direction = direction.value
            self._status.last_decision = "accepted" if accepted else "rejected"
            self._status.last_reason = "" if accepted else (record.first_failure or "")
            self._status.last_evaluation_ns = record.evaluated_at_ns
            self._status.top_rejections = tuple(self._rejections.most_common(5))
            if accepted:
                self._status.accepted_setups += 1

        # An accepted setup is the ONLY thing that may produce a candidate.
        if accepted and derived is not None and tick is not None:
            self._consider_entry(record, derived, tick)

    # -- candidate -> risk -> simulated order -----------------------------------------

    def _consider_entry(self, record: EvaluationRecord, derived: object, tick: MarketTick) -> None:
        """Turn an accepted setup into a risk-checked simulated order.

        Every price comes from the strategy context that produced the decision.
        If the strategy did not supply a stop or a target, there is no trade -
        the missing level is reported, never invented.
        """
        context = derived.context  # type: ignore[attr-defined]
        stop = context.stop_price
        target = context.target_price
        if stop is None or target is None:
            self._note_risk_rejection(REASON_NO_LEVELS, "strategy supplied no stop/target level")
            return
        with self._lock:
            session_id, contract = self._status.session_id, self._status.contract
            self._status.candidates += 1

        direction = Direction.LONG if record.direction == TradeDirection.LONG.value else Direction.SHORT
        try:
            intent = PaperOrderIntent(
                direction=direction,
                entry_reference=tick.price,
                stop=stop,
                target=target,
                provenance=SetupProvenance(
                    session_id=session_id,
                    setup_id=record.setup_id,
                    strategy_version=record.strategy_version,
                    contract=contract,
                    decision_event_index=record.event_index,
                    decision_ts_ns=record.evaluated_at_ns,
                    conditions_passed=tuple(c.name for c in record.conditions if c.passed),
                ),
            )
        except ValueError as error:
            # e.g. price has already moved through the stop: not a tradeable plan.
            self._note_risk_rejection(REASON_INVALID_GEOMETRY, str(error))
            return

        trading_day = trading_day_for_timestamp(tick.ts_ns)
        blocked = self._executor.gate(intent, tick, trading_day=trading_day)
        decision = blocked or size_intent(
            intent,
            balance=self._executor.balance,
            drawdown_room=self._profile.drawdown_room(  # type: ignore[union-attr]
                self._executor.balance, self._peak_balance,
            ),
            max_contracts=self._profile.effective_limits().max_contracts,  # type: ignore[union-attr]
            commission_per_contract=self._exec_config.commission_per_contract,
        )
        self._executor.submit(intent, decision, tick, trading_day=trading_day)
        if not decision.approved:
            self._note_risk_rejection(decision.reason_code, decision.reason)
            return
        with self._lock:
            self._status.orders_submitted += 1
        self._sync_execution_status(tick)

    def _note_risk_rejection(self, code: str, reason: str) -> None:
        self._risk_rejections[code] += 1
        with self._lock:
            self._status.risk_rejected += 1
            self._status.pending_order = f"no order: {reason}"

    def _sync_execution_status(self, tick: MarketTick | None) -> None:
        """Publish the executor's real state to the GUI status block."""
        executor = self._executor
        position = executor.position
        pending = executor.pending
        mark = tick.price if tick is not None else None
        trades = executor.trades
        with self._lock:
            self._status.balance = str(executor.balance)
            self._status.realized_pnl = str(executor.realized_pnl)
            self._status.trades = len(trades)
            self._status.wins = sum(1 for t in trades if t.won)
            self._status.losses = sum(1 for t in trades if not t.won)
            if trades:
                self._status.last_close_reason = trades[-1].close_reason.value
            if pending is not None:
                self._status.pending_order = (
                    f"{pending.intent.direction.value} {pending.contracts} awaiting causal fill"
                )
            elif position is None:
                self._status.pending_order = self._status.pending_order or "none"
            if position is None:
                self._status.open_position = "none"
                self._status.position_entry = ""
                self._status.position_stop = ""
                self._status.position_target = ""
                self._status.unrealized_pnl = "0.00"
                return
            self._status.pending_order = "none"
            self._status.open_position = f"{position.direction.value} {position.contracts}"
            self._status.position_entry = str(position.entry_price)
            self._status.position_stop = str(position.stop)
            self._status.position_target = str(position.target)
            if mark is not None:
                self._status.unrealized_pnl = str(position.unrealized_pnl(mark))

    # -- lifecycle -------------------------------------------------------------------

    def on_trade_closed(self, callback: object) -> None:
        """Register a sink (e.g. the ledger) called with each closed trade."""
        self._on_trade_closed.append(callback)

    def flatten(self, reason: CloseReason = CloseReason.SESSION_CLOSE) -> PaperTrade | None:
        """Flatten any open simulated position (session close / kill switch)."""
        tick = self._tick()
        if tick is None:
            return None
        trade = self._executor.liquidate(tick, reason)
        if trade is not None:
            self._closed_trades.append(trade)
            for callback in self._on_trade_closed:
                try:
                    callback(trade)  # type: ignore[operator]
                except Exception:  # noqa: BLE001,S110
                    pass
        self._sync_execution_status(tick)
        return trade

    # -- read models -----------------------------------------------------------------

    def status(self) -> PaperEngineStatus:
        """Return a copy of the live status (safe from the GUI thread)."""
        from dataclasses import replace

        with self._lock:
            return replace(self._status)

    def recent_evaluations(self, limit: int = 20) -> tuple[EvaluationRecord, ...]:
        """Return the most recent evaluations, newest last."""
        return tuple(self._evaluations)[-limit:]

    def recent_trades(self, limit: int = 50) -> tuple[PaperTrade, ...]:
        """Return the most recent closed simulated trades, newest last."""
        return tuple(self._closed_trades)[-limit:]

    def top_risk_rejections(self, limit: int = 5) -> tuple[tuple[str, int], ...]:
        """Return why candidates did not become orders (the honest zero-trade view)."""
        return tuple(self._risk_rejections.most_common(limit))

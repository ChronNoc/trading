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
from app.research.causal_context import CausalLevelTracker, derive_strategy_context
from app.research.episode_builder import STRATEGY_VERSION, EpisodeConfig
from app.strategy.order_flow import OrderFlowThresholds, TradeDirection, evaluate_day_trading_plan

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
    ) -> None:
        """Create an engine that begins evaluating as soon as warm-up completes."""
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
        # A capability the feed cannot supply disables only the setups that need
        # it - never the whole engine.
        caps = dict(capabilities or {})
        disabled: list[tuple[str, str]] = []
        if not caps.get("mbo", False):
            disabled.append(("iceberg_continuation", "requires MBO (order-by-order) data"))
        self._disabled = tuple(disabled)

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
            return

        self._window.append(self._state)
        self._event_index += 1

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
        self._evaluate_now()

    def _evaluate_now(self) -> None:
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
            self._record(direction, evaluation)

    def _record(self, direction: TradeDirection, evaluation: object) -> None:
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

    # -- read models -----------------------------------------------------------------

    def status(self) -> PaperEngineStatus:
        """Return a copy of the live status (safe from the GUI thread)."""
        from dataclasses import replace

        with self._lock:
            return replace(self._status)

    def recent_evaluations(self, limit: int = 20) -> tuple[EvaluationRecord, ...]:
        """Return the most recent evaluations, newest last."""
        return tuple(self._evaluations)[-limit:]

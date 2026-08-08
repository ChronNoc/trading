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
import re
from collections import Counter, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from app.market.state import MarketState
from app.paper.execution import ExecutionConfig, MarketTick, size_intent
from app.paper.models import (
    CloseReason,
    Direction,
    PaperOrderIntent,
    PaperTrade,
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

# -- ML decision-policy constants (see _apply_ml_policy) -------------------------
# decision_source values: which logic produced accepted/rejected.
DECISION_SOURCE_HEURISTIC = "HEURISTIC"
DECISION_SOURCE_ML = "ML"
DECISION_SOURCE_BLENDED = "BLENDED"
DECISION_SOURCE_FALLBACK = "FALLBACK"
# Versions the historical record so future policy changes are distinguishable.
ML_POLICY_VERSION = "ml-veto-v1"
# Conservative veto-only threshold: a heuristic-accepted setup is overridden to
# rejected only when the model's success probability is below this. Chosen as
# a clearly-below-coinflip bar so the veto only fires on a confident negative
# signal, never on ordinary uncertainty. Documented here, not derived from any
# backtest, until real shadow-scoring evidence justifies tuning it.
ML_VETO_PROBABILITY_THRESHOLD = 0.35
# A prediction is causal evidence only near the market event that produced it.
# The feature sampler runs every 30 seconds by default; allowing two cadences
# prevents boundary jitter while forbidding indefinite reuse of stale scores.
ML_PREDICTION_CORRELATION_WINDOW_NS = 60_000_000_000


def _widen_target(
    direction: Direction,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    target_reward_risk: Decimal,
) -> Decimal:
    """Aim FURTHER than the structural draw-on-liquidity target for bigger trades.

    When ``target_reward_risk > 0`` the target is pushed out to at least that
    multiple of the REAL stop distance from entry, in the trade direction. It is
    never moved CLOSER than the strategy's own target (the further of the two
    wins), and the real stop is never touched - so a widened target only ever
    aims for more, never fakes a level. 0 leaves the strategy's target unchanged.
    """
    if target_reward_risk <= 0:
        return target
    risk = abs(entry - stop)
    if risk <= 0:
        return target
    reach = target_reward_risk * risk
    if direction == Direction.LONG:
        return max(target, entry + reach)
    return min(target, entry - reach)


@dataclass(frozen=True, slots=True)
class ConditionResult:
    """One deterministic sub-rule outcome, always explainable."""

    name: str
    passed: bool
    message: str
    observed: str = ""
    required: str = ""


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
    # -- ML decision-policy provenance (see _apply_ml_policy) -------------------
    # Which logic actually produced ``accepted``: "HEURISTIC" (heuristic alone,
    # policy off or model not consulted), "ML" (heuristic accepted, model
    # probability was available and consulted but did not veto), "BLENDED"
    # (heuristic accepted, model probability vetoed it), or "FALLBACK" (policy
    # enabled but no usable model - behaviour matches heuristic-only).
    decision_source: str = DECISION_SOURCE_HEURISTIC
    # The model's success_probability consulted for this decision, when
    # available; None when the policy was off or no model was consulted.
    confidence: float | None = None
    # The untransformed output returned by this binary model. Kept distinct
    # from ``confidence`` so a future policy may calibrate/transform confidence
    # without losing the original model evidence. The current loader exposes a
    # single success probability, represented as a structured scalar payload.
    raw_model_output: dict[str, float] | None = None
    # The approved artifact_id backing ``confidence``, or "" when none.
    model_version: str = ""
    # Atomic identity from the exact correlated prediction evidence.
    model_prediction_id: str = ""
    model_artifact_sha256: str = ""
    # Why ML did not/could not influence this decision (e.g. "policy
    # disabled", "no approved model", "model not SCORING"); "" when ML was
    # actually consulted and influenced (or could have influenced) the result.
    fallback_reason: str = ""
    # A literal version string for the blending policy logic itself, so
    # future policy changes are distinguishable in the historical record.
    policy_version: str = ML_POLICY_VERSION

    @property
    def first_failure(self) -> str:
        """Return the first failing condition name (the headline reason)."""
        for condition in self.conditions:
            if not condition.passed:
                return condition.name
        return ""


def _pending_order_text(order: object) -> str:
    """One-line pending-order description for the GUI.

    A passive (maker) limit shows the price it is RESTING at, so a limit-scalper
    can see the order working the book; a market order just awaits its causal
    fill. Duck-typed to avoid importing the order model into this hot path.
    """
    direction = getattr(getattr(order, "intent", None), "direction", None)
    label = getattr(direction, "value", "?")
    contracts = getattr(order, "contracts", 0)
    resting = getattr(order, "entry_limit_price", None)
    if resting is not None:
        return f"{label} {contracts} resting @ {resting} (limit)"
    return f"{label} {contracts} awaiting causal fill"


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
    momentum_enabled: bool = False
    strategy_profile: str = "canonical"
    instrument: str = "MNQ"
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
    # --- analysis-stream integrity -------------------------------------------
    # Events the bounded analysis feed skipped (recording was unaffected), how
    # many distinct gaps occurred, and how many events of re-warm-up remain
    # before entries are allowed again. Entries are refused while a gap is
    # unresolved or re-warm-up is outstanding: no trading on a tape with holes.
    analysis_events_skipped: int = 0
    causality_breaks: int = 0
    rewarm_remaining: int = 0
    # Market time the analysis window currently covers (the quantity that
    # gates evaluation; warmup fields above are also expressed in seconds).
    window_span_seconds: float = 0.0
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
        model_loader: object | None = None,
    ) -> None:
        """Create an engine that begins evaluating as soon as warm-up completes.

        It also owns a :class:`PaperExecutor`, so a qualifying setup automatically
        becomes a risk-checked, causally-filled simulated trade - no button.

        ``model_loader`` is optional (default ``None``) and structurally
        duck-typed: any object exposing ``.snapshot()`` (returning something
        with a ``.state``/``.artifact_id``) and ``.last_probability(direction)`` returning timestamped evidence
        (``success_probability``/``timestamp_ns``)
        works - matching ``app.machine_learning.shadow_predictor
        .ObserveOnlyModelLoader``'s public interface, without importing that
        module here. Only consulted when ``config.ml_decision_policy_enabled``
        is true, and always inside a broad try/except: a broken or absent
        loader can never stop or alter heuristic-only paper evaluation.
        """
        from app.paper.execution import PaperExecutor
        from app.risk.account_profile import load_selected_profile

        self._profile = account_profile or load_selected_profile()
        limits = self._profile.effective_limits()  # type: ignore[union-attr]
        self._config = config or EpisodeConfig()
        self._model_loader = model_loader
        from app.instruments import resolve_instrument

        self._instrument = resolve_instrument(self._config.instrument)
        # One ExecutionConfig drives both the executor and risk sizing, carrying
        # the dynamic-stop (break-even/trailing) settings from the config.
        self._exec_config = ExecutionConfig(
            break_even_trigger_ticks=self._config.break_even_trigger_ticks,
            break_even_lock_ticks=self._config.break_even_lock_ticks,
            trail_activation_ticks=self._config.trail_activation_ticks,
            trail_distance_ticks=self._config.trail_distance_ticks,
            max_entries_per_day=self._config.max_entries_per_day,
            max_losses_per_day=self._config.max_losses_per_day,
            fixed_contracts=self._config.fixed_contracts,
            max_risk_per_trade_usd=self._config.max_risk_per_trade_usd,
            min_reward_risk=self._config.min_reward_risk,
            entry_order_type=self._config.entry_order_type,
            entry_limit_offset_ticks=self._config.entry_limit_offset_ticks,
            entry_limit_timeout_ns=int(
                self._config.entry_limit_timeout_seconds * Decimal(1_000_000_000)),
            entry_limit_cancel_ticks=self._config.entry_limit_cancel_ticks,
            entry_require_trade_through=self._config.entry_require_trade_through,
            cooldown_ns=int(self._config.entry_cooldown_seconds * Decimal(1_000_000_000)),
            time_stop_ns=int(self._config.time_stop_seconds * Decimal(1_000_000_000)),
        )
        self._executor = PaperExecutor(
            starting_balance=self._profile.account_size,  # type: ignore[union-attr]
            max_contracts=limits.max_contracts,
            is_synthetic_fixture=is_synthetic_fixture,
            tick_value=self._instrument.tick_value,
            config=self._exec_config,
        )
        from app.strategy.profiles import thresholds_for_profile

        self._thresholds = thresholds or thresholds_for_profile(
            self._config.strategy_profile,
            tick_size=self._config.tick_size,
            large_block_minimum=self._config.large_block_minimum,
            absorption_volume_minimum=self._config.absorption_volume_minimum,
        )
        self._tracker = CausalLevelTracker()
        from app.strategy.causal_window import CausalWindow

        # Time-spanned, sampled window (the fix for both the 0.12s-window
        # zero-candidates defect AND the evaluation-cost GIL starvation).
        self._window = CausalWindow(
            span_seconds=self._config.window_span_seconds,
            sample_interval_ms=self._config.depth_sample_interval_ms,
        )
        self._last_evaluation_market_ns = 0
        self._state = MarketState()
        self._lock = threading.Lock()
        self._status = PaperEngineStatus(
            warmup_events_required=int(self._config.warmup_span_seconds),
            state=STATE_WAITING,
        )
        self._rejections: Counter[str] = Counter()
        # name -> [passes, failures, last evidence message] so the GUI can
        # show observed-vs-required values instead of bare counts.
        self._condition_stats: dict[str, list[object]] = {}
        self._event_index = 0
        self._decision_opportunities = 0
        self._evaluations: deque[EvaluationRecord] = deque(maxlen=500)
        self._gap_pending = False
        self._rewarm_remaining = 0
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
        # The optional momentum-continuation setup runs BESIDE the canonical
        # plan only when the user enabled it in production_config.yaml.
        from app.strategy.momentum import MomentumThresholds

        self._momentum_thresholds = MomentumThresholds()
        if not self._config.momentum_enabled:
            disabled.append(
                ("momentum-v1", "disabled by config (paper_momentum_setup_enabled: false)"),
            )
        self._status.momentum_enabled = self._config.momentum_enabled
        from app.strategy.profiles import normalize_profile

        self._status.strategy_profile = normalize_profile(self._config.strategy_profile)
        self._status.instrument = self._instrument.symbol
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
        self.ingest(event, None)

    def ingest(self, event: Mapping[str, object], state: object | None) -> None:
        """Advance with one event, reusing a receiver-built state when given.

        The receiver already computed the post-event ``MarketState`` on the
        capture path; recomputing it here duplicated an O(book) copy per event
        (a real MNQ book holds hundreds of levels). ``MarketState`` is
        immutable, so sharing the instance is safe.
        """
        if state is not None:
            self._state = state  # type: ignore[assignment]
        else:
            try:
                self._state = self._state.update(event)
            except Exception as error:  # noqa: BLE001 - a bad event must not kill capture
                with self._lock:
                    self._status.state = STATE_ERROR
                    self._status.error = f"{type(error).__name__}: {error}"
                    self._status.malformed_events += 1
                return

        self._event_index += 1
        self._window.observe(self._state, event_index=self._event_index)
        # A causality gap (the analysis feed skipped events) is resolved before
        # anything else: the open position cannot be honestly managed across a
        # hole in the tape, and the window must rebuild from gap-free data.
        if self._gap_pending:
            self._resolve_causality_gap()
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

        span = self._window.span_seconds
        warmup_span = self._config.warmup_span_seconds
        with self._lock:
            self._status.events_seen = self._event_index
            self._status.window_span_seconds = span
            self._status.warmup_events_seen = int(min(span, warmup_span))
            self._status.warmup_events_required = int(warmup_span)
            if span < warmup_span or len(self._window) < 2:
                self._status.state = STATE_WARMING
                self._status.rewarm_remaining = int(max(0.0, warmup_span - span))
                return
            self._status.state = STATE_EVALUATING
            self._status.rewarm_remaining = 0

        # Evaluation cadence is MARKET TIME, not an event count: at the real
        # ~1,331 ev/s an every-25-events stride ran ~53 Decimal-heavy window
        # scans per second and starved the recorder writer of the GIL.
        interval_ns = int(self._config.evaluation_interval_ms * 1e6)
        now_ns = self._state.timestamp_ns
        if interval_ns > 0:
            if now_ns - self._last_evaluation_market_ns < interval_ns:
                return
            self._last_evaluation_market_ns = now_ns
        else:
            self._decision_opportunities += 1
            if self._decision_opportunities % self._config.decision_stride != 0:
                return
        self._evaluate_now(tick)

    def notify_causality_gap(self, skipped: int) -> None:
        """Record that ``skipped`` events never reached analysis.

        Called by the analysis feed (from its own thread) when its bounded
        queue overflowed. Recording upstream is unaffected; PAPER must now stop
        trusting its window. Resolution happens on the next ingested event.
        """
        with self._lock:
            self._status.analysis_events_skipped += skipped
            self._status.causality_breaks += 1
        self._gap_pending = True

    def _resolve_causality_gap(self) -> None:
        """Close exposure, drop the holed window, and require a re-warm-up.

        A stop or target may have been touched inside the gap; managing the
        position onward would assign it an unknowable outcome, so it is closed
        at the first post-gap price and labelled DATA_GAP. A pending order is
        cancelled - its causal fill window contained the hole.
        """
        tick = self._tick()
        if self._executor.position is not None and tick is None:
            return  # no post-gap price yet; retry on the next event
        self._gap_pending = False
        if tick is not None and self._executor.position is not None:
            trade = self._executor.liquidate(tick, CloseReason.DATA_GAP)
            if trade is not None:
                self._closed_trades.append(trade)
                for callback in self._on_trade_closed:
                    try:
                        callback(trade)  # type: ignore[operator]
                    except Exception:  # noqa: BLE001,S110
                        pass
        self._executor.pending = None
        # The window contained the hole: rebuild from post-gap events only.
        # Clearing it re-engages the span-based warm-up gate, so no evaluation
        # (and no entry) can happen until a full gap-free span exists again.
        self._window.clear()
        self._window.observe(self._state, event_index=self._event_index)
        with self._lock:
            self._status.rewarm_remaining = int(self._config.warmup_span_seconds)
        self._sync_execution_status(tick)

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
        window = self._window.view()
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
        if self._config.momentum_enabled:
            self._evaluate_momentum(window, tick)

    def _evaluate_momentum(self, window: tuple[object, ...], tick: MarketTick | None) -> None:
        """Evaluate the OPTIONAL momentum setup beside (never instead of) the plan."""
        from app.strategy.momentum import (
            MOMENTUM_STRATEGY_VERSION,
            derive_momentum_context,
            evaluate_momentum_plan,
        )

        for direction in (TradeDirection.LONG, TradeDirection.SHORT):
            try:
                derived = derive_momentum_context(
                    window, direction, self._tracker, self._thresholds,
                    self._momentum_thresholds,
                    stop_buffer_points=self._config.stop_buffer_points,
                )
                evaluation = evaluate_momentum_plan(
                    window, derived, self._thresholds, self._momentum_thresholds,
                )
            except Exception as error:  # noqa: BLE001 - keep the stream alive
                with self._lock:
                    self._status.error = (
                        f"momentum evaluation error: {type(error).__name__}: {error}"
                    )
                continue
            self._record(
                direction, evaluation, derived, tick,
                setup_version=MOMENTUM_STRATEGY_VERSION,
            )

    def _record(
        self,
        direction: TradeDirection,
        evaluation: object,
        derived: object | None = None,
        tick: MarketTick | None = None,
        setup_version: str = STRATEGY_VERSION,
    ) -> None:
        conditions = tuple(
            ConditionResult(
                name=c.key,
                passed=c.passed,
                message=c.message,
                observed=_condition_evidence(c.key, c.passed, c.message)[0],
                required=_condition_evidence(c.key, c.passed, c.message)[1],
            )
            for c in evaluation.conditions  # type: ignore[attr-defined]
        )
        reasons = list(c.message for c in conditions if not c.passed)
        heuristic_accepted = bool(evaluation.accepted)  # type: ignore[attr-defined]
        (
            accepted,
            decision_source,
            confidence,
            model_version,
            model_prediction_id,
            model_artifact_sha256,
            fallback_reason,
        ) = self._apply_ml_policy(direction, heuristic_accepted)
        if decision_source == DECISION_SOURCE_BLENDED:
            reasons.append(
                f"ML veto: success_probability={confidence:.4f} < "
                f"{ML_VETO_PROBABILITY_THRESHOLD:.2f} (model {model_version})",
            )
        with self._lock:
            session_id = self._status.session_id
        record = EvaluationRecord(
            session_id=session_id,
            setup_id=f"{session_id}:{setup_version}:{direction.value}:{self._event_index}",
            strategy_version=setup_version,
            direction=direction.value,
            evaluated_at_ns=time.time_ns(),
            event_index=self._event_index,
            accepted=accepted,
            conditions=conditions,
            rejection_reasons=tuple(reasons),
            decision_source=decision_source,
            confidence=confidence,
            raw_model_output=(
                {"success_probability": confidence} if confidence is not None else None
            ),
            model_version=model_version,
            model_prediction_id=model_prediction_id,
            model_artifact_sha256=model_artifact_sha256,
            fallback_reason=fallback_reason,
        )
        self._evaluations.append(record)
        for condition in conditions:
            stats = self._condition_stats.setdefault(condition.name, [0, 0, ""])
            stats[0 if condition.passed else 1] += 1
            stats[2] = condition.message
            if not condition.passed:
                self._rejections[condition.name] += 1
        with self._lock:
            self._status.evaluations += 1
            self._status.last_setup = f"{setup_version}_{direction.value}"
            self._status.last_direction = direction.value
            self._status.last_decision = "accepted" if accepted else "rejected"
            self._status.last_reason = (
                "" if accepted else (record.first_failure or (reasons[-1] if reasons else ""))
            )
            self._status.last_evaluation_ns = record.evaluated_at_ns
            self._status.top_rejections = tuple(self._rejections.most_common(5))
            if accepted:
                self._status.accepted_setups += 1

        # An accepted setup is the ONLY thing that may produce a candidate.
        if accepted and derived is not None and tick is not None:
            self._consider_entry(record, derived, tick)

    def _apply_ml_policy(
        self,
        direction: TradeDirection,
        heuristic_accepted: bool,
    ) -> tuple[bool, str, float | None, str, str, str, str]:
        """Apply the veto-only ML decision policy; always fail-safe to HEURISTIC.

        Returns ``(accepted, decision_source, confidence, model_version,
        model_prediction_id, model_artifact_sha256, fallback_reason)``. Artifact
        and prediction identity always come atomically from the exact correlated
        evidence; the loader snapshot only gates on ``SCORING``. The model can
        only turn a heuristic ACCEPT into a REJECT (conservative veto); it never
        accepts a heuristic-rejected setup. Any missing, malformed, stale, or
        broken model evidence falls back to the unchanged heuristic outcome.
        """
        fallback = (
            heuristic_accepted,
            DECISION_SOURCE_FALLBACK,
            None,
            "",
            "",
            "",
        )
        if not self._config.ml_decision_policy_enabled:
            return (
                heuristic_accepted,
                DECISION_SOURCE_HEURISTIC,
                None,
                "",
                "",
                "",
                "policy disabled",
            )

        if self._model_loader is None:
            return (*fallback, "no model loader configured")

        try:
            snapshot = self._model_loader.snapshot()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a broken loader must never stop paper evaluation
            return (*fallback, "model loader snapshot failed")

        state = getattr(snapshot, "state", "")
        if state != "SCORING":
            return (*fallback, f"no approved model (state={state or 'unknown'})")

        try:
            evaluation_timestamp_ns = int(getattr(self._state, "timestamp_ns", 0))
            with self._lock:
                session_id = self._status.session_id
            evidence = self._model_loader.last_probability(  # type: ignore[attr-defined]
                direction.value,
                session_id=session_id,
                as_of_timestamp_ns=evaluation_timestamp_ns,
                max_age_ns=ML_PREDICTION_CORRELATION_WINDOW_NS,
            )
        except Exception:  # noqa: BLE001 - a broken loader must never stop paper evaluation
            evidence = None

        if evidence is None:
            return (*fallback, "no matching prediction within correlation window")

        # Every field below must come from the same immutable evidence object.
        # Missing identity is malformed evidence and cannot affect a decision.
        prediction_id = getattr(evidence, "prediction_id", None)
        artifact_id = getattr(evidence, "artifact_id", None)
        artifact_sha256 = getattr(evidence, "artifact_sha256", None)
        evidence_session_id = getattr(evidence, "session_id", None)
        evidence_direction = getattr(evidence, "direction", None)
        probability = getattr(evidence, "success_probability", None)
        prediction_timestamp_ns = getattr(evidence, "timestamp_ns", None)
        if (
            not prediction_id
            or not artifact_id
            or not artifact_sha256
            or evidence_session_id != session_id
            or evidence_direction != direction.value
            or probability is None
            or prediction_timestamp_ns is None
        ):
            return (*fallback, "prediction evidence missing or mismatched atomic identity")

        age_ns = evaluation_timestamp_ns - int(prediction_timestamp_ns)
        if age_ns < 0 or age_ns > ML_PREDICTION_CORRELATION_WINDOW_NS:
            return (*fallback, f"prediction outside correlation window (age_ns={age_ns})")
        probability = float(probability)
        identity = (str(artifact_id), str(prediction_id), str(artifact_sha256))

        if not heuristic_accepted:
            # Veto-only: the model never independently accepts a
            # heuristic-rejected setup - it can only tighten, never loosen.
            return (
                heuristic_accepted,
                DECISION_SOURCE_HEURISTIC,
                probability,
                *identity,
                "",
            )

        if probability < ML_VETO_PROBABILITY_THRESHOLD:
            return False, DECISION_SOURCE_BLENDED, probability, *identity, ""

        return True, DECISION_SOURCE_ML, probability, *identity, ""

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
        target = _widen_target(direction, tick.price, stop, target, self._config.target_reward_risk)
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
            tick_value=self._instrument.tick_value,
            fixed_contracts=self._exec_config.fixed_contracts,
            max_risk_per_trade_usd=self._exec_config.max_risk_per_trade_usd,
            min_reward_risk=self._exec_config.min_reward_risk,
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
                self._status.pending_order = _pending_order_text(pending)
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
                self._status.unrealized_pnl = str(
                    position.unrealized_pnl(mark, self._instrument.tick_value))

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

    def condition_stats(self) -> tuple[tuple[str, int, int, str], ...]:
        """Return (condition, passes, failures, last evidence), worst first.

        This is the honest answer to "why zero candidates": every condition
        shows how often it passed, how often it failed, and its latest
        observed-vs-required message.
        """
        rows = tuple(
            (name, int(stats[0]), int(stats[1]), str(stats[2]))
            for name, stats in self._condition_stats.items()
        )
        return tuple(sorted(rows, key=lambda row: row[2], reverse=True))

    def top_risk_rejections(self, limit: int = 5) -> tuple[tuple[str, int], ...]:
        """Return why candidates did not become orders (the honest zero-trade view)."""
        return tuple(self._risk_rejections.most_common(limit))


_REQUIREMENT_BY_CONDITION = {
    "clear_dol": "a clear direction-of-liquidity target",
    "clear_take_profit": "a target with sufficient tick room",
    "valid_stop_location": "an explicit stop beyond the defended block",
    "controlling_side_known": "a directional controlling side",
    "durable_defending_block": "a durable defending liquidity block",
    "defending_block_holds": "the defending block remains present",
    "reload_confirmed": "the configured minimum reload count",
    "defending_block_stable": "a stable, non-chasing block sequence",
    "absorption_confirmed": "opposite aggression absorbed within max price progress",
    "aggressive_side_failed": "aggression fails to break the defended block",
    "loading_confirmed": "the configured minimum loading liquidity",
    "continuation_confirmed": "control, follow-through, loading, and reaction all pass",
    "market_not_too_fast": "spread, volatility, and velocity within configured maxima",
    "entry_after_reaction": "minimum reaction snapshots and ticks",
}


def _condition_evidence(name: str, passed: bool, message: str) -> tuple[str, str]:
    """Split a rule explanation into explicit observed and required evidence."""
    match = re.match(r"^(.*?),\s*(requires|allows)\s*([<>]=?)?\s*(.+)$", message)
    if match is not None:
        observed = match.group(1).strip()
        operator = (match.group(3) or "").strip()
        value = match.group(4).strip()
        return observed, " ".join(part for part in (operator, value) if part)
    if passed:
        return message, "rule passed"
    if message.lower().startswith("no "):
        observed = "not detected"
    elif "too early" in message.lower() or "first touch" in message.lower():
        observed = message
    else:
        observed = message
    return observed, _REQUIREMENT_BY_CONDITION.get(name, "configured rule must pass")

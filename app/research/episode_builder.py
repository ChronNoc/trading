"""Build causal strategy episodes and labels from real Bookmap recordings.

The state machine evaluates the existing handwritten order-flow strategy from
past snapshots only.  Acceptance creates a pending signal; entry is attempted
on the next trade event, and stop/target/timeout resolution begins only after
that fill.  Pending outcomes keep one same-timestamp touch group, not future
price history, so memory remains bounded by the strategy window and number of
simultaneously pending setups.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Sequence

from app.market.features import calculate_short_term_volatility
from app.market.state import MarketState
from app.research.causal_context import (
    CausalLevelTracker,
    DerivedContext,
    derive_strategy_context,
    trading_day_for_timestamp,
)
from app.research.replay_loader import (
    ReplayEvent,
    ReplayStats,
    session_stream_paths,
    stream_session_events_with_stats,
)
from app.simulator.fills import FillAssumptions, SimulatedOrder, decide_fill
from app.simulator.slippage import SlippageModel, apply_slippage
from app.strategy.order_flow import (
    OrderFlowThresholds,
    TradeDirection,
    evaluate_day_trading_plan,
)

STRATEGY_VERSION = "order-flow-plan-v1"
BUILDER_VERSION = "real-episodes-v2"
_NS_PER_SECOND = 1_000_000_000
_REAL_PROVENANCES = frozenset({"REAL_DELAYED", "REAL_REPLAY", "REAL_REALTIME"})
_COMPLETED_OUTCOMES = frozenset({"target_first", "stop_first", "timeout_exit"})


@dataclass(frozen=True, slots=True)
class EpisodeConfig:
    """Visible execution and sampling assumptions for offline episodes."""

    tick_size: Decimal = Decimal("0.25")
    tick_value: Decimal = Decimal("0.50")
    stop_buffer_points: Decimal = Decimal("10")
    fixed_slippage_ticks: Decimal = Decimal("1")
    volatility_slippage_multiplier: Decimal = Decimal("0")
    commission_per_round_turn: Decimal = Decimal("1.24")
    queue_position_fraction: Decimal = Decimal("0")
    window_size: int = 200
    warmup_events: int = 100
    decision_stride: int = 25
    # --- time-based windowing (the real fix) ---------------------------------
    # The event-count window above spans ~0.12 SECONDS at the real ~1,331 ev/s
    # feed rate, which made every time-scale order-flow condition structurally
    # unsatisfiable (18,614 evaluations, zero passes). The strategy window is
    # now sized in market time and sampled; see app/strategy/causal_window.py
    # for the proof that sampling preserves the canonical strategy semantics.
    window_span_seconds: float = 180.0
    depth_sample_interval_ms: float = 250.0
    # Evaluate at most once per this much market time (was: every 25 events =
    # ~53 evaluations/sec at the real rate, whose Decimal-heavy window scans
    # starved the GIL and throttled the recorder writer).
    evaluation_interval_ms: float = 1000.0
    # Evaluation begins once the window covers this much market time.
    warmup_span_seconds: float = 60.0
    # Liquidity-block size thresholds. Canonical defaults (90 / 400) were set
    # against synthetic fixtures; the MEASURED real delayed MNQ book runs
    # ~20 levels/side with top sizes 13-63 in thin RTH stretches, so these are
    # candidate-learnable dimensions. Canonical values stay unchanged here;
    # only EXPERIMENTAL research candidates explore alternatives.
    large_block_minimum: Decimal = Decimal("90")
    absorption_volume_minimum: Decimal = Decimal("400")
    timeout_seconds: int = 900
    dedupe_price_ticks: Decimal = Decimal("4")
    dedupe_seconds: int = 300
    # Optional momentum-continuation setup evaluated BESIDE the canonical
    # order-flow plan (never instead of it). Ships disabled; the live paper
    # engine reads paper_momentum_setup_enabled from production_config.yaml.
    # Research episodes keep the canonical default unless a candidate says
    # otherwise explicitly.
    momentum_enabled: bool = False
    # Whether the live paper engine's decision may be VETOED by the observe-
    # only shadow model's most recent scored probability (conservative
    # veto-only policy; see app/paper/streaming_engine.py). Ships disabled;
    # the live paper engine reads paper_ml_decision_policy_enabled from
    # production_config.yaml. Research/replay has no model_loader to consult,
    # so this has no effect outside the live streaming engine.
    ml_decision_policy_enabled: bool = False
    # "canonical" (the honest RTH-only strategy) or "relaxed" (a research
    # profile with lower thresholds and no RTH gate, for per-session training).
    # See app/strategy/profiles.py. Research/replay defaults to canonical.
    strategy_profile: str = "canonical"
    # Contract whose dollar multiplier the paper P&L uses: "MNQ" ($2/pt) or
    # "NQ" ($20/pt). See app/instruments.py. Prices are identical; only the
    # dollars-per-point differ (NQ is 10x MNQ).
    instrument: str = "MNQ"
    # Dynamic stop management for the live paper engine (0 = off). Break-even
    # moves the stop to entry (+lock) after a favourable move; trailing then
    # follows price. These reduce risk on trades that work; they do not change
    # initial position sizing. See app/paper/execution.py.
    break_even_trigger_ticks: Decimal = Decimal("0")
    break_even_lock_ticks: Decimal = Decimal("0")
    trail_activation_ticks: Decimal = Decimal("0")
    trail_distance_ticks: Decimal = Decimal("0")
    # Daily activity caps for the live paper engine. The setup can fire far more
    # candidates than this; these limit how many BECOME trades per day. Raising
    # entries collects the >=100-setup evidence sample faster but is a deliberate
    # risk-loosening; the loss cap protects the account and should stay tight.
    max_entries_per_day: int = 3
    max_losses_per_day: int = 3
    # Fixed-size PAPER sizing (0 = disabled, keeps the dynamic risk sizing
    # above unchanged). A PER-TRADE risk cap, not a daily loss cap - see
    # paper_fixed_contracts / paper_max_risk_per_trade_usd in
    # config/production_config.yaml. The strategy's real stop is never
    # replaced; a stop implying more than max_risk_per_trade_usd at
    # fixed_contracts contracts causes the trade to be rejected instead of
    # resized. See app/paper/execution.py::size_intent.
    fixed_contracts: int = 0
    max_risk_per_trade_usd: Decimal = Decimal("0")
    # Minimum reward:risk a setup must offer to be taken (0 = disabled). Skips
    # setups whose target is too close to the stop; the real stop/target are never
    # altered. See app/paper/execution.py::size_intent and paper_min_reward_risk.
    min_reward_risk: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        """Validate all assumptions before replay starts."""
        positive_decimals = {
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
            "stop_buffer_points": self.stop_buffer_points,
            "dedupe_price_ticks": self.dedupe_price_ticks,
        }
        for name, value in positive_decimals.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.fixed_slippage_ticks < 0 or self.volatility_slippage_multiplier < 0:
            raise ValueError("slippage assumptions must be non-negative")
        if self.commission_per_round_turn < 0:
            raise ValueError("commission_per_round_turn must be non-negative")
        if not Decimal("0") <= self.queue_position_fraction < Decimal("1"):
            raise ValueError("queue_position_fraction must be in [0, 1)")
        for name, value in {
            "window_size": self.window_size,
            "warmup_events": self.warmup_events,
            "decision_stride": self.decision_stride,
            "timeout_seconds": self.timeout_seconds,
            "dedupe_seconds": self.dedupe_seconds,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
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

    @property
    def slippage_model(self) -> SlippageModel:
        """Return the shared simulator slippage model."""
        return SlippageModel(
            tick_size=self.tick_size,
            fixed_ticks=self.fixed_slippage_ticks,
            volatility_multiplier=self.volatility_slippage_multiplier,
        )


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    """Pure first-touch result used by focused unit tests."""

    outcome: str
    exit_price: Decimal
    exit_ts_ns: int
    r_multiple: Decimal


def resolve_outcome(
    *,
    direction: str,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    future_prices: Sequence[tuple[int, Decimal]],
    timeout_ns: int,
) -> OutcomeResult:
    """Resolve first touch from explicitly supplied post-entry prices."""
    if direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    if timeout_ns <= 0:
        raise ValueError("timeout_ns must be positive")
    if not future_prices:
        return OutcomeResult("unfinished", entry, 0, Decimal("0"))
    is_long = direction == "long"
    risk = entry - stop if is_long else stop - entry
    reward = target - entry if is_long else entry - target
    if risk <= 0 or reward <= 0:
        raise ValueError("stop and target must bracket entry in the selected direction")
    deadline = future_prices[0][0] + timeout_ns
    target_ts: int | None = None
    stop_ts: int | None = None
    for timestamp_ns, price in future_prices:
        if timestamp_ns > deadline:
            break
        if target_ts is None and (price >= target if is_long else price <= target):
            target_ts = timestamp_ns
        if stop_ts is None and (price <= stop if is_long else price >= stop):
            stop_ts = timestamp_ns
    if target_ts is not None and stop_ts is not None:
        if target_ts == stop_ts:
            return OutcomeResult("ambiguous", entry, target_ts, Decimal("0"))
        if target_ts < stop_ts:
            return OutcomeResult("target_first", target, target_ts, (reward / risk).quantize(Decimal("0.0001")))
        return OutcomeResult("stop_first", stop, stop_ts, Decimal("-1.0000"))
    if target_ts is not None:
        return OutcomeResult("target_first", target, target_ts, (reward / risk).quantize(Decimal("0.0001")))
    if stop_ts is not None:
        return OutcomeResult("stop_first", stop, stop_ts, Decimal("-1.0000"))
    for timestamp_ns, price in future_prices:
        if timestamp_ns >= deadline:
            raw_r = (price - entry) / risk if is_long else (entry - price) / risk
            return OutcomeResult("timeout_exit", price, timestamp_ns, raw_r.quantize(Decimal("0.0001")))
    return OutcomeResult("unfinished", entry, 0, Decimal("0"))


@dataclass(slots=True)
class DecisionAudit:
    """Every accepted or rejected deterministic strategy evaluation."""

    session_id: str
    setup_id: str
    provenance: str
    direction: str
    decision_ts_ns: int
    decision_event_index: int
    strategy_accepted: bool
    accepted: bool
    duplicate: bool
    checks: dict[str, bool]
    condition_messages: dict[str, str]
    reasons: list[str]
    context: dict[str, object]
    prefix_hash: str
    source_event_range: tuple[int, int]
    ordering_mode: str
    entry_status: str = "not_requested"
    outcome: str | None = None

    def record(self) -> dict[str, object]:
        """Return a stable JSON-compatible decision record."""
        return {
            **asdict(self),
            "source_event_range": list(self.source_event_range),
            "strategy_version": STRATEGY_VERSION,
            "builder_version": BUILDER_VERSION,
            "decision": "accepted" if self.accepted else "rejected",
        }


@dataclass(frozen=True, slots=True)
class BuiltEpisode:
    """One accepted setup from decision through a completed or excluded outcome."""

    session_id: str
    setup_id: str
    provenance: str
    direction: str
    trading_day: str
    decision_ts_ns: int
    entry_ts_ns: int
    exit_ts_ns: int | None
    defended_price: Decimal
    entry_reference_price: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit_reference_price: Decimal | None
    exit: Decimal | None
    commission: Decimal
    slippage_cost: Decimal | None
    gross_pnl_per_contract: Decimal | None
    net_pnl_per_contract: Decimal | None
    risk_per_contract: Decimal
    r_multiple: Decimal | None
    outcome: str
    strategy_version: str
    builder_version: str
    source_event_range: tuple[int, int]
    decision_hash: str
    input_hash: str
    ordering_mode: str
    ordering_ambiguous: bool
    data_quality_ok: bool

    @property
    def eligible_for_ledger(self) -> bool:
        """Return whether this episode can enter real paper performance."""
        return (
            self.provenance == "REAL_DELAYED"
            and self.outcome in _COMPLETED_OUTCOMES
            and not self.ordering_ambiguous
            and self.data_quality_ok
            and self.exit is not None
            and self.net_pnl_per_contract is not None
            and self.r_multiple is not None
        )

    def processed_record(self) -> dict[str, object]:
        """Return the complete lineage record written under data/processed."""
        return {
            "session_id": self.session_id,
            "setup_id": self.setup_id,
            "decision": "accepted",
            "strategy_accepted": True,
            "provenance": self.provenance,
            "direction": self.direction,
            "trading_day": self.trading_day,
            "decision_ts_ns": self.decision_ts_ns,
            "entry_ts_ns": self.entry_ts_ns,
            "exit_ts_ns": self.exit_ts_ns,
            "defended_price": str(self.defended_price),
            "entry_reference_price": str(self.entry_reference_price),
            "entry": str(self.entry),
            "stop": str(self.stop),
            "target": str(self.target),
            "exit_reference_price": _decimal_text(self.exit_reference_price),
            "exit": _decimal_text(self.exit),
            "commission": str(self.commission),
            "slippage_cost": _decimal_text(self.slippage_cost),
            "gross_pnl_per_contract": _decimal_text(self.gross_pnl_per_contract),
            "net_pnl_per_contract": _decimal_text(self.net_pnl_per_contract),
            "risk_per_contract": str(self.risk_per_contract),
            "r_multiple": _decimal_text(self.r_multiple),
            "outcome": self.outcome,
            "strategy_version": self.strategy_version,
            "builder_version": self.builder_version,
            "source_event_range": list(self.source_event_range),
            "decision_hash": self.decision_hash,
            "input_hash": self.input_hash,
            "ordering_mode": self.ordering_mode,
            "ordering_ambiguous": self.ordering_ambiguous,
            "data_quality_ok": self.data_quality_ok,
            "eligible_for_ledger": self.eligible_for_ledger,
        }

    def label_record(self) -> dict[str, object]:
        """Return a supervised label only for unambiguous target/stop outcomes."""
        eligible = (
            self.provenance in _REAL_PROVENANCES
            and self.outcome in {"target_first", "stop_first"}
            and not self.ordering_ambiguous
            and self.data_quality_ok
        )
        return {
            "session_id": self.session_id,
            "setup_id": self.setup_id,
            "decision": "accepted",
            "provenance": self.provenance,
            "direction": self.direction,
            "trading_day": self.trading_day,
            "decision_ts_ns": self.decision_ts_ns,
            "entry_ts_ns": self.entry_ts_ns,
            "exit_ts_ns": self.exit_ts_ns,
            "outcome": self.outcome,
            "label": 1 if eligible and self.outcome == "target_first" else (0 if eligible else None),
            "r_multiple": _decimal_text(self.r_multiple),
            "source_event_range": list(self.source_event_range),
            "decision_hash": self.decision_hash,
            "input_hash": self.input_hash,
            "strategy_version": self.strategy_version,
            "builder_version": self.builder_version,
            "ordering_mode": self.ordering_mode,
            "ordering_ambiguous": self.ordering_ambiguous,
            "data_quality_ok": self.data_quality_ok,
            "eligible_for_training": eligible,
        }


@dataclass(slots=True)
class BuildResult:
    """Honest per-session episode-build summary."""

    session_id: str
    provenance: str
    events: int = 0
    evaluations: int = 0
    accepted_candidates: int = 0
    duplicate_candidates: int = 0
    entry_rejections: int = 0
    completed: int = 0
    ambiguous: int = 0
    unfinished: int = 0
    same_timestamp_collisions: int = 0
    ordering_mode: str = "timestamp_fallback"
    ordering_ambiguous: bool = False
    data_quality_ok: bool = True
    replay_quality: dict[str, object] = field(default_factory=dict)
    source_file_hashes: dict[str, str] = field(default_factory=dict)
    rejected_condition_tally: dict[str, int] = field(default_factory=dict)
    decisions: tuple[DecisionAudit, ...] = ()
    episodes: tuple[BuiltEpisode, ...] = ()

    @property
    def ledger_eligible_count(self) -> int:
        """Count completed episodes allowed into the fixed real ledger."""
        return sum(1 for episode in self.episodes if episode.eligible_for_ledger)


@dataclass(slots=True)
class _Pending:
    setup_id: str
    direction: str
    defended_price: Decimal
    stop: Decimal
    target: Decimal
    decision_ts_ns: int
    decision_event_index: int
    start_event_index: int
    decision_hash: str
    source_hasher: Any
    audit_index: int
    awaiting_entry: bool = True
    entry_reference_price: Decimal = Decimal("0")
    entry: Decimal = Decimal("0")
    entry_ts_ns: int = 0
    entry_event_index: int = 0
    deadline_ns: int = 0
    touch_timestamp_ns: int | None = None
    touch_event_index: int = 0
    target_touched: bool = False
    stop_touched: bool = False
    last_trade_price: Decimal | None = None
    last_event_index: int = 0


def build_episodes(
    session_dir: Path,
    *,
    session_id: str,
    provenance: str,
    config: EpisodeConfig | None = None,
    thresholds: OrderFlowThresholds | None = None,
    level_tracker: CausalLevelTracker | None = None,
) -> BuildResult:
    """Stream one session through strategy, entry, and outcome state machines."""
    cfg = config or EpisodeConfig()
    thr = thresholds or OrderFlowThresholds(
        tick_size=cfg.tick_size,
        large_block_minimum=cfg.large_block_minimum,
        absorption_volume_minimum=cfg.absorption_volume_minimum,
    )
    tracker = level_tracker or CausalLevelTracker()
    result = BuildResult(session_id=session_id, provenance=provenance)
    result.source_file_hashes = _source_hashes(session_dir)

    events_iter, replay_stats = stream_session_events_with_stats(session_dir)
    result.ordering_mode = replay_stats.ordering_mode
    from app.strategy.causal_window import CausalWindow

    state = MarketState()
    # The SAME time-spanned sampled window the streaming engine uses, so
    # replay and streaming cannot drift into two different strategies.
    window = CausalWindow(
        span_seconds=cfg.window_span_seconds,
        sample_interval_ms=cfg.depth_sample_interval_ms,
    )
    last_evaluation_market_ns = 0
    pending: list[_Pending] = []
    completed: list[BuiltEpisode] = []
    decisions: list[DecisionAudit] = []
    last_accepted: dict[tuple[str, str], int] = {}
    stream_hasher = hashlib.sha256()
    event_index = 0
    decision_opportunities = 0

    for event in events_iter:
        event_index += 1
        canonical = _canonical_event_bytes(event)
        stream_hasher.update(canonical)
        state = state.update(event.payload)
        window.observe(state, event_index=event_index)
        reference = _reference_price(state)
        tracker.observe(event.timestamp_ns, reference)

        _advance_pending(
            pending,
            completed,
            decisions,
            result,
            event,
            event_index,
            canonical,
            state,
            window.view(),
            cfg,
        )

        if event_index < cfg.warmup_events or not _is_decision_opportunity(event, thr):
            continue
        if window.span_seconds < cfg.warmup_span_seconds or len(window) < 2:
            continue
        # Market-time evaluation cadence, identical to the streaming engine.
        interval_ns = int(cfg.evaluation_interval_ms * 1e6)
        if interval_ns > 0:
            if event.timestamp_ns - last_evaluation_market_ns < interval_ns:
                continue
            last_evaluation_market_ns = event.timestamp_ns
        else:
            decision_opportunities += 1
            if decision_opportunities % cfg.decision_stride != 0:
                continue

        for direction in (TradeDirection.LONG, TradeDirection.SHORT):
            derived = derive_strategy_context(
                window.view(),
                direction,
                tracker,
                thr,
                stop_buffer_points=cfg.stop_buffer_points,
            )
            evaluation = evaluate_day_trading_plan(window.view(), derived.context, thr)
            result.evaluations += 1
            setup_id = f"{session_id}:{direction.value}:{event_index}"
            strategy_accepted = evaluation.accepted
            accepted = strategy_accepted
            duplicate = False
            checks = {condition.key: condition.passed for condition in evaluation.conditions}
            messages = {condition.key: condition.message for condition in evaluation.conditions}
            reasons = [condition.message for condition in evaluation.conditions if not condition.passed]
            for condition in evaluation.conditions:
                if not condition.passed:
                    result.rejected_condition_tally[condition.key] = (
                        result.rejected_condition_tally.get(condition.key, 0) + 1
                    )

            defended = derived.context.defended_level_price
            if accepted and defended is not None:
                dedupe_key = (direction.value, str(_dedupe_bucket(defended, cfg)))
                previous_ts = last_accepted.get(dedupe_key)
                if previous_ts is not None and event.timestamp_ns - previous_ts < cfg.dedupe_seconds * _NS_PER_SECOND:
                    accepted = False
                    duplicate = True
                    checks["deduplicated_setup"] = False
                    messages["deduplicated_setup"] = "Duplicate setup inside debounce window"
                    reasons.append(messages["deduplicated_setup"])
                    result.duplicate_candidates += 1
                    result.rejected_condition_tally["deduplicated_setup"] = (
                        result.rejected_condition_tally.get("deduplicated_setup", 0) + 1
                    )
                else:
                    last_accepted[dedupe_key] = event.timestamp_ns

            audit = DecisionAudit(
                session_id=session_id,
                setup_id=setup_id,
                provenance=provenance,
                direction=direction.value,
                decision_ts_ns=event.timestamp_ns,
                decision_event_index=event_index,
                strategy_accepted=strategy_accepted,
                accepted=accepted,
                duplicate=duplicate,
                checks=checks,
                condition_messages=messages,
                reasons=reasons,
                context=derived.evidence(),
                prefix_hash=stream_hasher.copy().hexdigest(),
                source_event_range=(max(1, window.info().first_event_index), event_index),
                ordering_mode=replay_stats.ordering_mode,
            )
            decisions.append(audit)
            if not accepted:
                continue
            if defended is None or derived.context.stop_price is None or derived.context.target_price is None:
                raise RuntimeError("accepted strategy result lacked defended, stop, or target context")
            result.accepted_candidates += 1
            audit.entry_status = "awaiting_next_trade"
            pending.append(
                _Pending(
                    setup_id=setup_id,
                    direction=direction.value,
                    defended_price=defended,
                    stop=derived.context.stop_price,
                    target=derived.context.target_price,
                    decision_ts_ns=event.timestamp_ns,
                    decision_event_index=event_index,
                    start_event_index=audit.source_event_range[0],
                    decision_hash=audit.prefix_hash,
                    source_hasher=stream_hasher.copy(),
                    audit_index=len(decisions) - 1,
                ),
            )

    _finish_pending_at_session_end(pending, completed, decisions, result, event_index, cfg)
    _apply_replay_quality(result, completed, replay_stats)
    result.events = replay_stats.depth_events + replay_stats.trade_events
    result.same_timestamp_collisions = replay_stats.same_timestamp_collisions
    result.decisions = tuple(decisions)
    return result


def _advance_pending(
    pending: list[_Pending],
    completed: list[BuiltEpisode],
    decisions: list[DecisionAudit],
    result: BuildResult,
    event: ReplayEvent,
    event_index: int,
    canonical: bytes,
    state: MarketState,
    window: Sequence[MarketState],
    cfg: EpisodeConfig,
) -> None:
    remaining: list[_Pending] = []
    for item in pending:
        item.source_hasher.update(canonical)
        item.last_event_index = event_index
        if item.awaiting_entry:
            if event.kind != "trade" or event_index <= item.decision_event_index:
                remaining.append(item)
                continue
            if _attempt_entry(item, decisions[item.audit_index], state, event, event_index, window, cfg):
                remaining.append(item)
            else:
                result.entry_rejections += 1
            continue

        if event.kind != "trade":
            remaining.append(item)
            continue
        trade_price = Decimal(str(event.payload["price"]))
        resolved = _observe_trade_touch(
            item,
            event.timestamp_ns,
            trade_price,
            event_index,
            result.ordering_mode,
        )
        if resolved is None:
            remaining.append(item)
            continue
        outcome, outcome_price, outcome_ts_ns, outcome_event_index = resolved
        _complete_pending(
            item,
            outcome,
            outcome_price,
            outcome_ts_ns,
            outcome_event_index,
            completed,
            decisions,
            result,
            cfg,
        )
    pending[:] = remaining


def _attempt_entry(
    item: _Pending,
    audit: DecisionAudit,
    state: MarketState,
    event: ReplayEvent,
    event_index: int,
    window: Sequence[MarketState],
    cfg: EpisodeConfig,
) -> bool:
    side = "buy" if item.direction == "long" else "sell"
    limit_price = state.best_ask if side == "buy" else state.best_bid
    if limit_price is None:
        audit.entry_status = "rejected_no_usable_book"
        audit.reasons.append("next trade arrived without a usable opposite book")
        return False
    order = SimulatedOrder(
        order_id=item.setup_id,
        symbol=str(event.payload.get("instrument", "MNQ")),
        side=side,
        quantity=1,
        remaining_quantity=1,
        limit_price=limit_price,
        submitted_timestamp_ns=item.decision_ts_ns,
    )
    fill = decide_fill(order, state, FillAssumptions(cfg.queue_position_fraction))
    if fill.status != "filled" or fill.fill_price is None:
        audit.entry_status = f"rejected_{fill.status}"
        audit.reasons.append(fill.reason)
        return False
    volatility = calculate_short_term_volatility(window)
    actual_entry = apply_slippage(fill.fill_price, side, cfg.slippage_model, volatility)
    valid_bracket = (
        item.stop < actual_entry < item.target
        if item.direction == "long"
        else item.target < actual_entry < item.stop
    )
    if not valid_bracket:
        audit.entry_status = "rejected_fill_outside_bracket"
        audit.reasons.append("slipped entry was not between the fixed stop and target")
        return False
    item.awaiting_entry = False
    item.entry_reference_price = fill.fill_price
    item.entry = actual_entry
    item.entry_ts_ns = event.timestamp_ns
    item.entry_event_index = event_index
    item.deadline_ns = event.timestamp_ns + cfg.timeout_seconds * _NS_PER_SECOND
    audit.entry_status = "filled"
    return True


def _observe_trade_touch(
    item: _Pending,
    timestamp_ns: int,
    price: Decimal,
    event_index: int,
    ordering_mode: str,
) -> tuple[str, Decimal, int, int] | None:
    if ordering_mode == "receive_sequence":
        if timestamp_ns > item.deadline_ns:
            return "timeout_exit", price, timestamp_ns, event_index
        target_touched = price >= item.target if item.direction == "long" else price <= item.target
        stop_touched = price <= item.stop if item.direction == "long" else price >= item.stop
        if target_touched:
            return "target_first", price, timestamp_ns, event_index
        if stop_touched:
            return "stop_first", price, timestamp_ns, event_index
        item.last_trade_price = price
        item.touch_timestamp_ns = timestamp_ns
        item.touch_event_index = event_index
        return None

    if item.touch_timestamp_ns is not None and timestamp_ns != item.touch_timestamp_ns:
        prior = _touch_outcome(item)
        if prior is not None:
            return (
                prior,
                item.last_trade_price or item.entry,
                item.touch_timestamp_ns,
                item.touch_event_index,
            )
        item.target_touched = False
        item.stop_touched = False
        item.touch_timestamp_ns = None

    if timestamp_ns > item.deadline_ns:
        return "timeout_exit", price, timestamp_ns, event_index
    if item.touch_timestamp_ns is None:
        item.touch_timestamp_ns = timestamp_ns
        item.touch_event_index = event_index
    else:
        item.touch_event_index = event_index
    item.last_trade_price = price
    if item.direction == "long":
        item.target_touched = item.target_touched or price >= item.target
        item.stop_touched = item.stop_touched or price <= item.stop
    else:
        item.target_touched = item.target_touched or price <= item.target
        item.stop_touched = item.stop_touched or price >= item.stop
    return None


def _touch_outcome(item: _Pending) -> str | None:
    if item.target_touched and item.stop_touched:
        return "ambiguous"
    if item.target_touched:
        return "target_first"
    if item.stop_touched:
        return "stop_first"
    return None


def _finish_pending_at_session_end(
    pending: list[_Pending],
    completed: list[BuiltEpisode],
    decisions: list[DecisionAudit],
    result: BuildResult,
    event_index: int,
    cfg: EpisodeConfig,
) -> None:
    for item in pending:
        audit = decisions[item.audit_index]
        if item.awaiting_entry:
            audit.entry_status = "unfilled_session_end"
            audit.outcome = "unfinished"
            result.unfinished += 1
            continue
        touch = _touch_outcome(item)
        if touch is not None:
            _complete_pending(
                item,
                touch,
                item.last_trade_price or item.entry,
                item.touch_timestamp_ns or item.entry_ts_ns,
                item.touch_event_index or event_index,
                completed,
                decisions,
                result,
                cfg,
            )
            continue
        if item.touch_timestamp_ns is not None and item.touch_timestamp_ns >= item.deadline_ns:
            _complete_pending(
                item,
                "timeout_exit",
                item.last_trade_price or item.entry,
                item.touch_timestamp_ns,
                item.touch_event_index or event_index,
                completed,
                decisions,
                result,
                cfg,
            )
            continue
        _complete_pending(
            item,
            "unfinished",
            item.entry,
            item.entry_ts_ns,
            event_index,
            completed,
            decisions,
            result,
            cfg,
        )


def _complete_pending(
    item: _Pending,
    outcome: str,
    observed_price: Decimal,
    exit_ts_ns: int,
    event_index: int,
    completed: list[BuiltEpisode],
    decisions: list[DecisionAudit],
    result: BuildResult,
    cfg: EpisodeConfig,
) -> None:
    decisions[item.audit_index].outcome = outcome
    if outcome == "ambiguous":
        result.ambiguous += 1
        exit_reference = None
    elif outcome == "unfinished":
        result.unfinished += 1
        exit_reference = None
    else:
        result.completed += 1
        exit_reference = item.target if outcome == "target_first" else item.stop if outcome == "stop_first" else observed_price
    completed.append(_make_episode(item, outcome, exit_reference, exit_ts_ns, event_index, result, cfg))


def _make_episode(
    item: _Pending,
    outcome: str,
    exit_reference: Decimal | None,
    exit_ts_ns: int,
    event_index: int,
    result: BuildResult,
    cfg: EpisodeConfig,
) -> BuiltEpisode:
    risk = _risk_per_contract(item, cfg)
    if exit_reference is None:
        actual_exit = None
        slippage_cost = None
        gross = None
        net = None
        r_multiple = None
        completed_exit_ts: int | None = None
    else:
        exit_side = "sell" if item.direction == "long" else "buy"
        actual_exit = apply_slippage(exit_reference, exit_side, cfg.slippage_model)
        gross = _directional_dollars(
            item.direction,
            item.entry_reference_price,
            exit_reference,
            cfg,
        )
        net_before_commission = _directional_dollars(item.direction, item.entry, actual_exit, cfg)
        net = net_before_commission - cfg.commission_per_round_turn
        slippage_cost = gross - net_before_commission
        r_multiple = (net / risk).quantize(Decimal("0.0001"))
        completed_exit_ts = exit_ts_ns
    return BuiltEpisode(
        session_id=result.session_id,
        setup_id=item.setup_id,
        provenance=result.provenance,
        direction=item.direction,
        trading_day=trading_day_for_timestamp(item.decision_ts_ns),
        decision_ts_ns=item.decision_ts_ns,
        entry_ts_ns=item.entry_ts_ns,
        exit_ts_ns=completed_exit_ts,
        defended_price=item.defended_price,
        entry_reference_price=item.entry_reference_price,
        entry=item.entry,
        stop=item.stop,
        target=item.target,
        exit_reference_price=exit_reference,
        exit=actual_exit,
        commission=cfg.commission_per_round_turn,
        slippage_cost=slippage_cost,
        gross_pnl_per_contract=gross,
        net_pnl_per_contract=net,
        risk_per_contract=risk,
        r_multiple=r_multiple,
        outcome=outcome,
        strategy_version=STRATEGY_VERSION,
        builder_version=BUILDER_VERSION,
        source_event_range=(item.start_event_index, item.last_event_index or event_index),
        decision_hash=item.decision_hash,
        input_hash=item.source_hasher.copy().hexdigest(),
        ordering_mode=result.ordering_mode,
        ordering_ambiguous=False,
        data_quality_ok=True,
    )


def _risk_per_contract(item: _Pending, cfg: EpisodeConfig) -> Decimal:
    exit_side = "sell" if item.direction == "long" else "buy"
    slipped_stop = apply_slippage(item.stop, exit_side, cfg.slippage_model)
    loss_before_commission = -_directional_dollars(item.direction, item.entry, slipped_stop, cfg)
    return max(loss_before_commission + cfg.commission_per_round_turn, Decimal("0.01"))


def _directional_dollars(
    direction: str,
    entry: Decimal,
    exit_price: Decimal,
    cfg: EpisodeConfig,
) -> Decimal:
    points = exit_price - entry if direction == "long" else entry - exit_price
    return (points / cfg.tick_size * cfg.tick_value).quantize(Decimal("0.0001"))


def _apply_replay_quality(
    result: BuildResult,
    episodes: list[BuiltEpisode],
    stats: ReplayStats,
) -> None:
    result.ordering_ambiguous = stats.ordering_ambiguous
    result.data_quality_ok = stats.continuity_ok
    result.replay_quality = {
        "ordering_mode": stats.ordering_mode,
        "receive_order_available": stats.receive_order_available,
        "same_timestamp_collisions": stats.same_timestamp_collisions,
        "timestamp_regressions": stats.timestamp_regressions,
        "trade_sequence_gaps": stats.trade_sequence_gaps,
        "missed_trade_events": stats.missed_trade_events,
        "nonmonotonic_trade_sequences": stats.nonmonotonic_trade_sequences,
        "receive_sequence_gaps": stats.receive_sequence_gaps,
        "missed_receive_events": stats.missed_receive_events,
        "nonmonotonic_receive_sequences": stats.nonmonotonic_receive_sequences,
        "continuity_ok": stats.continuity_ok,
    }
    result.episodes = tuple(
        replace(
            episode,
            ordering_ambiguous=stats.ordering_ambiguous,
            data_quality_ok=stats.continuity_ok,
        )
        for episode in episodes
    )


def _is_decision_opportunity(event: ReplayEvent, thresholds: OrderFlowThresholds) -> bool:
    if event.kind == "trade":
        return True
    previous = Decimal(str(event.payload.get("previous_size", "0")))
    current = Decimal(str(event.payload.get("new_size", "0")))
    return previous < thresholds.large_block_minimum <= current


def _dedupe_bucket(price: Decimal, cfg: EpisodeConfig) -> Decimal:
    """Return a tick-size-correct bucket for duplicate setup suppression."""
    step = cfg.dedupe_price_ticks * cfg.tick_size
    units = (price / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def _reference_price(state: MarketState) -> Decimal | None:
    if state.mid_price is not None:
        return state.mid_price
    if state.best_bid is not None:
        return state.best_bid
    return state.best_ask


def _canonical_event_bytes(event: ReplayEvent) -> bytes:
    payload = {
        "kind": event.kind,
        "timestamp_ns": event.timestamp_ns,
        "receive_sequence": event.receive_sequence,
        "payload": event.payload,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def _source_hashes(session_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in session_stream_paths(session_dir):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        hashes[path.relative_to(session_dir).as_posix()] = digest.hexdigest()
    return hashes


def write_episode_artifacts(
    result: BuildResult,
    *,
    processed_root: Path,
    labels_root: Path,
) -> tuple[Path, Path]:
    """Atomically write decisions, episodes, labels, and a build summary."""
    processed_root.mkdir(parents=True, exist_ok=True)
    labels_root.mkdir(parents=True, exist_ok=True)
    decisions_path = processed_root / f"{result.session_id}.decisions.jsonl"
    episodes_path = processed_root / f"{result.session_id}.episodes.jsonl"
    labels_path = labels_root / f"{result.session_id}.labels.jsonl"
    summary_path = processed_root / f"{result.session_id}.build.json"
    _atomic_write_jsonl(decisions_path, [decision.record() for decision in result.decisions])
    _atomic_write_jsonl(episodes_path, [episode.processed_record() for episode in result.episodes])
    _atomic_write_jsonl(labels_path, [episode.label_record() for episode in result.episodes])
    summary = {
        "session_id": result.session_id,
        "provenance": result.provenance,
        "builder_version": BUILDER_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "events": result.events,
        "evaluations": result.evaluations,
        "accepted_candidates": result.accepted_candidates,
        "duplicate_candidates": result.duplicate_candidates,
        "entry_rejections": result.entry_rejections,
        "completed": result.completed,
        "ambiguous": result.ambiguous,
        "unfinished": result.unfinished,
        "ledger_eligible": result.ledger_eligible_count,
        "ordering_ambiguous": result.ordering_ambiguous,
        "data_quality_ok": result.data_quality_ok,
        "replay_quality": result.replay_quality,
        "source_file_hashes": result.source_file_hashes,
        "rejected_condition_tally": result.rejected_condition_tally,
    }
    _atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return episodes_path, labels_path


def _atomic_write_jsonl(path: Path, records: Sequence[dict[str, object]]) -> None:
    text = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)

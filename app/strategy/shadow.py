"""Shadow-mode strategy pipeline with no real execution imports."""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from app.database.logging import DecisionLogEntry, append_decision_log_entry
from app.market.features import MarketFeatures, compute_market_features
from app.market.state import MarketState
from app.strategy.setups import SetupEvaluationResult

ShadowLogMode = Literal["observe", "simulate"]
Direction = Literal["long", "short"]


class ShadowStrategyEngine(Protocol):
    """Protocol for strategy engines that can run inside shadow mode."""

    def __call__(
        self,
        market_state: MarketState,
        feature_window: tuple[MarketState, ...],
        features: MarketFeatures,
    ) -> "ShadowStrategyDecision | None":
        """Return a strategy decision for the current state, or None."""


class ShadowRiskEngine(Protocol):
    """Protocol for risk engines used by shadow mode."""

    def __call__(
        self,
        order_intent: "ShadowOrderIntent",
        market_state: MarketState,
        features: MarketFeatures,
    ) -> "ShadowRiskDecision":
        """Return whether the shadow order intent would be allowed."""


@dataclass(frozen=True, slots=True)
class ShadowOrderIntent:
    """Order intent that shadow mode may record but never submit to a broker."""

    symbol: str
    direction: Direction
    quantity: int
    requested_price: Decimal
    strategy_version: str
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowStrategyDecision:
    """Strategy output used for decision logging and optional shadow submission."""

    setup_result: SetupEvaluationResult
    order_intent: ShadowOrderIntent | None


@dataclass(frozen=True, slots=True)
class ShadowRiskDecision:
    """Risk output used by shadow mode before a no-op submission is recorded."""

    allowed: bool
    reason: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShadowSubmissionRecord:
    """No-op record of an order that shadow mode would have submitted."""

    timestamp_ns: int
    intent: ShadowOrderIntent
    message: str


@dataclass(frozen=True, slots=True)
class ShadowPipelineConfig:
    """Configuration for rolling features and decision-log metadata."""

    feature_window_size: int = 50
    bid_reload_level: int = 1
    bid_reload_threshold: Decimal = Decimal("1")
    log_mode: ShadowLogMode = "observe"


@dataclass(frozen=True, slots=True)
class ShadowPipelineResult:
    """Result of running a shadow pipeline over an event stream."""

    final_state: MarketState
    events_processed: int
    decisions_logged: int
    submissions: tuple[ShadowSubmissionRecord, ...]


@dataclass(slots=True)
class NoOpShadowExecutionStub:
    """No-op execution sink that records would-have-submitted messages only."""

    submissions: list[ShadowSubmissionRecord]

    def __init__(self) -> None:
        """Create an empty no-op shadow execution sink."""
        self.submissions = []

    def record_submission(self, *, timestamp_ns: int, intent: ShadowOrderIntent) -> ShadowSubmissionRecord:
        """Record the order that shadow mode would have submitted."""
        if intent.quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        message = (
            f"would have submitted {intent.quantity} {intent.direction} "
            f"{intent.symbol} at {intent.requested_price}"
        )
        record = ShadowSubmissionRecord(timestamp_ns=timestamp_ns, intent=intent, message=message)
        self.submissions.append(record)
        return record


def run_shadow_pipeline(
    events: Iterable[Mapping[str, object]],
    *,
    strategy_engine: ShadowStrategyEngine,
    risk_engine: ShadowRiskEngine,
    decision_log_path: str | Path,
    execution_stub: NoOpShadowExecutionStub | None = None,
    config: ShadowPipelineConfig | None = None,
    initial_state: MarketState | None = None,
) -> ShadowPipelineResult:
    """Run market events through state, features, strategy, risk, and no-op execution."""
    shadow_config = _validated_config(config or ShadowPipelineConfig())
    shadow_execution = execution_stub or NoOpShadowExecutionStub()
    state = initial_state or MarketState()
    feature_window: list[MarketState] = []
    events_processed = 0
    decisions_logged = 0

    for event in events:
        state = state.update(event)
        feature_window.append(state)
        if len(feature_window) > shadow_config.feature_window_size:
            feature_window = feature_window[-shadow_config.feature_window_size :]

        features = compute_market_features(
            tuple(feature_window),
            bid_reload_level=shadow_config.bid_reload_level,
            bid_reload_threshold=shadow_config.bid_reload_threshold,
        )
        strategy_decision = strategy_engine(state, tuple(feature_window), features)
        _handle_shadow_decision(
            state=state,
            feature_window=tuple(feature_window),
            features=features,
            strategy_decision=strategy_decision,
            risk_engine=risk_engine,
            execution_stub=shadow_execution,
            decision_log_path=decision_log_path,
            log_mode=shadow_config.log_mode,
        )
        events_processed += 1
        decisions_logged += 1

    return ShadowPipelineResult(
        final_state=state,
        events_processed=events_processed,
        decisions_logged=decisions_logged,
        submissions=tuple(shadow_execution.submissions),
    )


async def run_shadow_pipeline_async(
    events: AsyncIterable[Mapping[str, object]],
    *,
    strategy_engine: ShadowStrategyEngine,
    risk_engine: ShadowRiskEngine,
    decision_log_path: str | Path,
    execution_stub: NoOpShadowExecutionStub | None = None,
    config: ShadowPipelineConfig | None = None,
    initial_state: MarketState | None = None,
) -> ShadowPipelineResult:
    """Run an async live-style event stream through the shadow pipeline."""
    buffered_events: list[Mapping[str, object]] = []
    async for event in events:
        buffered_events.append(event)
    return run_shadow_pipeline(
        buffered_events,
        strategy_engine=strategy_engine,
        risk_engine=risk_engine,
        decision_log_path=decision_log_path,
        execution_stub=execution_stub,
        config=config,
        initial_state=initial_state,
    )


def _handle_shadow_decision(
    *,
    state: MarketState,
    feature_window: tuple[MarketState, ...],
    features: MarketFeatures,
    strategy_decision: ShadowStrategyDecision | None,
    risk_engine: ShadowRiskEngine,
    execution_stub: NoOpShadowExecutionStub,
    decision_log_path: str | Path,
    log_mode: ShadowLogMode,
) -> None:
    if strategy_decision is None:
        append_decision_log_entry(
            decision_log_path,
            _decision_log_entry(
                timestamp_ns=state.timestamp_ns,
                mode=log_mode,
                symbol=_symbol_from_state_or_intent(None),
                direction="long",
                strategy_version="shadow-no-signal",
                model_version=None,
                context=_feature_context(features, feature_window),
                checks={},
                decision="rejected",
                reason=("No setup detected.",),
            ),
        )
        return

    intent = strategy_decision.order_intent
    setup_result = strategy_decision.setup_result
    checks = {condition.key: condition.passed for condition in setup_result.conditions}
    failed_reasons = tuple(
        condition.message for condition in setup_result.conditions if not condition.passed
    )

    if not setup_result.accepted or intent is None:
        append_decision_log_entry(
            decision_log_path,
            _decision_log_entry(
                timestamp_ns=state.timestamp_ns,
                mode=log_mode,
                symbol=_symbol_from_state_or_intent(intent),
                direction=intent.direction if intent is not None else "long",
                strategy_version=intent.strategy_version if intent is not None else setup_result.setup_name,
                model_version=intent.model_version if intent is not None else None,
                context=_feature_context(features, feature_window),
                checks=checks,
                decision="rejected",
                reason=failed_reasons or ("Strategy setup rejected.",),
            ),
        )
        return

    risk_decision = risk_engine(intent, state, features)
    if not risk_decision.allowed:
        append_decision_log_entry(
            decision_log_path,
            _decision_log_entry(
                timestamp_ns=state.timestamp_ns,
                mode=log_mode,
                symbol=intent.symbol,
                direction=intent.direction,
                strategy_version=intent.strategy_version,
                model_version=intent.model_version,
                context=_feature_context(features, feature_window),
                checks=checks,
                decision="rejected",
                reason=risk_decision.reason or ("Risk engine rejected shadow order.",),
            ),
        )
        return

    submission = execution_stub.record_submission(timestamp_ns=state.timestamp_ns, intent=intent)
    append_decision_log_entry(
        decision_log_path,
        _decision_log_entry(
            timestamp_ns=state.timestamp_ns,
            mode=log_mode,
            symbol=intent.symbol,
            direction=intent.direction,
            strategy_version=intent.strategy_version,
            model_version=intent.model_version,
            context={
                **_feature_context(features, feature_window),
                "shadow_submission": submission.message,
            },
            checks=checks,
            decision="accepted",
            reason=(submission.message,),
        ),
    )


def _decision_log_entry(
    *,
    timestamp_ns: int,
    mode: ShadowLogMode,
    symbol: str,
    direction: Direction,
    strategy_version: str,
    model_version: str | None,
    context: Mapping[str, object],
    checks: Mapping[str, bool],
    decision: Literal["accepted", "rejected"],
    reason: tuple[str, ...],
) -> DecisionLogEntry:
    return DecisionLogEntry(
        timestamp=timestamp_ns,
        mode=mode,
        symbol=symbol,
        direction=direction,
        strategy_version=strategy_version,
        model_version=model_version,
        context=context,
        checks=checks,
        decision=decision,
        reason=reason,
    )


def _feature_context(features: MarketFeatures, feature_window: tuple[MarketState, ...]) -> dict[str, object]:
    return {
        "feature_window_size": len(feature_window),
        "book_imbalance": features.book_imbalance,
        "liquidity_added": features.liquidity_added,
        "liquidity_cancelled": features.liquidity_cancelled,
        "trade_velocity": features.trade_velocity,
        "short_term_volatility": features.short_term_volatility,
        "distance_from_session_high": features.distance_from_session_high,
        "distance_from_session_low": features.distance_from_session_low,
        "bid_reload_count": features.bid_reload_count,
    }


def _symbol_from_state_or_intent(intent: ShadowOrderIntent | None) -> str:
    if intent is None:
        return "UNKNOWN"
    return intent.symbol


def _validated_config(config: ShadowPipelineConfig) -> ShadowPipelineConfig:
    if config.feature_window_size <= 0:
        raise ValueError("feature_window_size must be greater than zero")
    if config.bid_reload_level <= 0:
        raise ValueError("bid_reload_level must be greater than zero")
    if config.bid_reload_threshold <= Decimal("0"):
        raise ValueError("bid_reload_threshold must be greater than zero")
    if config.log_mode not in {"observe", "simulate"}:
        raise ValueError("shadow log_mode must be observe or simulate")
    return config

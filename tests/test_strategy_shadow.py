"""Tests for the shadow-mode strategy pipeline."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from app.database.logging import read_ndjson_log
from app.market.features import MarketFeatures
from app.market.state import MarketState
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult
from app.strategy.shadow import (
    NoOpShadowExecutionStub,
    ShadowOrderIntent,
    ShadowPipelineConfig,
    ShadowRiskDecision,
    ShadowStrategyDecision,
    run_shadow_pipeline,
    run_shadow_pipeline_async,
)


class SequenceStrategyEngine:
    """Strategy test double that returns configured decisions in sequence."""

    def __init__(self, decisions: list[ShadowStrategyDecision | None]) -> None:
        """Create a strategy engine with queued decisions."""
        self.decisions = decisions
        self.calls: list[tuple[MarketState, tuple[MarketState, ...], MarketFeatures]] = []

    def __call__(
        self,
        market_state: MarketState,
        feature_window: tuple[MarketState, ...],
        features: MarketFeatures,
    ) -> ShadowStrategyDecision | None:
        """Return the next configured decision."""
        self.calls.append((market_state, feature_window, features))
        if not self.decisions:
            return None
        return self.decisions.pop(0)


class StaticRiskEngine:
    """Risk test double with a static allow/reject result."""

    def __init__(self, decision: ShadowRiskDecision) -> None:
        """Create a static risk engine."""
        self.decision = decision
        self.calls: list[tuple[ShadowOrderIntent, MarketState, MarketFeatures]] = []

    def __call__(
        self,
        order_intent: ShadowOrderIntent,
        market_state: MarketState,
        features: MarketFeatures,
    ) -> ShadowRiskDecision:
        """Record the risk request and return the static decision."""
        self.calls.append((order_intent, market_state, features))
        return self.decision


class AsyncEventStream:
    """Async event stream test double."""

    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        """Create an async event stream."""
        self.events = events

    def __aiter__(self) -> "AsyncEventStream":
        """Return this stream as its own async iterator."""
        self._index = 0
        return self

    async def __anext__(self) -> dict[str, object]:
        """Return the next event."""
        if self._index >= len(self.events):
            raise StopAsyncIteration
        event = self.events[self._index]
        self._index += 1
        return event


def test_shadow_pipeline_logs_acceptance_and_records_noop_submission(tmp_path: Path) -> None:
    """Accepted strategy and risk decisions are logged and recorded as no-op submissions."""
    log_path = tmp_path / "decisions.ndjson"
    intent = _intent()
    strategy = SequenceStrategyEngine([ShadowStrategyDecision(_accepted_setup(), intent)])
    risk = StaticRiskEngine(ShadowRiskDecision(allowed=True, reason=("risk passed",)))
    execution_stub = NoOpShadowExecutionStub()

    result = run_shadow_pipeline(
        _book_events(),
        strategy_engine=strategy,
        risk_engine=risk,
        decision_log_path=log_path,
        execution_stub=execution_stub,
    )

    assert result.events_processed == 1
    assert result.decisions_logged == 1
    assert len(result.submissions) == 1
    assert result.submissions[0].message == "would have submitted 2 long MNQ at 100.25"
    assert risk.calls[0][0] == intent
    logs = read_ndjson_log(log_path)
    assert logs[0]["decision"] == "accepted"
    assert logs[0]["mode"] == "observe"
    assert logs[0]["reason"] == ["would have submitted 2 long MNQ at 100.25"]
    assert logs[0]["checks"] == {"at_important_level": True, "risk_shape": True}


def test_shadow_pipeline_logs_strategy_rejection_without_risk_or_submission(tmp_path: Path) -> None:
    """Rejected setup decisions are logged before risk or execution is consulted."""
    log_path = tmp_path / "decisions.ndjson"
    strategy = SequenceStrategyEngine([ShadowStrategyDecision(_rejected_setup(), _intent())])
    risk = StaticRiskEngine(ShadowRiskDecision(allowed=True, reason=("risk passed",)))

    result = run_shadow_pipeline(
        _book_events(),
        strategy_engine=strategy,
        risk_engine=risk,
        decision_log_path=log_path,
    )

    assert result.submissions == ()
    assert risk.calls == []
    logs = read_ndjson_log(log_path)
    assert logs[0]["decision"] == "rejected"
    assert logs[0]["checks"]["risk_shape"] is False
    assert logs[0]["reason"] == ["Risk shape failed"]


def test_shadow_pipeline_logs_risk_rejection_without_submission(tmp_path: Path) -> None:
    """Risk rejections are logged and do not reach the no-op execution stub."""
    log_path = tmp_path / "decisions.ndjson"
    strategy = SequenceStrategyEngine([ShadowStrategyDecision(_accepted_setup(), _intent())])
    risk = StaticRiskEngine(ShadowRiskDecision(allowed=False, reason=("daily lock",)))
    execution_stub = NoOpShadowExecutionStub()

    result = run_shadow_pipeline(
        _book_events(),
        strategy_engine=strategy,
        risk_engine=risk,
        decision_log_path=log_path,
        execution_stub=execution_stub,
    )

    assert result.submissions == ()
    assert execution_stub.submissions == []
    logs = read_ndjson_log(log_path)
    assert logs[0]["decision"] == "rejected"
    assert logs[0]["reason"] == ["daily lock"]


def test_shadow_pipeline_logs_no_signal_for_every_event(tmp_path: Path) -> None:
    """Every processed event receives a decision log entry even when no setup appears."""
    log_path = tmp_path / "decisions.ndjson"
    strategy = SequenceStrategyEngine([None, None])
    risk = StaticRiskEngine(ShadowRiskDecision(allowed=True, reason=("risk passed",)))

    result = run_shadow_pipeline(
        _two_events(),
        strategy_engine=strategy,
        risk_engine=risk,
        decision_log_path=log_path,
        config=ShadowPipelineConfig(feature_window_size=1, log_mode="simulate"),
    )

    assert result.events_processed == 2
    assert result.decisions_logged == 2
    assert risk.calls == []
    logs = read_ndjson_log(log_path)
    assert [entry["decision"] for entry in logs] == ["rejected", "rejected"]
    assert [entry["mode"] for entry in logs] == ["simulate", "simulate"]
    assert logs[0]["reason"] == ["No setup detected."]


def test_shadow_pipeline_async_accepts_live_style_stream(tmp_path: Path) -> None:
    """The async entry point can consume a live-style async event stream."""
    log_path = tmp_path / "decisions.ndjson"
    strategy = SequenceStrategyEngine([ShadowStrategyDecision(_accepted_setup(), _intent())])
    risk = StaticRiskEngine(ShadowRiskDecision(allowed=True, reason=("risk passed",)))

    result = asyncio.run(
        run_shadow_pipeline_async(
            AsyncEventStream(_book_events()),
            strategy_engine=strategy,
            risk_engine=risk,
            decision_log_path=log_path,
        ),
    )

    assert result.events_processed == 1
    assert len(result.submissions) == 1
    assert read_ndjson_log(log_path)[0]["decision"] == "accepted"


def test_shadow_module_does_not_import_real_execution_orders() -> None:
    """Shadow mode cannot reach real Tradovate execution because it does not import it."""
    source = Path("app/strategy/shadow.py").read_text(encoding="utf-8")

    assert "app.execution.orders" not in source
    assert "Tradovate" not in source


def _accepted_setup() -> SetupEvaluationResult:
    return SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(
            SetupConditionResult("at_important_level", True, "Price at overnight_low"),
            SetupConditionResult("risk_shape", True, "Risk shape passed"),
        ),
    )


def _rejected_setup() -> SetupEvaluationResult:
    return SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(
            SetupConditionResult("at_important_level", True, "Price at overnight_low"),
            SetupConditionResult("risk_shape", False, "Risk shape failed"),
        ),
    )


def _intent() -> ShadowOrderIntent:
    return ShadowOrderIntent(
        symbol="MNQ",
        direction="long",
        quantity=2,
        requested_price=Decimal("100.25"),
        strategy_version="shadow-test-v1",
        model_version=None,
    )


def _book_events() -> tuple[dict[str, object], ...]:
    return (
        {
            "type": "depth_update",
            "timestamp": 100,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )


def _two_events() -> tuple[dict[str, object], ...]:
    return (
        {
            "type": "depth_update",
            "timestamp": 100,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
        {
            "type": "depth_update",
            "timestamp": 200,
            "symbol": "MNQ",
            "side": "ask",
            "price": "100.25",
            "previous_size": "0",
            "new_size": "8",
        },
    )

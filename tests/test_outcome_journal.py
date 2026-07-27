"""Prediction/outcome journal safety contract (ML-004).

Mirrors tests/test_shadow_predictor.py in structure: resolution only ever
uses causally observed prices, an unresolved prediction is dropped rather
than guessed, and the module carries no dependency on
strategy/paper/risk/execution code.
"""

from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path

from app.machine_learning.outcome_journal import (
    OutcomeJournal,
    OutcomeRecord,
    PendingOutcomeTracker,
)
from app.machine_learning.shadow_predictor import ShadowPrediction
from app.market.state import MarketState


def _prediction(**overrides: object) -> ShadowPrediction:
    base = dict(
        prediction_id="challenger-x-000000000001",
        artifact_id="challenger-x",
        artifact_sha256="a" * 64,
        session_id="session-one",
        timestamp_ns=1_000_000_000,
        direction="long",
        feature_vector_sha256="b" * 64,
        success_probability=0.6,
    )
    base.update(overrides)
    return ShadowPrediction(**base)  # type: ignore[arg-type]


def _state_at_price(price: Decimal, timestamp_ns: int) -> MarketState:
    state = MarketState()
    state = state.update({
        "type": "depth_update", "timestamp": timestamp_ns, "symbol": "MNQ",
        "side": "bid", "price": str(price), "previous_size": "0", "new_size": "10",
    })
    state = state.update({
        "type": "depth_update", "timestamp": timestamp_ns, "symbol": "MNQ",
        "side": "ask", "price": str(price + Decimal("0.25")),
        "previous_size": "0", "new_size": "10",
    })
    return state


def test_target_resolution_writes_win_with_causal_only_prices(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")

    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )

    # Price moves up 6 ticks (1.5) -> target hit.
    tracker.observe_market({}, _state_at_price(Decimal("29501.50"), 2_000_000_000))

    snapshot = tracker.snapshot()
    assert snapshot.resolved == 1
    assert snapshot.resolved_target == 1
    assert snapshot.pending == 0
    assert journal.count == 1

    record = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["prediction_id"] == prediction.prediction_id
    assert record["label"] == 1
    assert record["resolution_reason"] == "target"
    assert record["decision_impact"] == "none"


def test_stop_resolution_writes_loss(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    # Price drops 6 ticks (1.5) -> stop hit. _state_at_price(bid) sets
    # ask=bid+0.25, so mid = bid+0.125; use a bid low enough that mid <= stop.
    tracker.observe_market({}, _state_at_price(Decimal("29498.25"), 2_000_000_000))
    snapshot = tracker.snapshot()
    assert snapshot.resolved_stop == 1
    assert snapshot.resolved_target == 0


def test_timeout_resolution_when_neither_barrier_hit_before_deadline(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=5.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    # Price barely moves and the deadline (1s + 5s = 6s) has passed.
    tracker.observe_market({}, _state_at_price(Decimal("29500.25"), 7_000_000_000))
    snapshot = tracker.snapshot()
    assert snapshot.resolved_timeout == 1
    record = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["label"] == 0
    assert record["resolution_reason"] == "timeout"


def test_post_horizon_price_crossing_resolves_timeout_not_target(tmp_path: Path) -> None:
    """A target crossing first observed after T+horizon is future data."""
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=5.0)
    tracker.bind_session("session-one")
    tracker.register(
        _prediction(timestamp_ns=1_000_000_000),
        entry_price=Decimal("29500.00"),
        direction="long",
        target_ticks=Decimal("6"),
        stop_ticks=Decimal("6"),
        tick_size=Decimal("0.25"),
    )

    # Deadline is 6s. The first later observation crosses the target at 7s;
    # its price must not be inspected as though it occurred inside the horizon.
    tracker.observe_market({}, _state_at_price(Decimal("29502.00"), 7_000_000_000))

    snapshot = tracker.snapshot()
    assert snapshot.resolved_target == 0
    assert snapshot.resolved_timeout == 1
    record = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["label"] == 0
    assert record["resolution_reason"] == "timeout"


def test_barrier_crossing_exactly_at_deadline_is_inside_horizon(tmp_path: Path) -> None:
    """The horizon endpoint is inclusive, unlike observations after it."""
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=5.0)
    tracker.bind_session("session-one")
    tracker.register(
        _prediction(timestamp_ns=1_000_000_000),
        entry_price=Decimal("29500.00"),
        direction="long",
        target_ticks=Decimal("6"),
        stop_ticks=Decimal("6"),
        tick_size=Decimal("0.25"),
    )

    tracker.observe_market({}, _state_at_price(Decimal("29501.50"), 6_000_000_000))

    snapshot = tracker.snapshot()
    assert snapshot.resolved_target == 1
    assert snapshot.resolved_timeout == 0


def test_session_boundary_drops_unresolved_predictions_never_guessing(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=600.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    # Never resolves before session end.
    tracker.bind_session("session-two")
    snapshot = tracker.snapshot()
    assert snapshot.resolved == 0
    assert snapshot.dropped_unresolved == 1
    assert snapshot.pending == 0
    record = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["label"] is None
    assert record["resolution_reason"] == "session_end_unresolved"


def test_a_later_session_prediction_never_resolves_against_a_prior_sessions_prices(
    tmp_path: Path,
) -> None:
    """No leakage across sessions: switching sessions must not resolve stale entries."""
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=600.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    tracker.bind_session("session-two")
    # A price that WOULD have hit target in session-one must not resolve
    # anything: the pending entry was already flushed unresolved at the
    # session boundary.
    tracker.observe_market({}, _state_at_price(Decimal("29510.00"), 2_000_000_000))
    snapshot = tracker.snapshot()
    assert snapshot.resolved == 0
    assert snapshot.dropped_unresolved == 1


def test_causality_gap_taints_pending_resolution_with_quality_flag(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    tracker.notify_causality_gap(5)
    tracker.observe_market({}, _state_at_price(Decimal("29501.50"), 2_000_000_000))
    snapshot = tracker.snapshot()
    assert snapshot.gap_tainted_resolutions == 1
    record = json.loads((tmp_path / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["quality_flags"] == ["causality_gap_during_window"]


def test_overflow_beyond_max_pending_drops_the_oldest_prediction(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=6000.0, max_pending=2)
    tracker.bind_session("session-one")
    for index in range(3):
        prediction = _prediction(
            prediction_id=f"challenger-x-{index:012d}",
            timestamp_ns=1_000_000_000 + index * 1_000_000_000,
        )
        tracker.register(
            prediction, entry_price=Decimal("29500.00"), direction="long",
            target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
        )
    snapshot = tracker.snapshot()
    assert snapshot.pending == 2
    assert snapshot.dropped_overflow == 1


def test_short_direction_barriers_resolve_correctly(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000, direction="short")
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="short",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    # Short target: price drops 6 ticks (mid = bid + 0.125).
    tracker.observe_market({}, _state_at_price(Decimal("29498.25"), 2_000_000_000))
    snapshot = tracker.snapshot()
    assert snapshot.resolved_target == 1


def test_double_registration_of_same_prediction_id_is_not_double_counted(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")
    prediction = _prediction(timestamp_ns=1_000_000_000)
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    tracker.register(
        prediction, entry_price=Decimal("29500.00"), direction="long",
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"), tick_size=Decimal("0.25"),
    )
    assert tracker.snapshot().pending == 1


def test_journal_tolerates_a_torn_final_line(tmp_path: Path) -> None:
    path = tmp_path / "outcomes.jsonl"
    journal = OutcomeJournal(path)
    journal.append(OutcomeRecord(
        prediction_id="p-1", artifact_id="challenger-x", artifact_sha256="a" * 64,
        session_id="s", direction="long", entry_timestamp_ns=1, entry_price="29500.00",
        target_price="29501.50", stop_price="29498.50", success_probability=0.6,
        resolved_timestamp_ns=2, resolution_reason="target", label=1,
        resolution_latency_ns=1,
    ))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"broken json')

    recovery = OutcomeJournal.recover(path)
    assert recovery.damaged_tail is True
    assert len(recovery.records) == 1

    reopened = OutcomeJournal(path)
    assert reopened.count == 1


def test_no_paper_risk_execution_or_broker_imports() -> None:
    """Same causal reference-price helper feature_contract.py legitimately uses;
    the ban is on paper/risk/execution/broker code, not app.strategy itself."""
    source = Path("app/machine_learning/outcome_journal.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    banned = ("app.paper", "app.risk", "app.execution", "broker", "tradovate")
    assert not [name for name in imports if any(item in name.lower() for item in banned)]


def test_malformed_state_is_ignored_never_raises(tmp_path: Path) -> None:
    journal = OutcomeJournal(tmp_path / "outcomes.jsonl")
    tracker = PendingOutcomeTracker(journal=journal, horizon_seconds=60.0)
    tracker.bind_session("session-one")
    # Must not raise for a non-MarketState object.
    tracker.observe_market({}, object())
    assert tracker.snapshot().resolved == 0

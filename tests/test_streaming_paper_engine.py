"""Regression tests: the delayed stream MUST be paper-evaluated automatically.

The defect these pin: delayed data set ``decisions_allowed = False`` and the
runtime entered RECORDING_ONLY, so while Bookmap streamed and the recorder was
healthy, the paper engine sat at "No setup has been evaluated yet" forever.
Recording-eligibility and paper-eligibility are different questions and must
never be conflated again.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from app.paper.streaming_engine import (
    STATE_EVALUATING,
    STATE_WARMING,
    DelayedPaperEngine,
)


def test_condition_evidence_exposes_observed_and_required_values() -> None:
    from app.paper.streaming_engine import _condition_evidence

    observed, required = _condition_evidence(
        "absorption_confirmed",
        False,
        "opposite aggressive volume 142, requires >= 200",
    )
    assert observed == "opposite aggressive volume 142"
    assert required == ">= 200"

    observed, required = _condition_evidence(
        "reload_confirmed",
        False,
        "No block available to evaluate reload",
    )
    assert observed == "not detected"
    assert "reload" in required


def _depth(i: int, price: Decimal) -> dict:
    return {"type": "depth_update", "timestamp": 1_752_537_751_000_000_000 + i * 500_000_000,
            "symbol": "MNQ", "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
            "previous_size": "0", "new_size": str(i % 40 + 1)}


def _trade(i: int, price: Decimal) -> dict:
    return {"timestamp_ns": 1_752_537_751_000_000_000 + i * 500_000_000 + 1, "price": f"{price:.2f}",
            "size": "1", "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ",
            "sequence_id": i + 1}


def _stream(engine: DelayedPaperEngine, count: int, *, start: int = 0) -> None:
    price = Decimal("29500.00")
    for offset in range(count):
        i = start + offset  # market time moves FORWARD across repeated calls
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        engine.on_market_event(_depth(i, price))
        engine.on_market_event(_trade(i, price))


def test_streaming_events_start_evaluation_with_no_user_action() -> None:
    """Feeding live events is the ONLY trigger - no button, no finalized session."""
    engine = DelayedPaperEngine()
    engine.bind_session("session_live", "MNQ")
    _stream(engine, 300)
    status = engine.status()
    assert status.state == STATE_EVALUATING
    assert status.evaluations > 0, "a live delayed stream must be evaluated automatically"
    assert status.events_seen == 600


def test_warmup_is_bounded_and_visible_then_transitions_to_evaluating() -> None:
    """Warm-up must be visible and bounded, not an indefinite idle state."""
    engine = DelayedPaperEngine()
    engine.bind_session("s", "MNQ")
    _stream(engine, 10)  # 20 events spanning ~5s, below the 60s warm-up span
    warming = engine.status()
    assert warming.state == STATE_WARMING
    assert 0.0 < warming.warmup_fraction < 1.0
    assert warming.evaluations == 0  # honest: not yet evaluating

    _stream(engine, 300, start=10)
    ready = engine.status()
    assert ready.state == STATE_EVALUATING
    assert ready.warmup_fraction == 1.0
    assert ready.evaluations > 0


def test_evaluation_count_increases_as_the_stream_continues() -> None:
    """The count must keep climbing - never stick at zero while events arrive."""
    engine = DelayedPaperEngine()
    engine.bind_session("s", "MNQ")
    _stream(engine, 300)
    first = engine.status().evaluations
    _stream(engine, 300, start=300)
    assert engine.status().evaluations > first


def test_rejected_setups_expose_exact_reasons_never_a_blank_state() -> None:
    """Zero trades is only acceptable WITH the exact reasons shown."""
    engine = DelayedPaperEngine()
    engine.bind_session("s", "MNQ")
    _stream(engine, 300)
    status = engine.status()
    assert status.last_decision in {"accepted", "rejected"}
    if status.last_decision == "rejected":
        assert status.last_reason, "a rejection must name the failing condition"
        assert status.top_rejections, "rejection reasons must be tallied for the GUI"
    recent = engine.recent_evaluations(limit=1)[-1]
    assert recent.conditions, "every evaluation records its full checklist"
    assert all(c.name for c in recent.conditions)
    assert recent.session_id == "s"
    assert recent.strategy_version  # traceable to a versioned strategy


def test_evaluations_are_traceable_to_session_and_event_range() -> None:
    """Every evaluation carries full lineage."""
    engine = DelayedPaperEngine()
    engine.bind_session("session_20260717T140000Z", "MNQU6")
    _stream(engine, 300)
    record = engine.recent_evaluations(limit=1)[-1]
    assert record.session_id == "session_20260717T140000Z"
    assert record.setup_id.startswith("session_20260717T140000Z:")
    assert record.event_index > 0
    assert record.evaluated_at_ns > 0
    assert record.direction in {"long", "short"}


def test_session_id_is_never_unknown_once_bound() -> None:
    """The GUI must never show 'unknown' after recording starts."""
    engine = DelayedPaperEngine()
    assert engine.status().session_id == ""  # honest before binding
    engine.bind_session("session_real", "MNQU6")
    status = engine.status()
    assert status.session_id == "session_real"
    assert status.contract == "MNQU6"


def test_missing_mbo_disables_only_mbo_setups_not_the_engine() -> None:
    """A missing capability must never disable the whole paper engine."""
    engine = DelayedPaperEngine(capabilities={"mbo": False})
    engine.bind_session("s", "MNQ")
    _stream(engine, 300)
    status = engine.status()
    disabled = dict(status.disabled_setups)
    assert "iceberg_continuation" in disabled
    assert "MBO" in disabled["iceberg_continuation"]
    assert status.evaluations > 0, "other setups must keep evaluating without MBO"

    with_mbo = DelayedPaperEngine(capabilities={"mbo": True})
    with_mbo.bind_session("s", "MNQ")
    assert with_mbo.status().disabled_setups == ()


def test_engine_is_causal_and_never_needs_a_finalized_session() -> None:
    """No finalized file, no replay: only the events seen so far."""
    engine = DelayedPaperEngine()
    engine.bind_session("active_recording", "MNQ")
    _stream(engine, 300)
    # Evaluations happened while the session is still actively recording.
    assert engine.status().evaluations > 0
    # Every evaluation's event_index is <= the events seen at that moment.
    seen = engine.status().events_seen
    assert all(r.event_index <= seen for r in engine.recent_evaluations(limit=50))


def test_a_bad_event_does_not_kill_the_engine() -> None:
    """One malformed event must not stop paper evaluation of the stream."""
    engine = DelayedPaperEngine()
    engine.bind_session("s", "MNQ")
    engine.on_market_event({"type": "depth_update", "price": "not-a-number"})
    _stream(engine, 300)
    assert engine.status().evaluations > 0


def test_paper_engine_cannot_reach_any_broker_module() -> None:
    """Structural proof: delayed paper is simulation-only by construction.

    Checked across the WHOLE app/paper package, not just this one module, so a
    broker import cannot be smuggled in one hop away. ``app.paper.execution`` is
    the simulator and is allowed; the broker package ``app.execution`` is not.
    """
    banned = ("app.execution", "tradovate", "broker", "gateway", "live_execution", "order_lifecycle")
    offenders: list[str] = []
    for module in sorted(Path("app/paper").glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        offenders += [f"{module.as_posix()}:{n}" for n in names
                      if any(b in n.lower() for b in banned)]
    assert not offenders, offenders


def test_launcher_feeds_every_market_event_to_the_paper_engine() -> None:
    """The wiring that makes evaluation automatic must stay in place.

    It is now the ANALYSIS FEED wiring: every accepted event is offered from
    the capture loop (O(1)) and consumed by the paper engine on the feed's own
    thread. Inline evaluation on the capture loop is the measured cause of the
    real Bookmap drops and must never come back.
    """
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "DelayedPaperEngine()" in source, "the launcher must create the engine at startup"
    assert "on_event_state=feed.offer" in source, (
        "every accepted market event must be offered to the analysis feed"
    )
    assert "paper_engine.ingest(event, state)" in source, (
        "the paper engine must consume events from the analysis thread"
    )
    assert "feed.add_gap_sink(paper_engine.notify_causality_gap)" in source, (
        "an analysis overflow must be reported to the paper engine as a gap"
    )
    assert "paper_engine.on_market_event(event)" not in source, (
        "inline paper evaluation on the capture loop is the drop root cause"
    )
    # And the real session id must be bound when the recorder is created.
    assert "paper_engine.bind_session(recorder.session_id" in source


def test_gui_snapshot_reports_real_evaluations_not_a_placeholder() -> None:
    """The GUI must show the engine's real counts, not a hardcoded zero."""
    from app.gui.snapshot_source import SnapshotSource

    engine = DelayedPaperEngine()
    engine.bind_session("session_gui", "MNQU6")
    _stream(engine, 300)
    snapshot = SnapshotSource(paper_engine=engine)()
    assert snapshot.paper.evaluations > 0
    assert "EVALUATING" in snapshot.paper.mode
    assert snapshot.paper.setup_checks, "the GUI must show the real condition checklist"
    assert snapshot.capture.session_id == "session_gui"  # never 'unknown'
    assert "MNQU6" in snapshot.market.contract
    # The honest empty-state reason must reflect real evaluations, not silence.
    assert "opportunities evaluated" in snapshot.paper.empty_reason

"""End-to-end proof of the AUTOMATIC causal paper-trading lifecycle.

market event -> state -> features -> setup evaluation -> candidate -> risk
-> simulated order -> causal fill -> open position -> stop/target management
-> exit -> P&L and costs -> account update -> ledger.

**Fixture honesty.** ``_absorption_long_events`` is a hand-built deterministic
synthetic tape, NOT a market recording. Every engine here is constructed with
``is_synthetic_fixture=True``, so each resulting trade is stamped
``is_synthetic_fixture: true`` and is excluded from real statistics by
``LedgerRecovery.real_records``. These tests prove the machinery works; they are
not evidence that the strategy is profitable on real data.

The fixture is shaped like the accepted long-absorption scenario in
``tests/test_strategy_order_flow.py``, but placed at a real New York RTH time and
at real MNQ prices, because the engine derives its session context causally from
the event timestamps rather than being handed a context.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.paper.ledger import PaperLedger
from app.paper.models import CloseReason
from app.paper.streaming_engine import DelayedPaperEngine
from app.research.episode_builder import EpisodeConfig

NEW_YORK = ZoneInfo("America/New_York")
SEC = 1_000_000_000
# 10:05 ET: inside RTH and past the mandatory 10-minute opening observation.
BASE_NS = int(datetime(2026, 7, 16, 10, 5, tzinfo=NEW_YORK).timestamp()) * SEC


def _price(offset: str) -> str:
    """Place the fixture's shape at a realistic MNQ price."""
    return f"{Decimal(offset) + Decimal('29400'):.2f}"


def _depth(ts_ns: int, side: str, price: str, previous: str, new: str) -> dict[str, object]:
    return {"type": "depth_update", "timestamp": ts_ns, "symbol": "MNQ", "side": side,
            "price": price, "previous_size": previous, "new_size": new}


def _trade(ts_ns: int, price: str, size: str, side: str, seq: int) -> dict[str, object]:
    return {"type": "trade", "timestamp_ns": ts_ns, "instrument": "MNQ", "price": price,
            "size": size, "aggressor_side": side, "sequence_id": seq}


def _absorption_long_events(*, follow_through_to_target: bool = False) -> list[dict[str, object]]:
    """A deterministic synthetic tape: bid absorption then buy continuation."""
    events = [
        _depth(BASE_NS, "bid", _price("100.00"), "0", "120"),
        _depth(BASE_NS, "bid", _price("99.75"), "0", "70"),
        _depth(BASE_NS, "ask", _price("100.25"), "0", "100"),
        _depth(BASE_NS, "ask", _price("102.00"), "0", "120"),
        _trade(BASE_NS + SEC, _price("100.00"), "420", "sell", 1),      # absorbed
        _depth(BASE_NS + SEC, "bid", _price("100.00"), "120", "20"),
        _depth(BASE_NS + 2 * SEC, "bid", _price("100.00"), "20", "135"),  # reload
        _depth(BASE_NS + 3 * SEC, "ask", _price("100.25"), "100", "10"),
        _depth(BASE_NS + 4 * SEC, "ask", _price("100.25"), "10", "0"),
        _depth(BASE_NS + 4 * SEC, "ask", _price("100.75"), "0", "40"),
        _depth(BASE_NS + 4 * SEC, "bid", _price("100.50"), "0", "70"),
        _depth(BASE_NS + 4 * SEC, "bid", _price("100.00"), "135", "130"),
        _trade(BASE_NS + 5 * SEC, _price("100.75"), "80", "buy", 2),
        _depth(BASE_NS + 5 * SEC, "bid", _price("100.00"), "130", "140"),
        _trade(BASE_NS + 6 * SEC, _price("100.75"), "70", "buy", 3),
    ]
    if follow_through_to_target:
        events += [
            _depth(BASE_NS + 7 * SEC, "bid", _price("101.50"), "0", "60"),
            _depth(BASE_NS + 7 * SEC, "ask", _price("102.00"), "120", "60"),
            _trade(BASE_NS + 8 * SEC, _price("102.00"), "90", "buy", 4),
            _depth(BASE_NS + 8 * SEC, "bid", _price("102.00"), "0", "80"),
            _depth(BASE_NS + 8 * SEC, "ask", _price("102.25"), "0", "60"),
        ]
    return events


def _engine() -> DelayedPaperEngine:
    """An engine that evaluates every event (fixture-scale warm-up and stride)."""
    return DelayedPaperEngine(
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
        is_synthetic_fixture=True,
    )


def _run(events: list[dict[str, object]], engine: DelayedPaperEngine | None = None) -> DelayedPaperEngine:
    engine = engine or _engine()
    engine.bind_session("session_fixture_20260716", "MNQU6")
    for event in events:
        engine.on_market_event(event)
    return engine


# --- the full lifecycle ----------------------------------------------------------


def test_a_qualifying_setup_automatically_opens_a_simulated_position() -> None:
    """No button, no manual step: an accepted setup becomes a real paper order."""
    status = _run(_absorption_long_events()).status()
    assert status.accepted_setups > 0, "the fixture must genuinely pass the strategy"
    assert status.candidates > 0, "an accepted setup must produce a candidate"
    assert status.orders_submitted == 1
    assert status.open_position.startswith("long"), status.open_position
    assert status.position_entry and status.position_stop and status.position_target


def test_the_position_closes_at_the_target_with_real_economics() -> None:
    """Fill -> manage -> exit -> P&L -> balance, all from the event stream."""
    engine = _run(_absorption_long_events(follow_through_to_target=True))
    status = engine.status()
    assert status.trades == 1
    trade = engine.recent_trades()[-1]
    assert trade.close_reason is CloseReason.TARGET
    assert trade.exit_price == Decimal("29502.00"), "a limit target fills AT the target"
    # Costs are real: gross must exceed net by exactly the commission.
    assert trade.gross_pnl - trade.commission == trade.net_pnl
    assert trade.commission == Decimal("1.24") * trade.contracts
    assert engine.status().balance == str(trade.balance_after)
    assert status.wins == 1 and status.losses == 0


def test_the_entry_price_is_a_price_mnq_could_actually_trade() -> None:
    """Regression: the entry once booked at the mid (e.g. 29500.875), which is fiction."""
    engine = _run(_absorption_long_events())
    entry = Decimal(engine.status().position_entry)
    assert entry % Decimal("0.25") == 0, f"{entry} is not on the MNQ tick grid"


def test_stop_and_target_come_from_the_strategy_not_from_the_executor() -> None:
    """Levels must be the strategy's own, never invented to make a trade possible."""
    engine = _run(_absorption_long_events())
    status = engine.status()
    stop, target, entry = (Decimal(status.position_stop), Decimal(status.position_target),
                           Decimal(status.position_entry))
    assert stop < entry < target, "long geometry must hold"
    # The defended bid block sits at 29500.00 and the DOL target block at 29502.00.
    assert target == Decimal("29502.00"), "target is the observed direction-of-liquidity"
    assert stop < Decimal("29500.00"), "stop sits beyond the defended block"


def test_only_one_position_is_open_at_a_time_and_extras_are_explained() -> None:
    """Later accepted setups must be refused with a real reason, not silently."""
    engine = _run(_absorption_long_events())
    status = engine.status()
    assert status.candidates > status.orders_submitted, "the fixture repeats the setup"
    assert status.risk_rejected > 0
    assert dict(engine.top_risk_rejections()).get("one_position_max", 0) > 0


def test_closed_trades_persist_to_the_ledger_automatically(tmp_path: Path) -> None:
    """The lifecycle ends in durable storage, written to an external temp dir."""
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    engine = _engine()
    engine.on_trade_closed(ledger.append)
    _run(_absorption_long_events(follow_through_to_target=True), engine)
    recovery = PaperLedger.recover(tmp_path / "paper.jsonl")
    assert len(recovery.records) == 1
    record = recovery.records[0]
    assert record["session_id"] == "session_fixture_20260716"
    assert record["contract"] == "MNQU6"
    assert record["close_reason"] == "target"


def test_fixture_trades_are_quarantined_from_real_statistics(tmp_path: Path) -> None:
    """A synthetic fixture may never be counted as a real trading result."""
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    engine = _engine()
    engine.on_trade_closed(ledger.append)
    _run(_absorption_long_events(follow_through_to_target=True), engine)
    recovery = PaperLedger.recover(tmp_path / "paper.jsonl")
    assert recovery.records[0]["is_synthetic_fixture"] is True
    assert recovery.real_records == (), "fixture trades must not enter real results"
    assert recovery.realized_pnl == Decimal("0")


def test_every_trade_is_traceable_to_the_events_that_caused_it() -> None:
    """Provenance is what separates a result from a story."""
    engine = _run(_absorption_long_events(follow_through_to_target=True))
    trade = engine.recent_trades()[-1]
    assert trade.provenance.session_id == "session_fixture_20260716"
    assert trade.provenance.strategy_version
    assert trade.provenance.setup_id
    # The decision strictly precedes the fill, which strictly precedes the close.
    assert trade.provenance.decision_event_index <= trade.opened_event_index
    assert trade.opened_event_index < trade.closed_event_index


# --- honesty guarantees ----------------------------------------------------------


def test_a_stream_with_no_qualifying_setup_produces_no_trades_and_real_reasons() -> None:
    """Zero trades is a valid outcome - but it must be explained, not silent."""
    events = [
        _depth(BASE_NS + i * SEC, "bid" if i % 2 else "ask",
               _price(f"{100 + (i % 4) * 0.25:.2f}"), "0", str(10 + i % 7))
        for i in range(60)
    ]
    engine = _run(events)
    status = engine.status()
    assert status.evaluations > 0, "the engine must really be evaluating"
    assert status.trades == 0 and status.orders_submitted == 0
    assert status.open_position == "none"
    assert status.top_rejections, "a zero-trade stream must name its rejection reasons"


def test_positions_are_managed_on_every_event_not_only_on_decision_events() -> None:
    """A stop may not wait for the next evaluation: exposure resolves per event."""
    engine = DelayedPaperEngine(
        # A wide stride means evaluations are rare; management must not be.
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
        is_synthetic_fixture=True,
    )
    _run(_absorption_long_events(), engine)
    assert engine.status().open_position.startswith("long")
    engine._config = EpisodeConfig(warmup_events=1, decision_stride=1_000_000, warmup_span_seconds=0, evaluation_interval_ms=1e12, depth_sample_interval_ms=0)  # noqa: SLF001
    # Drive price down through the stop on a non-decision event.
    engine.on_market_event(_depth(BASE_NS + 20 * SEC, "bid", _price("80.00"), "0", "50"))
    engine.on_market_event(_depth(BASE_NS + 21 * SEC, "ask", _price("80.25"), "0", "50"))
    status = engine.status()
    assert status.trades == 1, "the stop must resolve without waiting for an evaluation"
    assert engine.recent_trades()[-1].close_reason is CloseReason.STOP


def test_flatten_closes_an_open_position_at_session_end() -> None:
    engine = _run(_absorption_long_events())
    assert engine.status().open_position.startswith("long")
    trade = engine.flatten()
    assert trade is not None
    assert trade.close_reason is CloseReason.SESSION_CLOSE
    assert engine.status().open_position == "none"


def test_status_reports_the_executors_real_numbers() -> None:
    """The GUI must never show a number the executor does not actually hold."""
    engine = _run(_absorption_long_events(follow_through_to_target=True))
    status = engine.status()
    trades = engine.recent_trades()
    assert status.trades == len(trades)
    assert status.realized_pnl == str(sum((t.net_pnl for t in trades), Decimal("0")))
    assert status.wins == sum(1 for t in trades if t.won)


def test_dropped_market_events_are_counted_not_silently_swallowed() -> None:
    """A parser rejection must be visible: silent data loss corrupts everything."""
    engine = _engine()
    engine.bind_session("s", "MNQU6")
    engine.on_market_event({"type": "trade", "nonsense": True})  # wrong schema
    status = engine.status()
    assert status.malformed_events == 1, "a dropped event must be counted"
    assert status.error, "and it must say what went wrong"
    # The stream must still recover on the next good event.
    for event in _absorption_long_events():
        engine.on_market_event(event)
    assert engine.status().evaluations > 0, "one bad event must not stop the engine"
    assert engine.status().malformed_events == 1, "good events must not inflate the count"

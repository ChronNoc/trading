"""Tests for causal real-session episode building and outcome accounting."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

import app.research.episode_builder as builder_module
from app.database.recorder import MarketSessionRecorder
from app.market.state import MarketState
from app.research.replay_loader import ReplayEvent
from app.research.causal_context import DerivedContext
from app.research.episode_builder import (
    BuildResult,
    EpisodeConfig,
    _dedupe_bucket,
    build_episodes,
    resolve_outcome,
    write_episode_artifacts,
)
from app.research.real_episodes import load_completed_real_outcomes
from app.strategy.order_flow import OrderFlowPlanContext, StrategyLevels, TradeDirection
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult

_TIMEOUT_NS = 900 * 1_000_000_000


def _future(values: list[str], step_ns: int = 1_000_000) -> list[tuple[int, Decimal]]:
    return [(index * step_ns, Decimal(value)) for index, value in enumerate(values)]


def test_resolve_target_and_stop_first_for_long_and_short() -> None:
    long_win = resolve_outcome(
        direction="long", entry=Decimal("100"), stop=Decimal("90"), target=Decimal("120"),
        future_prices=_future(["101", "121"]), timeout_ns=_TIMEOUT_NS,
    )
    long_loss = resolve_outcome(
        direction="long", entry=Decimal("100"), stop=Decimal("90"), target=Decimal("120"),
        future_prices=_future(["99", "89"]), timeout_ns=_TIMEOUT_NS,
    )
    short_win = resolve_outcome(
        direction="short", entry=Decimal("100"), stop=Decimal("110"), target=Decimal("80"),
        future_prices=_future(["99", "79"]), timeout_ns=_TIMEOUT_NS,
    )
    short_loss = resolve_outcome(
        direction="short", entry=Decimal("100"), stop=Decimal("110"), target=Decimal("80"),
        future_prices=_future(["101", "111"]), timeout_ns=_TIMEOUT_NS,
    )
    assert (long_win.outcome, long_loss.outcome) == ("target_first", "stop_first")
    assert (short_win.outcome, short_loss.outcome) == ("target_first", "stop_first")


def test_resolve_same_timestamp_is_ambiguous_and_empty_is_unfinished() -> None:
    same_ns = 5_000_000
    ambiguous = resolve_outcome(
        direction="long", entry=Decimal("100"), stop=Decimal("90"), target=Decimal("120"),
        future_prices=[(same_ns, Decimal("121")), (same_ns, Decimal("89"))],
        timeout_ns=_TIMEOUT_NS,
    )
    unfinished = resolve_outcome(
        direction="long", entry=Decimal("100"), stop=Decimal("90"), target=Decimal("120"),
        future_prices=[], timeout_ns=_TIMEOUT_NS,
    )
    assert ambiguous.outcome == "ambiguous"
    assert unfinished.outcome == "unfinished"


def test_no_lookahead_earlier_touch_wins() -> None:
    result = resolve_outcome(
        direction="long", entry=Decimal("100"), stop=Decimal("90"), target=Decimal("120"),
        future_prices=[(1, Decimal("89")), (2, Decimal("121"))], timeout_ns=_TIMEOUT_NS,
    )
    assert result.outcome == "stop_first"


def test_dedupe_bucket_uses_tick_size_not_tick_value() -> None:
    config = EpisodeConfig()
    assert _dedupe_bucket(Decimal("29450.25"), config) == _dedupe_bucket(Decimal("29450.75"), config)
    assert config.slippage_model.tick_size == Decimal("0.25")


@pytest.mark.parametrize(
    "overrides",
    [
        {"fixed_contracts": -1},
        {"max_risk_per_trade_usd": Decimal("-0.01")},
        {"max_risk_per_trade_usd": Decimal("Infinity")},
        {"max_risk_per_trade_usd": Decimal("NaN")},
        {"fixed_contracts": 4, "max_risk_per_trade_usd": Decimal("0")},
    ],
)
def test_fixed_sizing_config_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EpisodeConfig(**overrides)


def test_fixed_sizing_config_accepts_four_contracts_with_positive_cap() -> None:
    config = EpisodeConfig(fixed_contracts=4, max_risk_per_trade_usd=Decimal("80"))
    assert config.fixed_contracts == 4
    assert config.max_risk_per_trade_usd == Decimal("80")


@pytest.mark.parametrize(
    ("direction", "outcome", "expected"),
    [
        ("long", "target", "target_first"),
        ("long", "stop", "stop_first"),
        ("short", "target", "target_first"),
        ("short", "stop", "stop_first"),
        ("long", "ambiguous", "ambiguous"),
        ("long", "timeout", "timeout_exit"),
        ("long", "unfinished", "unfinished"),
    ],
)
def test_streaming_state_machine_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    outcome: str,
    expected: str,
) -> None:
    _force_direction(monkeypatch, direction)
    session_dir = _simple_session(tmp_path, direction=direction, outcome=outcome)
    if outcome == "ambiguous":
        _remove_receive_order(session_dir)
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0, timeout_seconds=1),
    )
    matching = [episode for episode in result.episodes if episode.outcome == expected]
    assert matching, result.rejected_condition_tally
    episode = matching[0]
    assert episode.entry_ts_ns > episode.decision_ts_ns
    assert episode.source_event_range[1] >= episode.source_event_range[0]
    if expected in {"target_first", "stop_first", "timeout_exit"}:
        assert episode.net_pnl_per_contract is not None
        assert episode.slippage_cost is not None and episode.slippage_cost >= 0
    else:
        assert episode.eligible_for_ledger is False


def test_receive_sequence_resolves_same_timestamp_first_touch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_direction(monkeypatch, "long")
    session_dir = _simple_session(tmp_path, direction="long", outcome="ambiguous")
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    assert result.ordering_mode == "receive_sequence"
    assert result.episodes[0].outcome == "target_first"


def test_setup_deduplication_logs_duplicate_decisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_direction(monkeypatch, "long")
    session_dir = _simple_session(tmp_path, direction="long", outcome="target")
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    assert result.accepted_candidates == 1
    assert result.duplicate_candidates > 0
    duplicates = [decision for decision in result.decisions if decision.duplicate]
    assert duplicates
    assert duplicates[0].checks["deduplicated_setup"] is False


def test_future_outcome_does_not_change_decision_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_direction(monkeypatch, "long")
    target_dir = _simple_session(tmp_path / "target", direction="long", outcome="target")
    stop_dir = _simple_session(tmp_path / "stop", direction="long", outcome="stop")
    config = EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0)
    target_result = build_episodes(target_dir, session_id="same", provenance="REAL_DELAYED", config=config)
    stop_result = build_episodes(stop_dir, session_id="same", provenance="REAL_DELAYED", config=config)
    first_target = next(decision for decision in target_result.decisions if decision.accepted)
    first_stop = next(decision for decision in stop_result.decisions if decision.accepted)
    assert first_target.prefix_hash == first_stop.prefix_hash
    assert first_target.checks == first_stop.checks


def test_old_timestamp_only_collision_excludes_completed_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_direction(monkeypatch, "long")
    session_dir = _simple_session(tmp_path, direction="long", outcome="target")
    _remove_receive_order(session_dir)
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    assert result.ordering_mode == "timestamp_fallback"
    assert result.ordering_ambiguous is True
    assert result.ledger_eligible_count == 0
    assert all(not episode.eligible_for_ledger for episode in result.episodes)


def test_artifacts_are_traceable_and_synthetic_is_structurally_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_direction(monkeypatch, "long")
    session_dir = _simple_session(tmp_path / "raw", direction="long", outcome="target")
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    processed = tmp_path / "processed"
    labels = tmp_path / "labels"
    episodes_path, labels_path = write_episode_artifacts(result, processed_root=processed, labels_root=labels)
    assert episodes_path.exists() and labels_path.exists()
    assert (processed / f"{result.session_id}.decisions.jsonl").exists()
    loaded = load_completed_real_outcomes(processed)
    assert loaded and loaded[0].input_hash
    assert loaded[0].source_event_range[1] > loaded[0].source_event_range[0]

    synthetic_episodes = tuple(replace(episode, provenance="SYNTHETIC") for episode in result.episodes)
    synthetic = BuildResult(session_id="synthetic", provenance="SYNTHETIC", episodes=synthetic_episodes)
    write_episode_artifacts(synthetic, processed_root=processed, labels_root=labels)
    assert all(outcome.provenance != "SYNTHETIC" for outcome in load_completed_real_outcomes(processed))


def test_zero_acceptance_still_writes_honest_decisions_and_empty_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_direction(monkeypatch, "none")
    session_dir = _simple_session(tmp_path / "raw", direction="long", outcome="target")
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    episodes_path, labels_path = write_episode_artifacts(
        result,
        processed_root=tmp_path / "processed",
        labels_root=tmp_path / "labels",
    )
    assert result.accepted_candidates == 0
    assert result.decisions
    assert episodes_path.read_text(encoding="utf-8") == ""
    assert labels_path.read_text(encoding="utf-8") == ""


def test_builder_does_not_modify_raw_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_direction(monkeypatch, "long")
    session_dir = _simple_session(tmp_path, direction="long", outcome="target")
    before = {
        path.relative_to(session_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in session_dir.rglob("*")
        if path.is_file()
    }
    build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    after = {
        path.relative_to(session_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in session_dir.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_existing_strategy_accepts_clean_long_sequence(tmp_path: Path) -> None:
    session_dir = _actual_clean_long_session(tmp_path)
    result = build_episodes(
        session_dir,
        session_id=session_dir.name,
        provenance="REAL_DELAYED",
        config=EpisodeConfig(warmup_events=1, decision_stride=1, warmup_span_seconds=0, evaluation_interval_ms=0, depth_sample_interval_ms=0),
    )
    accepted_long = [decision for decision in result.decisions if decision.accepted and decision.direction == "long"]
    assert accepted_long, result.rejected_condition_tally
    assert any(episode.outcome == "target_first" for episode in result.episodes)


def _force_direction(monkeypatch: pytest.MonkeyPatch, accepted_direction: str) -> None:
    def derive(
        snapshots: object,
        direction: TradeDirection,
        level_tracker: object,
        thresholds: object,
        *,
        stop_buffer_points: Decimal,
        news_lockout_active: bool = False,
    ) -> DerivedContext:
        del snapshots, level_tracker, thresholds, stop_buffer_points, news_lockout_active
        if direction == TradeDirection.LONG:
            defended, stop, target = Decimal("100"), Decimal("99"), Decimal("101.5")
        else:
            defended, stop, target = Decimal("100.25"), Decimal("101"), Decimal("98.5")
        context = OrderFlowPlanContext(
            direction=direction,
            levels=StrategyLevels(psychological_interval=None),
            defended_level_price=defended,
            stop_price=stop,
            target_price=target,
            session_open_timestamp_ns=0,
        )
        return DerivedContext(context=context, defending_block=None, direction_of_liquidity=None)

    def evaluate(
        snapshots: object,
        context: OrderFlowPlanContext,
        thresholds: object,
    ) -> SetupEvaluationResult:
        del snapshots, thresholds
        passed = context.direction.value == accepted_direction
        return SetupEvaluationResult(
            setup_name="ORDER FLOW PLAN",
            conditions=(SetupConditionResult("forced_test_condition", passed, "test condition"),),
        )

    monkeypatch.setattr(builder_module, "derive_strategy_context", derive)
    monkeypatch.setattr(builder_module, "evaluate_day_trading_plan", evaluate)


def _simple_session(root: Path, *, direction: str, outcome: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=root, session_start_utc=start)
    recorder.record_control_event({"type": "delayed_mode", "timestamp_ns": _ns(start), "delay_minutes": 15})
    base = _ns(start)
    events: list[dict[str, object]] = [
        _depth(base + 1, "bid", "100.00", "0", "100"),
        _depth(base + 2, "ask", "100.25", "0", "100"),
        _depth(base + 3, "bid", "100.00", "100", "101"),
        _trade(base + 3, "100.25", "buy", 1),
        _trade(base + 4, "100.25" if direction == "long" else "100.00", "buy", 2),
    ]
    if outcome == "target":
        price = "101.50" if direction == "long" else "98.50"
        events += [_trade(base + 5, price, "buy", 3), _trade(base + 6, "100.50", "buy", 4)]
    elif outcome == "stop":
        price = "99.00" if direction == "long" else "101.00"
        events += [_trade(base + 5, price, "sell", 3), _trade(base + 6, "100.50", "sell", 4)]
    elif outcome == "ambiguous":
        events += [
            _trade(base + 5, "101.50", "buy", 3),
            _trade(base + 5, "99.00", "sell", 4),
            _trade(base + 6, "100.50", "buy", 5),
        ]
    elif outcome == "timeout":
        events += [_trade(base + 2_000_000_005, "100.50", "buy", 3)]
    elif outcome != "unfinished":
        raise AssertionError(outcome)
    for event in events:
        recorder.record(event)
    recorder.finalize(clean_shutdown=True)
    return recorder.session_dir


def _actual_clean_long_session(root: Path) -> Path:
    start = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=root, session_start_utc=start)
    recorder.record_control_event({"type": "delayed_mode", "timestamp_ns": _ns(start), "delay_minutes": 15})
    base = _ns(start)
    events = [
        _depth(base, "bid", "100.00", "0", "120"),
        _depth(base, "bid", "99.75", "0", "70"),
        _depth(base, "ask", "100.25", "0", "100"),
        _depth(base, "ask", "102.00", "0", "120"),
        _trade(base + 1_000_000_000, "100.00", "sell", 1, "420"),
        _depth(base + 1_000_000_001, "bid", "100.00", "120", "20"),
        _depth(base + 2_000_000_000, "bid", "100.00", "20", "135"),
        _depth(base + 3_000_000_000, "ask", "100.25", "100", "10"),
        _depth(base + 4_000_000_000, "ask", "100.25", "10", "0"),
        _depth(base + 4_000_000_001, "ask", "100.75", "0", "40"),
        _depth(base + 4_000_000_002, "bid", "100.50", "0", "70"),
        _depth(base + 4_000_000_003, "bid", "100.00", "135", "130"),
        _trade(base + 5_000_000_000, "100.75", "buy", 2, "80"),
        _depth(base + 5_000_000_001, "bid", "100.00", "130", "140"),
        _trade(base + 6_000_000_000, "100.75", "buy", 3, "70"),
        _trade(base + 7_000_000_000, "100.75", "buy", 4),
        _trade(base + 8_000_000_000, "102.00", "buy", 5),
        _trade(base + 9_000_000_000, "101.00", "sell", 6),
    ]
    for event in events:
        recorder.record(event)
    recorder.finalize(clean_shutdown=True)
    return recorder.session_dir


def _remove_receive_order(session_dir: Path) -> None:
    for name in ("depth.parquet", "trades.parquet"):
        path = session_dir / name
        table = pq.read_table(path)
        table = table.drop(["receive_sequence"])
        pq.write_table(table, path)


def _depth(timestamp: int, side: str, price: str, previous: str, new: str) -> dict[str, object]:
    return {
        "type": "depth_update", "timestamp": timestamp, "symbol": "MNQ", "side": side,
        "price": price, "previous_size": previous, "new_size": new,
    }


def _trade(
    timestamp: int,
    price: str,
    side: str,
    sequence_id: int,
    size: str = "1",
) -> dict[str, object]:
    return {
        "type": "trade", "timestamp_ns": timestamp, "price": price, "size": size,
        "aggressor_side": side, "instrument": "MNQ", "sequence_id": sequence_id,
    }


def _ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000


# --- passive (maker) offline entry -------------------------------------------
# The offline builder can label entries at the maker touch (no spread), matching
# the live paper engine, with honest no-fills (a setup whose limit is never
# reached is not an episode). These unit-test the entry state machine directly.

_SEC_NS = 1_000_000_000
_LIMIT_CFG = EpisodeConfig(entry_order_type="limit", entry_limit_timeout_seconds=Decimal("45"),
                           entry_limit_cancel_ticks=Decimal("8"))


class _StubAudit:
    def __init__(self) -> None:
        self.entry_status = ""
        self.reasons: list[str] = []


def _book(bid: str, ask: str) -> MarketState:
    state = MarketState()
    state = state.update({"type": "depth_update", "side": "bid", "price": bid,
                          "previous_size": "0", "new_size": "100", "timestamp": 1000})
    return state.update({"type": "depth_update", "side": "ask", "price": ask,
                         "previous_size": "0", "new_size": "100", "timestamp": 1000})


def _mk_trade(price: str, ts_ns: int) -> ReplayEvent:
    return ReplayEvent(timestamp_ns=ts_ns, kind="trade",
                       payload={"price": price, "instrument": "MNQ", "size": "1",
                                "aggressor_side": "sell", "sequence_id": 1, "timestamp_ns": ts_ns})


def _maker_pending(direction: str = "long") -> "builder_module._Pending":
    stop, target = (Decimal("29490"), Decimal("29520")) if direction == "long" \
        else (Decimal("29510"), Decimal("29480"))
    return builder_module._Pending(
        setup_id="s", direction=direction, defended_price=Decimal("29500"),
        stop=stop, target=target, decision_ts_ns=1_000, decision_event_index=5,
        start_event_index=1, decision_hash="h", source_hasher=hashlib.sha256(), audit_index=0)


def _attempt(item, audit, state, event, cfg=_LIMIT_CFG, idx=6):  # noqa: ANN001
    return builder_module._attempt_maker_entry(item, audit, state, event, idx, (), cfg)


def test_maker_offline_entry_fills_at_the_touch_without_slippage() -> None:
    item, audit = _maker_pending("long"), _StubAudit()
    book = _book("29500.00", "29500.25")
    # First trade only CAPTURES the resting price (the bid); it never also fills.
    assert _attempt(item, audit, book, _mk_trade("29500.12", 2_000)) is True
    assert item.maker_limit_price == Decimal("29500.00")
    assert item.awaiting_entry is True
    # A later trade reaches the bid -> maker fill AT 29500.00, no adverse slippage.
    assert _attempt(item, audit, book, _mk_trade("29500.00", 3_000)) is True
    assert item.awaiting_entry is False
    assert item.entry == Decimal("29500.00")
    assert audit.entry_status == "filled"


def test_maker_offline_short_fills_at_the_ask() -> None:
    item, audit = _maker_pending("short"), _StubAudit()
    book = _book("29499.75", "29500.00")
    _attempt(item, audit, book, _mk_trade("29499.88", 2_000))          # capture ask
    assert item.maker_limit_price == Decimal("29500.00")
    assert _attempt(item, audit, book, _mk_trade("29500.00", 3_000)) is True
    assert item.awaiting_entry is False
    assert item.entry == Decimal("29500.00")


def test_maker_offline_unfilled_on_timeout_is_not_an_episode() -> None:
    item, audit = _maker_pending("long"), _StubAudit()
    book = _book("29500.00", "29500.25")
    _attempt(item, audit, book, _mk_trade("29500.12", 2_000))          # capture
    # Price stays above the bid; a trade past the 45s window abandons the rest.
    assert _attempt(item, audit, book, _mk_trade("29500.25", 2_000 + 46 * _SEC_NS)) is False
    assert item.awaiting_entry is True  # never filled -> no episode
    assert audit.entry_status == "rejected_unfilled_maker"


def test_maker_offline_unfilled_when_price_runs_away() -> None:
    cfg = EpisodeConfig(entry_order_type="limit", entry_limit_timeout_seconds=Decimal("0"),
                        entry_limit_cancel_ticks=Decimal("2"))
    item, audit = _maker_pending("long"), _StubAudit()
    book = _book("29500.00", "29500.25")
    _attempt(item, audit, book, _mk_trade("29500.12", 2_000), cfg=cfg)  # capture 29500
    # Price runs 3 ticks (0.75) up, away from the resting bid -> abandoned.
    assert _attempt(item, audit, book, _mk_trade("29500.75", 3_000), cfg=cfg) is False
    assert audit.entry_status == "rejected_unfilled_maker"


def test_maker_offline_require_trade_through_needs_a_through_print() -> None:
    cfg = EpisodeConfig(entry_order_type="limit", entry_limit_timeout_seconds=Decimal("45"),
                        entry_require_trade_through=True)
    item, audit = _maker_pending("long"), _StubAudit()
    book = _book("29500.00", "29500.25")
    _attempt(item, audit, book, _mk_trade("29500.12", 2_000), cfg=cfg)  # capture
    # A mere touch at the bid does not fill under the strict rule.
    assert _attempt(item, audit, book, _mk_trade("29500.00", 3_000), cfg=cfg) is True
    assert item.awaiting_entry is True
    # Trading strictly through the bid fills, at the limit.
    assert _attempt(item, audit, book, _mk_trade("29499.75", 4_000), cfg=cfg) is True
    assert item.awaiting_entry is False
    assert item.entry == Decimal("29500.00")

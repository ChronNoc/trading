"""The governor inside the paper engine: it can veto, shrink and flatten, never loosen anything."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.paper.execution import MarketTick
from app.paper.models import CloseReason, Direction, PaperOrderIntent, RiskDecision, SetupProvenance
from app.paper.options import read_governor_policy
from app.paper.streaming_engine import DelayedPaperEngine
from app.risk.lucid_governor import NEW_YORK, EdgeEvidence, EdgeState, GovernorPolicy

NS = 1_000_000_000
DAY = "2026-11-09"
TICK = D("0.25")


def at(hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(2026, 11, 9, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC).timestamp()) * NS


def tick(index: int, price: str, ts_ns: int) -> MarketTick:
    return MarketTick(event_index=index, ts_ns=ts_ns, price=D(price),
                      best_bid=D(price) - TICK, best_ask=D(price) + TICK)


def intent(stop_ticks: int = 57, target_ticks: int = 114, entry: str = "29200.00") -> PaperOrderIntent:
    price = D(entry)
    return PaperOrderIntent(
        direction=Direction.LONG, entry_reference=price,
        stop=price - TICK * stop_ticks, target=price + TICK * target_ticks,
        provenance=SetupProvenance(session_id="s", setup_id=f"s:{stop_ticks}:{target_ticks}",
                                   strategy_version="v", contract="MNQ", decision_event_index=0,
                                   decision_ts_ns=0, conditions_passed=()))


def approved(contracts: int) -> RiskDecision:
    return RiskDecision(approved=True, contracts=contracts, reason_code="approved", reason="risk approved")


def governed(**policy: object) -> DelayedPaperEngine:
    return DelayedPaperEngine(governor_policy=GovernorPolicy(**policy))  # type: ignore[arg-type]


# --- off by default, so nothing that exists today changes -------------------------------------


def test_an_engine_without_a_policy_has_no_governor_and_behaves_as_before() -> None:
    engine = DelayedPaperEngine()
    assert engine._governor is None and engine._book is None
    engine._book_closed(object())  # type: ignore[arg-type]  # a no-op, not an error
    engine._govern(tick(1, "29200", at(11)))


# --- entries: veto, shrink, never loosen, fail closed --------------------------------------------


def test_an_entry_outside_the_window_is_vetoed_with_the_governors_reason_code() -> None:
    engine = governed()
    verdict = engine._govern_entry(approved(1), intent(), tick(1, "29200", at(20)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_outside_entry_window"


def test_an_entry_the_generic_engine_sized_at_four_is_shrunk_to_what_the_budget_allows() -> None:
    engine = governed()
    verdict = engine._govern_entry(approved(4), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert verdict.approved and verdict.contracts == 1
    assert "governor:" in verdict.reason and verdict.reason.startswith("risk approved")


def test_the_governor_never_asks_for_more_contracts_than_the_generic_engine_approved() -> None:
    engine = governed()
    proven = EdgeEvidence(EdgeState.PROVEN, 150, D("0.3"), D("0.2"), D("0.4"), D("2"))
    engine._book.edge_cache = proven  # type: ignore[union-attr]
    tight = intent(stop_ticks=14, target_ticks=40)
    assert engine._govern_entry(approved(2), tight, tick(1, "29200", at(10, 30)), DAY).contracts == 2
    assert engine._govern_entry(approved(9), tight, tick(1, "29200", at(10, 30)), DAY).contracts == 3


def test_a_setup_whose_target_cannot_pay_for_its_costs_is_vetoed() -> None:
    engine = governed()
    verdict = engine._govern_entry(approved(1), intent(target_ticks=6), tick(1, "29200", at(10, 30)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_trade_quality"


def test_if_the_governor_itself_breaks_entries_are_blocked_not_waved_through() -> None:
    engine = governed()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broken")

    engine._governor.check_entry = boom  # type: ignore[union-attr,method-assign]
    verdict = engine._govern_entry(approved(1), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_error"
    assert "failed closed" in verdict.reason


# --- positions: flatten and lock ------------------------------------------------------------------


def open_position(engine: DelayedPaperEngine, entry: str = "29200.00", when: int | None = None) -> None:
    """Open one contract through the real executor with a stop far enough not to interfere."""
    wide = intent(stop_ticks=800, target_ticks=1600, entry=entry)
    start = at(10, 30) if when is None else when
    first = tick(1, entry, start)
    engine._executor.submit(wide, approved(1), first, trading_day=DAY)
    engine._tick = lambda: engine._latest  # type: ignore[method-assign,attr-defined]
    engine._latest = first  # type: ignore[attr-defined]
    nxt = tick(2, entry, start + NS)
    engine._latest = nxt  # type: ignore[attr-defined]
    engine._advance_execution(nxt)


def test_the_governor_books_the_entry_the_executor_filled() -> None:
    engine = governed()
    open_position(engine)
    assert engine._executor.position is not None
    assert engine._book.day_entries == 1 and engine._book.last_entry_ts_ns is not None  # type: ignore[union-attr]


def test_the_day_loss_budget_flattens_the_open_position_and_locks_the_rest_of_the_day() -> None:
    engine = governed()
    open_position(engine)
    entry = engine._executor.position.entry_price  # type: ignore[union-attr]
    crash = tick(3, str(entry - D("125")), at(10, 31))   # one contract: -125 points = -$250 open loss
    engine._latest = crash  # type: ignore[attr-defined]
    engine._advance_execution(crash)
    assert engine._executor.position is None
    last = engine._executor.trades[-1]
    assert last.close_reason is CloseReason.RISK_GOVERNOR
    assert engine._book.day_locked_reason == "governor_day_loss_budget"  # type: ignore[union-attr]
    assert engine._book.balance == engine._executor.balance                # the book saw the close
    later = engine._govern_entry(approved(1), intent(), tick(4, "29200", at(11)), DAY)
    assert not later.approved and later.reason_code == "governor_day_locked"


def test_the_consistency_cap_takes_the_profit_and_stops_for_the_day() -> None:
    engine = governed()
    open_position(engine)
    entry = engine._executor.position.entry_price  # type: ignore[union-attr]
    spike = tick(3, str(entry + D("251")), at(10, 31))   # +$502 open profit: past the $500 cap
    engine._latest = spike  # type: ignore[attr-defined]
    engine._advance_execution(spike)
    assert engine._executor.position is None
    assert engine._executor.trades[-1].close_reason is CloseReason.RISK_GOVERNOR
    assert engine._book.day_locked_reason == "governor_day_profit_cap"  # type: ignore[union-attr]


def test_the_position_is_flat_before_lucids_auto_close() -> None:
    engine = governed()
    open_position(engine, when=at(16, 39))     # opened a minute before the flat time
    late = tick(3, "29200.00", at(16, 40))
    engine._latest = late  # type: ignore[attr-defined]
    engine._advance_execution(late)
    assert engine._executor.position is None
    assert engine._executor.trades[-1].close_reason is CloseReason.RISK_GOVERNOR


def test_an_account_at_the_floor_locks_the_executor_for_good() -> None:
    engine = governed()
    engine._book.balance = D("24000")  # type: ignore[union-attr]
    engine._govern(tick(1, "29200", at(11)))
    blocked = engine._executor.gate(intent(), tick(2, "29200", at(11, 5)), trading_day=DAY)
    assert blocked is not None and blocked.reason_code == "drawdown_lock"


def test_a_retired_account_stops_trading() -> None:
    engine = governed()
    book = engine._book
    book.balance = book.day_start_balance = D("24250")  # type: ignore[union-attr]  # only $250 of room left
    engine._govern(tick(1, "29200", at(11)))
    assert engine._executor.gate(intent(), tick(2, "29200", at(11, 5)), trading_day=DAY) is not None


def test_a_governor_error_while_watching_never_kills_the_stream() -> None:
    engine = governed()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broken")

    engine._governor.check_position = boom  # type: ignore[union-attr,method-assign]
    engine._advance_execution(tick(1, "29200", at(11)))
    assert "risk governor error" in engine.status().error


# --- configuration --------------------------------------------------------------------------------


def test_a_missing_governor_file_gives_the_strict_defaults_and_a_disabled_one_gives_none(tmp_path: Path) -> None:
    assert read_governor_policy(tmp_path / "absent.yaml") == GovernorPolicy()
    off = tmp_path / "off.yaml"
    off.write_text("enabled: false\n", encoding="utf-8")
    assert read_governor_policy(off) is None


def test_a_broken_governor_file_falls_back_to_the_strict_defaults_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("day_loss_fractoin: 0.2\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        policy = read_governor_policy(bad)
    assert policy == GovernorPolicy() and "strict defaults" in caplog.text


def test_a_governor_file_that_weakens_the_rules_falls_back_to_the_strict_defaults(tmp_path: Path) -> None:
    weak = tmp_path / "weak.yaml"
    weak.write_text("day_loss_fraction: 0.95\n", encoding="utf-8")
    assert read_governor_policy(weak) == GovernorPolicy()


def test_both_production_start_up_paths_hand_the_policy_to_the_engine() -> None:
    for path in ("tools/start_backend.py", "tools/start_assistant.py"):
        source = Path(path).read_text(encoding="utf-8")
        assert source.count('governor_policy=read_governor_policy(Path("config/risk_governor.yaml"))') == 1, path


# --- the real entry path the production engine uses ---------------------------------------------------


def candidate() -> tuple[object, object]:
    """A setup shaped like the strategy's own: record + derived context, stop 57 / target 114 ticks."""
    from types import SimpleNamespace

    from app.strategy.order_flow import TradeDirection

    record = SimpleNamespace(
        direction=TradeDirection.LONG.value, setup_id="s:cand", strategy_version="v",
        event_index=1, evaluated_at_ns=0, conditions=())
    derived = SimpleNamespace(context=SimpleNamespace(stop_price=D("29185.75"), target_price=D("29228.50")))
    return record, derived


def bound(engine: DelayedPaperEngine) -> DelayedPaperEngine:
    engine.bind_session("s", "MNQZ6")
    return engine


def test_the_full_entry_path_places_one_contract_inside_the_window_and_every_limit() -> None:
    engine = bound(governed())
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(10, 30)))  # type: ignore[arg-type]
    pending = engine._executor.pending
    assert pending is not None and pending.contracts == 1
    assert "governor:" in pending.reason and not engine._risk_rejections


def test_the_full_entry_path_refuses_outside_the_window_and_counts_the_governors_reason() -> None:
    engine = bound(governed())
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(20)))  # type: ignore[arg-type]
    assert engine._executor.pending is None
    assert engine._risk_rejections["governor_outside_entry_window"] == 1


def test_the_same_setup_at_the_same_time_is_taken_when_there_is_no_governor() -> None:
    """The control: without the governor nothing in the generic engine objects at 20:00."""
    engine = bound(DelayedPaperEngine())
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(20)))  # type: ignore[arg-type]
    assert engine._executor.pending is not None and not engine._risk_rejections


def test_a_second_setup_straight_after_the_first_is_blocked_by_the_governors_spacing() -> None:
    engine = bound(governed(cooldown_after_loss_seconds=0))
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(10, 30)))  # type: ignore[arg-type]
    engine._advance_execution(tick(6, "29200", at(10, 30, 1)))               # fills the first order
    assert engine._executor.position is not None
    record.setup_id = "s:second"  # type: ignore[attr-defined]
    engine._executor.liquidate(tick(7, "29200", at(10, 30, 2)))
    engine._book_closed(engine._executor.trades[-1])
    engine._consider_entry(record, derived, tick(8, "29200", at(10, 30, 30)))  # type: ignore[arg-type]
    blocked = sum(count for code, count in engine._risk_rejections.items() if code.startswith("governor_") or code == "setup_cooldown")
    assert blocked == 1 and engine._executor.pending is None

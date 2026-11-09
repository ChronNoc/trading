"""The red-team fix pack (2026-11-08): each test pins one confirmed hole as closed.

Restart continuity, strict settings, DATA_GAP booking, the governor-error fail-safe, the
backwards day roll, net-profit consistency, the minimum trading days, the recoverable
microscalping guard (minimum hold before profit), the half-spread entry cost and overnight
flattening.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.paper.execution import ExecutionConfig, MarketTick, PaperExecutor
from app.paper.models import CloseReason, Direction, PaperOrderIntent, RiskDecision, SetupProvenance
from app.paper.options import read_governor_policy
from app.paper.streaming_engine import DelayedPaperEngine
from app.risk.account_profile import lucid_flex_25k
from app.risk.governor_state import default_state_path, load_book_state, save_book_state
from app.risk.lucid_governor import (
    NEW_YORK,
    AccountStatus,
    ClosedTrade,
    GovernorBook,
    GovernorPolicy,
    LucidGovernor,
    PositionAction,
    consistency_share,
)

NS = 1_000_000_000
TICK = D("0.25")
DAY = "2026-11-09"
PROFILE = lucid_flex_25k()


def at(hour: int, minute: int = 0, second: int = 0, day: int = 9) -> int:
    return int(datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC).timestamp()) * NS


def tick(index: int, price: str, ts_ns: int, spread_ticks: int = 2) -> MarketTick:
    """A priced event; the book straddles the price by ``spread_ticks`` in total."""
    mid = D(price)
    half = TICK * spread_ticks / 2
    return MarketTick(event_index=index, ts_ns=ts_ns, price=mid, best_bid=mid - half, best_ask=mid + half)


def intent(stop_ticks: int = 57, target_ticks: int = 114, entry: str = "29200.00", setup: str = "s") -> PaperOrderIntent:
    price = D(entry)
    return PaperOrderIntent(
        direction=Direction.LONG, entry_reference=price,
        stop=price - TICK * stop_ticks, target=price + TICK * target_ticks,
        provenance=SetupProvenance(session_id="s", setup_id=setup, strategy_version="v", contract="MNQZ6",
                                   decision_event_index=0, decision_ts_ns=0, conditions_passed=()))


def approved(contracts: int = 1) -> RiskDecision:
    return RiskDecision(approved=True, contracts=contracts, reason_code="approved", reason="risk approved")


def governed(state_path: Path | None = None, **policy: object) -> DelayedPaperEngine:
    engine = DelayedPaperEngine(
        governor_policy=GovernorPolicy(**policy), governor_state_path=state_path)  # type: ignore[arg-type]
    engine.bind_session("s", "MNQZ6")
    return engine


def round_trip(engine: DelayedPaperEngine, index: int, opened: int, closed: int, exit_price: str) -> None:
    """Open one contract at 29200 and close it at ``exit_price``, through the engine's own paths."""
    engine._executor.submit(intent(setup=f"s{index}"), approved(), tick(index, "29200", opened),
                            trading_day=DAY)
    engine._advance_execution(tick(index + 1, "29200", opened + NS))        # fills, books the entry
    trade = engine._executor.liquidate(tick(index + 2, exit_price, closed))
    assert trade is not None
    engine._book_closed(trade)


# --- 1. a restart continues the same account --------------------------------------------------


def test_a_new_attempt_writes_its_state_file_at_once(tmp_path: Path) -> None:
    path = default_state_path(tmp_path / "paper_trades.jsonl")
    governed(path)
    state = load_book_state(path)
    assert state is not None and D(str(state["balance"])) == D("25000")


def test_a_restart_keeps_the_balance_the_streak_and_the_day(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    first = governed(path)
    for number, minute in enumerate((0, 10, 20)):
        round_trip(first, number * 10, at(10, minute), at(10, minute + 1), "29190")
    assert first._book.losing_streak == 3                                   # type: ignore[union-attr]
    balance = first._executor.balance

    second = governed(path)                                                 # the restart
    assert second._book.losing_streak == 3                                  # type: ignore[union-attr]
    assert second._book.balance == balance == second._executor.balance      # type: ignore[union-attr]
    assert second._book.day == DAY and second._book.day_entries == 3        # type: ignore[union-attr]
    verdict = second._govern_entry(approved(), intent(setup="later"), tick(50, "29200", at(11)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_losing_streak"


def test_a_retired_account_stays_retired_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    first = governed(path)
    first._book.balance = first._book.day_start_balance = D("24250")         # type: ignore[union-attr]
    first._save_governor_state()
    second = governed(path)
    assert second._executor._locked_out
    verdict = second._govern_entry(approved(), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_account_not_active"


def test_the_high_water_mark_and_the_day_lock_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    first = governed(path)
    book = first._book
    assert book is not None
    book.roll_day("2026-11-06")
    book.record_trade(ClosedTrade(at(10, day=6), at(11, day=6), D("400"), D("1")))
    book.roll_day(DAY)
    book.lock_day("governor_day_loss_budget")
    first._save_governor_state()
    second = governed(path)
    assert second._book.highest_closing_balance == D("25400")              # type: ignore[union-attr]
    assert second._book.day_locked_reason == "governor_day_loss_budget"     # type: ignore[union-attr]


def test_an_unreadable_state_file_refuses_entries_and_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    path.write_text("{ not json", encoding="utf-8")
    engine = governed(path)
    verdict = engine._govern_entry(approved(), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert not verdict.approved and verdict.reason_code == "governor_state_unreadable"
    assert "removing it starts a new evaluation attempt" in engine.status().error
    engine._save_governor_state()
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_a_state_file_for_another_account_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    state = GovernorBook(PROFILE, GovernorPolicy()).to_state()
    state["profile_id"] = "some_other_account"
    save_book_state(path, state)
    engine = governed(path)
    verdict = engine._govern_entry(approved(), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert verdict.reason_code == "governor_state_unreadable"


def test_the_book_round_trips_through_its_state_document_exactly() -> None:
    policy = GovernorPolicy()
    book = GovernorBook(PROFILE, policy)
    book.roll_day("2026-11-06")
    book.record_entry(at(10, day=6))
    book.record_trade(ClosedTrade(at(10, day=6), at(10, 0, 3, day=6), D("54.76"), D("1.37")))
    book.roll_day(DAY)
    book.record_entry(at(10))
    book.record_trade(ClosedTrade(at(10), at(10, 5), D("-30.74"), D("-1")))
    book.lock_day("governor_losing_streak")
    document = json.loads(json.dumps(book.to_state()))
    restored = GovernorBook.from_state(PROFILE, policy, document)
    assert restored.to_state() == book.to_state()
    assert restored.snapshot(at(11)) == book.snapshot(at(11))


def test_saving_is_atomic_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    save_book_state(path, {"a": 1})
    save_book_state(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
    assert not [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_every_entry_and_close_is_saved_as_it_happens(tmp_path: Path) -> None:
    path = tmp_path / "governor_state.json"
    engine = governed(path)
    engine._executor.submit(intent(), approved(), tick(1, "29200", at(10)), trading_day=DAY)
    engine._advance_execution(tick(2, "29200", at(10, 0, 1)))
    assert load_book_state(path)["day_entries"] == 1                      # type: ignore[index]
    trade = engine._executor.liquidate(tick(3, "29190", at(10, 2)))
    engine._book_closed(trade)                                             # type: ignore[arg-type]
    assert load_book_state(path)["losing_streak"] == 1                    # type: ignore[index]


# --- 2. strict settings ---------------------------------------------------------------------------


def test_the_shipped_settings_file_loads_cleanly_as_the_defaults() -> None:
    policy = read_governor_policy(Path("config/risk_governor.yaml"))
    assert policy == GovernorPolicy() and not policy.config_error


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_entries_per_day": 100_000}, {"max_losing_streak": 1000},
        {"cooldown_after_loss_seconds": 0}, {"min_seconds_between_entries": 0},
        {"min_net_reward_risk": D("0")}, {"micro_hold_seconds": -1}, {"micro_hold_seconds": 4},
        {"edge_min_profit_factor": D("0")}, {"edge_min_profit_factor": D("NaN")},
        {"contracts_when_proven": 20}, {"hard_stop_buffer_fraction": D("0.0001")},
        {"retire_buffer_fraction": D("0.0001")},
        {"retire_buffer_fraction": D("0.20"), "hard_stop_buffer_fraction": D("0.20")},
        {"flat_time": __import__("datetime").time(17, 30)},
        {"edge_window_trades": 5000},
    ],
)
def test_no_setting_can_be_loosened_past_its_bound(overrides: dict) -> None:
    with pytest.raises(ValueError):
        GovernorPolicy(**overrides)


def test_a_config_error_refuses_entries_but_the_rules_still_watch_positions() -> None:
    governor = LucidGovernor(PROFILE, GovernorPolicy(config_error="bad file"))
    book = GovernorBook(PROFILE, governor.policy)
    book.roll_day(DAY)
    from app.risk.lucid_governor import EntryRequest

    decision = governor.check_entry(book.snapshot(at(10, 30)),
                                    EntryRequest(D(57), D(114), D("0.50"), D("1.24"), D(2), 20))
    assert not decision.allowed and decision.reason_code == "governor_config_invalid"
    late = governor.check_position(book.snapshot(at(16, 41)))
    assert late.action is PositionAction.FLATTEN


# --- 3. losses the governor used to miss ----------------------------------------------------------


def test_a_data_gap_close_is_booked_by_the_governor() -> None:
    engine = governed()
    engine._executor.submit(intent(stop_ticks=800, target_ticks=1600), approved(), tick(1, "29200", at(10, 30)),
                            trading_day=DAY)
    latest = {"tick": tick(2, "29200", at(10, 30, 1))}
    engine._tick = lambda: latest["tick"]  # type: ignore[method-assign]
    engine._advance_execution(latest["tick"])
    assert engine._executor.position is not None
    latest["tick"] = tick(3, "29150", at(10, 31))
    engine._gap_pending = True
    engine._resolve_causality_gap()
    last = engine._executor.trades[-1]
    assert last.close_reason is CloseReason.DATA_GAP and last.net_pnl < 0
    assert engine._book.balance == engine._executor.balance < D("25000")   # type: ignore[union-attr]
    assert len(engine._book.trades) == 1                                  # type: ignore[union-attr]


def test_a_governor_error_with_a_position_open_flattens_locks_and_stops_entries() -> None:
    engine = governed()
    latest = {"tick": tick(1, "29200", at(10, 30))}
    engine._tick = lambda: latest["tick"]  # type: ignore[method-assign]
    engine._executor.submit(intent(), approved(), latest["tick"], trading_day=DAY)
    latest["tick"] = tick(2, "29200", at(10, 30, 1))
    engine._advance_execution(latest["tick"])
    assert engine._executor.position is not None

    def boom(snapshot: object) -> object:
        raise RuntimeError("broken rule")

    engine._governor.check_position = boom  # type: ignore[union-attr,method-assign]
    latest["tick"] = tick(3, "29195", at(10, 31))
    engine._advance_execution(latest["tick"])
    assert engine._executor.position is None
    assert engine._executor.trades[-1].close_reason is CloseReason.RISK_GOVERNOR
    assert engine._book.day_locked_reason == "governor_error"             # type: ignore[union-attr]
    assert engine._executor._locked_out
    assert "position flattened and entries locked" in engine.status().error


def test_a_stale_event_from_yesterday_cannot_reopen_the_day_or_erase_its_result() -> None:
    book = GovernorBook(PROFILE, GovernorPolicy())
    book.roll_day("2026-11-09")
    book.record_trade(ClosedTrade(at(10), at(11), D("375"), D("1")))
    assert book.roll_day("2026-11-10")
    book.record_trade(ClosedTrade(at(10, day=10), at(11, day=10), D("-260"), D("-1")))
    book.lock_day("governor_day_loss_budget")
    assert not book.roll_day("2026-11-09")                                  # the stale event
    assert book.day == "2026-11-10" and book.day_locked_reason == "governor_day_loss_budget"
    assert book.day_realized == D("-260")
    book.roll_day("2026-11-11")
    assert book.day_pnls == {"2026-11-09": D("375"), "2026-11-10": D("-260")}


# --- 4. consistency, minimum days, microscalping ------------------------------------------------


def test_consistency_uses_net_account_profit_as_lucid_does() -> None:
    days = {"a": D("500"), "b": D("-300"), "c": D("500"), "d": D("550")}   # net $1,250, best day 44%
    assert consistency_share(days) == D("0.44")
    assert PROFILE.consistency_breached(days)
    governor = LucidGovernor(PROFILE)
    book = GovernorBook(PROFILE, governor.policy)
    t = at(10, day=2)
    for number, (day, pnl) in enumerate(zip(("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"),
                                            days.values())):
        book.roll_day(day)
        book.record_entry(t + number * 86_400 * NS)
        book.record_trade(ClosedTrade(t, t + 60 * NS, pnl, D("1")))
    assert book.balance == D("26250")
    assert governor.status(book.snapshot(at(11, day=5))) is AccountStatus.ACTIVE   # not PASSED


def test_a_pass_needs_the_minimum_trading_days() -> None:
    governor = LucidGovernor(PROFILE)
    book = GovernorBook(PROFILE, governor.policy)
    book.roll_day(DAY)
    book.balance = D("26250")
    snapshot = replace(book.snapshot(at(11)), best_day_share=D("0.30"))
    assert governor.status(replace(snapshot, traded_days=2)) is AccountStatus.ACTIVE
    assert governor.status(replace(snapshot, traded_days=3)) is AccountStatus.PASSED
    assert PROFILE.min_trading_days == 3


def test_the_microscalp_share_is_measured_against_net_profit() -> None:
    book = GovernorBook(PROFILE, GovernorPolicy())
    book.roll_day(DAY)
    book.record_trade(ClosedTrade(at(10), at(10, 0, 3), D("100"), D("1")))     # a 3 s winner
    book.record_trade(ClosedTrade(at(10, 5), at(10, 10), D("200"), D("1")))
    book.record_trade(ClosedTrade(at(10, 20), at(10, 30), D("-200"), D("-1")))
    assert book.micro_profit_share() == D("1")                                # $100 of a $100 net profit


def test_one_fast_winner_no_longer_freezes_a_governed_account() -> None:
    from app.risk.lucid_governor import EntryRequest

    blocking = LucidGovernor(PROFILE)
    enforced = LucidGovernor(PROFILE, min_profit_hold_enforced=True)
    book = GovernorBook(PROFILE, blocking.policy)
    book.roll_day(DAY)
    book.record_trade(ClosedTrade(at(10), at(10, 0, 2), D("54.76"), D("1")))   # 100% micro
    request = EntryRequest(D(57), D(114), D("0.50"), D("1.24"), D(2), 20)
    later = book.snapshot(at(11))
    assert "governor_microscalp_guard" in {c.name for c in blocking.check_entry(later, request).failures}
    assert enforced.check_entry(later, request).allowed


def test_the_governed_engine_enforces_the_minimum_hold_and_the_plain_engine_does_not() -> None:
    assert governed()._exec_config.min_profit_hold_ns == 6 * NS
    assert DelayedPaperEngine()._exec_config.min_profit_hold_ns == 0
    assert governed()._governor.min_profit_hold_enforced                        # type: ignore[union-attr]


def _executor(**config: object) -> PaperExecutor:
    return PaperExecutor(starting_balance=D("25000"), max_contracts=20, config=ExecutionConfig(**config))


def test_no_profit_is_taken_before_the_minimum_hold_but_the_stop_still_protects() -> None:
    hold = _executor(min_profit_hold_ns=6 * NS)
    hold.submit(intent(stop_ticks=20, target_ticks=20), approved(), tick(1, "29200", at(10)), trading_day=DAY)
    hold.on_tick(tick(2, "29200", at(10, 0, 1)))                                # filled at 29200.25 + slip
    assert hold.position is not None
    assert hold.on_tick(tick(3, "29210", at(10, 0, 3))) is None                 # target touched at 2 s: held
    closed = hold.on_tick(tick(4, "29210", at(10, 0, 8)))                       # 7 s: taken
    assert closed is not None and closed.close_reason is CloseReason.TARGET

    stop = _executor(min_profit_hold_ns=6 * NS)
    stop.submit(intent(stop_ticks=20, target_ticks=20), approved(), tick(1, "29200", at(10)), trading_day=DAY)
    stop.on_tick(tick(2, "29200", at(10, 0, 1)))
    lost = stop.on_tick(tick(3, "29190", at(10, 0, 2)))                         # stop hit at 1 s: closed
    assert lost is not None and lost.close_reason is CloseReason.STOP


def test_break_even_and_trailing_moves_wait_for_the_minimum_hold() -> None:
    executor = _executor(min_profit_hold_ns=6 * NS, break_even_trigger_ticks=D("8"), break_even_lock_ticks=D("1"))
    executor.submit(intent(stop_ticks=20, target_ticks=200), approved(), tick(1, "29200", at(10)), trading_day=DAY)
    executor.on_tick(tick(2, "29200", at(10, 0, 1)))
    original_stop = executor.position.stop                                      # type: ignore[union-attr]
    executor.on_tick(tick(3, "29205", at(10, 0, 3)))                            # 20 ticks in favour, 2 s old
    assert executor.position.stop == original_stop                              # type: ignore[union-attr]
    executor.on_tick(tick(4, "29205", at(10, 0, 8)))
    assert executor.position.stop > original_stop                               # type: ignore[union-attr]


# --- 5. the per-trade cap counts the half-spread of a market entry ----------------------------------


def test_the_per_trade_cap_includes_the_half_spread_paid_on_entry() -> None:
    engine = governed()
    # 75-tick stop: $37.50 + $1.24 commission + 2 ticks of slippage = $39.74 before the spread.
    tight = engine._govern_entry(approved(4), intent(stop_ticks=75, target_ticks=200),
                                 tick(1, "29200", at(10, 30), spread_ticks=1), DAY)
    assert tight.approved and tight.contracts == 1                              # + half of 1 tick = $39.99
    wide = engine._govern_entry(approved(4), intent(stop_ticks=75, target_ticks=200, setup="w"),
                                tick(2, "29200", at(10, 31), spread_ticks=2), DAY)
    assert not wide.approved and wide.reason_code == "governor_risk_budget"     # + 1 tick = $40.24


# --- 6. never carried overnight -------------------------------------------------------------------


def test_a_position_seen_after_midnight_is_flattened() -> None:
    governor = LucidGovernor(PROFILE)
    book = GovernorBook(PROFILE, governor.policy)
    book.roll_day("2026-11-10")
    assert governor.check_position(book.snapshot(at(0, 30, day=10))).action is PositionAction.FLATTEN
    assert governor.check_position(book.snapshot(at(10, 0, day=10))).action is PositionAction.HOLD

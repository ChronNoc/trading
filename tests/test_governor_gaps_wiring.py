"""Governor wiring inside DelayedPaperEngine the existing tests did not pin (red-team, 2026-11-08).

Each test killed a surviving mutant (the S ids). The cost check includes the half-spread of a
market entry since the fix pack."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import app.paper.streaming_engine as streaming
from app.paper.execution import MarketTick
from app.paper.models import CloseReason, Direction, PaperOrderIntent, RiskDecision, SetupProvenance
from app.paper.streaming_engine import DelayedPaperEngine
from app.risk.lucid_governor import NEW_YORK, GovernorPolicy

NS = 1_000_000_000
DAY = "2026-11-09"
TICK = D("0.25")


def at(hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(2026, 11, 9, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC).timestamp()) * NS


def tick(index: int, price: str, ts_ns: int) -> MarketTick:
    return MarketTick(event_index=index, ts_ns=ts_ns, price=D(price), best_bid=D(price) - TICK, best_ask=D(price) + TICK)


def intent(stop_ticks: int = 57, target_ticks: int = 114, entry: str = "29200.00") -> PaperOrderIntent:
    price = D(entry)
    return PaperOrderIntent(
        direction=Direction.LONG, entry_reference=price, stop=price - TICK * stop_ticks, target=price + TICK * target_ticks,
        provenance=SetupProvenance(session_id="s", setup_id=f"s:{stop_ticks}:{target_ticks}", strategy_version="v",
                                   contract="MNQ", decision_event_index=0, decision_ts_ns=0, conditions_passed=()))


def approved(contracts: int) -> RiskDecision:
    return RiskDecision(approved=True, contracts=contracts, reason_code="approved", reason="risk approved")


def governed(**policy: object) -> DelayedPaperEngine:
    return DelayedPaperEngine(governor_policy=GovernorPolicy(**policy))  # type: ignore[arg-type]


def open_position(engine: DelayedPaperEngine, stop_ticks: int = 800, entry: str = "29200.00", when: int | None = None) -> None:
    wide = intent(stop_ticks=stop_ticks, target_ticks=stop_ticks * 2, entry=entry)
    start = at(10, 30) if when is None else when
    first = tick(1, entry, start)
    engine._executor.submit(wide, approved(1), first, trading_day=DAY)
    engine._tick = lambda: engine._latest  # type: ignore[method-assign,attr-defined]
    engine._latest = first  # type: ignore[attr-defined]
    nxt = tick(2, entry, start + NS)
    engine._latest = nxt  # type: ignore[attr-defined]
    engine._advance_execution(nxt)


def bound(engine: DelayedPaperEngine) -> DelayedPaperEngine:
    engine.bind_session("s", "MNQZ6")
    return engine


def candidate():
    from app.strategy.order_flow import TradeDirection

    record = SimpleNamespace(direction=TradeDirection.LONG.value, setup_id="s:cand", strategy_version="v",
                             event_index=1, evaluated_at_ns=0, conditions=())
    derived = SimpleNamespace(context=SimpleNamespace(stop_price=D("29185.75"), target_price=D("29228.50")))
    return record, derived


def test_gap_the_governor_cannot_turn_a_generic_rejection_into_an_approval() -> None:        # S02
    engine = bound(governed())
    engine._executor.lock_out()                       # the generic gate now rejects every entry
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(10, 30)))  # type: ignore[arg-type]
    assert engine._executor.pending is None
    assert engine._risk_rejections["drawdown_lock"] == 1 and not any(c.startswith("governor_") for c in engine._risk_rejections)


def test_gap_the_governor_sees_the_real_stop_and_all_costs() -> None:                         # S06 S08 S09
    engine = governed()
    config = engine._exec_config
    entry = tick(1, "29200", at(10, 30))
    half_spread = (entry.best_ask - entry.best_bid) / D("0.25") / 2          # fix pack: paid on a market entry
    cost = config.commission_per_contract + (config.entry_slippage_ticks + config.stop_slippage_ticks + half_spread) * engine._instrument.tick_value
    verdict = engine._govern_entry(approved(1), intent(stop_ticks=57, target_ticks=114), entry, DAY)
    expected_risk = D(57) * engine._instrument.tick_value + cost
    assert verdict.approved and f"${expected_risk} risk each" in verdict.reason
    wide = engine._govern_entry(approved(1), intent(stop_ticks=100, target_ticks=300), tick(2, "29200", at(10, 30)), DAY)
    assert not wide.approved and wide.reason_code == "governor_risk_budget"            # $52 risk vs the $40 cap


def test_gap_the_entry_path_rolls_the_trading_day() -> None:                                  # S14
    engine = governed()
    engine._book.roll_day("2026-11-06")
    engine._book.lock_day("yesterday's lock")
    verdict = engine._govern_entry(approved(1), intent(), tick(1, "29200", at(10, 30)), DAY)
    assert verdict.approved                           # a new trading day clears yesterday's lock


def test_gap_a_closed_trade_reaches_the_book_with_its_real_times_pnl_and_r() -> None:          # S16 S17 S18
    engine = governed()
    engine._book.roll_day(DAY)
    trade = SimpleNamespace(opened_ts_ns=at(10), closed_ts_ns=at(10, 5), net_pnl=D("42"), r_multiple=D("1.4"))
    engine._book_closed(trade)  # type: ignore[arg-type]
    booked = engine._book.trades[-1]
    assert (booked.opened_ts_ns, booked.closed_ts_ns, booked.net_pnl, booked.r_multiple) == (at(10), at(10, 5), D("42"), D("1.4"))
    assert engine._book.micro_profit == D("0") and engine._book.balance == D("25042")


def test_gap_a_stop_out_on_the_tick_path_is_booked_by_the_governor() -> None:                 # S19
    engine = governed()
    open_position(engine, stop_ticks=20)
    entry = engine._executor.position.entry_price  # type: ignore[union-attr]
    crash = tick(3, str(entry - D("8")), at(10, 31))
    engine._latest = crash  # type: ignore[attr-defined]
    engine._advance_execution(crash)
    assert engine._executor.position is None and engine._executor.trades[-1].close_reason is not CloseReason.RISK_GOVERNOR
    assert len(engine._book.trades) == 1 and engine._book.balance == engine._executor.balance


def test_gap_an_open_position_is_checked_on_every_tick_not_throttled() -> None:               # S25
    engine = governed()
    open_position(engine)
    entry = engine._executor.position.entry_price  # type: ignore[union-attr]
    crash = tick(3, str(entry - D("125")), at(10, 31))
    engine._governor_checked_ns = crash.ts_ns - 1      # the last check was a nanosecond ago
    engine._latest = crash  # type: ignore[attr-defined]
    engine._advance_execution(crash)
    assert engine._executor.position is None           # still flattened


def test_gap_the_idle_check_runs_every_second_but_is_throttled_inside_it() -> None:           # S26 S27
    engine = governed()
    engine._govern(tick(1, "29200", at(11)))
    engine._book.balance = D("24000")                  # the account hits the floor
    engine._govern(tick(2, "29200", at(11) + NS // 2)) # 0.5 s later: throttled, not looked at yet
    assert engine._executor.gate(intent(), tick(3, "29200", at(11, 0, 1)), trading_day=DAY) is None
    engine._govern(tick(4, "29200", at(11) + 2 * NS))  # 2 s after the first check: looked at
    blocked = engine._executor.gate(intent(), tick(5, "29200", at(11, 0, 3)), trading_day=DAY)
    assert blocked is not None and blocked.reason_code == "drawdown_lock"


def test_gap_sizing_uses_the_governors_closing_balance_trail(monkeypatch) -> None:           # S37
    engine = bound(governed())
    engine._book.highest_closing_balance = D("25800")
    seen = {}
    real = streaming.size_intent

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(streaming, "size_intent", spy)
    record, derived = candidate()
    engine._consider_entry(record, derived, tick(5, "29200", at(10, 30)))  # type: ignore[arg-type]
    assert seen["drawdown_room"] == engine._profile.drawdown_room(engine._executor.balance, D("25800"))


def test_gap_the_engine_uses_the_policy_it_was_given_for_rules_and_book() -> None:             # S42 S43
    policy = GovernorPolicy(max_entries_per_day=3)
    engine = DelayedPaperEngine(governor_policy=policy)
    assert engine._governor.policy is policy and engine._book.policy is policy

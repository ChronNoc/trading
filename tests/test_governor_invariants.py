"""Property / invariant tests for the claims in docs/RISK_GOVERNOR.md (red-team, 2026-11-08).

Seeded `random` (hypothesis is not installed in the venv). Money is Decimal throughout; the only
floats are the random draws, converted to cent Decimals immediately. The overnight test changed
with the fix pack: a position is now flattened outside the holding hours, not only after 16:40.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from app.risk.account_profile import lucid_flex_25k
from app.risk.lucid_governor import (
    AccountSnapshot,
    AccountStatus,
    ClosedTrade,
    EdgeEvidence,
    EdgeState,
    EntryRequest,
    GovernorBook,
    GovernorPolicy,
    LucidGovernor,
    PositionAction,
    assess_edge,
)

NS = 1_000_000_000
NY = ZoneInfo("America/New_York")
PROFILE = lucid_flex_25k()
POLICY = GovernorPolicy()
GOV = LucidGovernor(PROFILE, POLICY)
MLL = PROFILE.max_loss_limit
HARD_MARGIN = POLICY.hard_stop_buffer_fraction * MLL


def dec(rng: random.Random, low: int, high: int) -> D:
    """A random cent amount in [low, high] dollars."""
    return D(rng.randint(low * 100, high * 100)) / D(100)


def ny_ns(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=NY).astimezone(UTC).timestamp()) * NS


def edge(state: EdgeState) -> EdgeEvidence:
    return EdgeEvidence(state, 150, D("0.2"), D("0.1"), D("0.3"), D("2"))


def random_request(rng: random.Random) -> EntryRequest:
    return EntryRequest(
        stop_ticks=D(rng.randint(4, 200)), target_ticks=D(rng.randint(4, 600)), tick_value=D("0.50"),
        commission_per_contract=D(rng.randint(0, 300)) / D(100), slippage_ticks_round_trip=D(rng.randint(0, 6)),
        contract_cap=rng.randint(1, 25))


def risk_of(request: EntryRequest) -> D:
    cost = request.commission_per_contract + request.slippage_ticks_round_trip * request.tick_value
    return request.stop_ticks * request.tick_value + cost


def random_snapshot(rng: random.Random, now_ns: int, state: EdgeState | None = None) -> AccountSnapshot:
    """A self-consistent account: balance = day start + day realised; highest close >= day start."""
    highest = dec(rng, 25000, 27500)
    day_start = highest - dec(rng, 0, 1100)
    day_start = max(day_start, D("23800"))
    realized = dec(rng, -400, 600)
    return AccountSnapshot(
        now_ns=now_ns, trading_day="2026-11-09", balance=day_start + realized, open_pnl=dec(rng, -400, 600),
        highest_closing_balance=highest, day_start_balance=day_start, day_realized=realized,
        day_entries=rng.randint(0, 8), losing_streak=rng.randint(0, 3),
        last_entry_ts_ns=None, last_loss_ts_ns=None, day_locked_reason="",
        micro_profit_share=D("0"), edge=edge(state or rng.choice(list(EdgeState))),
        best_day_share=None)


# --- 1. never sizes above the per-trade cap ------------------------------------------------------


def test_invariant_never_sizes_above_the_per_trade_cap_or_the_day_budget_left() -> None:
    rng = random.Random(20261108)
    approved = 0
    for _ in range(20000):
        snap = random_snapshot(rng, ny_ns(2026, 11, 9, 11))
        req = random_request(rng)
        result = GOV.check_entry(snap, req)
        cap = GOV.day_budget(snap).per_trade_risk_cap
        assert cap <= POLICY.max_trade_risk_fraction_of_mll * MLL            # the absolute $40
        if result.allowed:
            approved += 1
            spent = result.contracts * risk_of(req)
            assert spent <= cap, (snap, req, result.contracts, spent, cap)
            remaining = max(D(0), result.budget.loss_budget + min(snap.day_realized + snap.open_pnl, D(0)))
            assert spent <= remaining
            assert 1 <= result.contracts <= min(req.contract_cap, PROFILE.max_micro_contracts)
    assert approved > 500          # the property is not vacuous


# --- 2. never opens when equity minus the floor is under the hard-stop margin -----------------


def test_invariant_never_opens_inside_the_hard_stop_margin() -> None:
    rng = random.Random(7)
    approved = 0
    for _ in range(30000):
        snap = random_snapshot(rng, ny_ns(2026, 11, 9, 11))
        result = GOV.check_entry(snap, random_request(rng))
        if result.allowed:
            approved += 1
            equity = snap.balance + snap.open_pnl
            assert equity - result.budget.floor > HARD_MARGIN, (snap, result.budget)
            # and a worst-case stop-out of the approved size still stays above the margin
    assert approved > 500


# --- 3. geometric survival over 1,000 random worst-case days -------------------------------------


def worst_case_day(book: GovernorBook, rng: random.Random, day: str, gap: D, clock: list[int]) -> None:
    """Take every entry the governor allows and lose each at its full stop (+ an optional gap)."""
    book.roll_day(day)
    for attempt in range(40):
        now = ny_ns(2026, 1, 5, 10) + attempt * 400 * NS                 # 10:00 .. 14:27, inside the window
        snap = book.snapshot(now)
        req = random_request(rng)
        result = GOV.check_entry(snap, req)
        if not result.allowed:
            continue
        book.record_entry(now)
        loss = result.contracts * risk_of(req) + gap
        book.record_trade(ClosedTrade(now, now + 60 * NS, -loss, D("-1")))
        floor_now = GOV.floor(book.highest_closing_balance)
        assert book.balance > floor_now, ("breached", book.balance, floor_now)


def test_invariant_geometric_survival_over_1000_random_worst_case_days_without_gaps() -> None:
    rng = random.Random(99)
    days = accounts = 0
    while days < 1000:
        accounts += 1
        book = GovernorBook(PROFILE, POLICY)
        clock = [0]
        for number in range(1, 400):
            day = f"{2026 + number // 365}-{(number % 365) // 30 + 1:02d}-{(number % 30) + 1:02d}-{accounts}"
            book.roll_day(day)
            snap = book.snapshot(ny_ns(2026, 1, 5, 10))
            if GOV.status(snap) is not AccountStatus.ACTIVE:
                break                       # retired: it stopped itself
            worst_case_day(book, rng, day, D(0), clock)
            days += 1
            if days >= 1000:
                break
    assert accounts > 20            # many accounts, each stopped itself well before the floor


def test_invariant_a_gap_up_to_the_hard_stop_margin_on_every_trade_still_cannot_reach_the_floor() -> None:
    rng = random.Random(5)
    days = 0
    while days < 1000:
        book = GovernorBook(PROFILE, POLICY)
        clock = [0]
        for number in range(1, 400):
            day = f"d{days}-{number}"
            book.roll_day(day)
            if GOV.status(book.snapshot(ny_ns(2026, 1, 5, 10))) is not AccountStatus.ACTIVE:
                break
            worst_case_day(book, rng, day, HARD_MARGIN / D(4), clock)   # $25 beyond each stop
            days += 1
            if days >= 1000:
                break


def test_boundary_of_geometric_survival_a_big_enough_gap_does_reach_the_floor() -> None:
    """Honest limit: the claim is conditional on gaps. A $225 slip on the last allowed trade ends a
    just-active account (buffer $300): 25% day budget leaves $225 and the hard margin does not help."""
    book = GovernorBook(PROFILE, POLICY)
    book.balance = book.day_start_balance = book.highest_closing_balance = D("24300")
    book.roll_day("x")
    now = ny_ns(2026, 11, 9, 10)
    snap = book.snapshot(now)
    assert GOV.status(snap) is AccountStatus.ACTIVE
    book.record_trade(ClosedTrade(now, now + NS, -(GOV.day_budget(snap).loss_budget + D("225")), D("-1")))
    assert GOV.status(book.snapshot(now + 10 * NS)) is AccountStatus.FAILED


# --- 4. no entry outside the entry window or after the flat time ---------------------------------


def test_invariant_no_entry_outside_the_window_across_a_whole_year_incl_dst() -> None:
    rng = random.Random(3)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    snap0 = GovernorBook(PROFILE, POLICY).snapshot(0)
    req = EntryRequest(D(20), D(40), D("0.50"), D("1.24"), D(2), 20)
    inside = outside = 0
    for _ in range(40000):
        moment = base + timedelta(seconds=rng.randint(0, 365 * 86400))
        snap = replace(snap0, now_ns=int(moment.timestamp()) * NS, day_realized=D(0), open_pnl=D(0),
                       balance=snap0.day_start_balance, last_entry_ts_ns=None, last_loss_ts_ns=None,
                       day_entries=0, losing_streak=0, micro_profit_share=D(0))
        local = moment.astimezone(NY).time().replace(microsecond=0)
        result = GOV.check_entry(snap, req)
        in_window = POLICY.entry_window_start <= local < POLICY.entry_window_end
        if result.allowed:
            assert in_window and local < POLICY.flat_time, (moment, local)
            inside += 1
        elif not in_window:
            outside += 1
    assert inside > 1000 and outside > 1000


def test_check_position_flattens_from_the_flat_time_and_never_holds_overnight() -> None:
    """Fix pack: outside the holding hours (before the entry window, or from the flat time) it flattens."""
    book = GovernorBook(PROFILE, POLICY)
    book.roll_day("2026-11-09")
    actions = {}
    for label, (h, mi, d) in {"16:39": (16, 39, 9), "16:40": (16, 40, 9), "23:59": (23, 59, 9),
                              "02:00 next day": (2, 0, 10), "09:34 next day": (9, 34, 10)}.items():
        actions[label] = GOV.check_position(book.snapshot(ny_ns(2026, 11, d, h, mi))).action
    assert actions["16:39"] is PositionAction.HOLD
    assert actions["16:40"] is PositionAction.FLATTEN and actions["23:59"] is PositionAction.FLATTEN
    assert actions["02:00 next day"] is PositionAction.FLATTEN and actions["09:34 next day"] is PositionAction.FLATTEN


# --- 5. the daily profit cap ---------------------------------------------------------------------


def test_invariant_at_or_over_the_profit_cap_no_entry_and_open_positions_are_flattened() -> None:
    rng = random.Random(11)
    cap = GOV.day_budget(random_snapshot(rng, 0)).profit_cap
    assert cap == D("375.00")
    checked = 0
    for _ in range(5000):
        snap = random_snapshot(rng, ny_ns(2026, 11, 9, 11))
        snap = replace(snap, day_realized=cap + dec(rng, 0, 300), open_pnl=D(0),
                       balance=snap.day_start_balance + cap + dec(rng, 0, 300))
        day_pnl = snap.day_realized + snap.open_pnl
        if day_pnl < GOV.day_budget(snap).profit_cap:
            continue
        checked += 1
        assert not GOV.check_entry(snap, random_request(rng)).allowed
        verdict = GOV.check_position(snap)
        assert verdict.action in (PositionAction.FLATTEN_AND_LOCK_DAY,)
    assert checked > 1000


def test_the_cap_is_a_gate_not_a_ceiling_the_last_winner_can_overshoot_it() -> None:
    """Claim check ('best day of a $1,250 pass is at most 30%'): an entry is allowed at +$370, so a
    1-contract 114-tick winner (+$57 gross) makes a $427 day, 34% of a $1,250 pass, not 30%."""
    book = GovernorBook(PROFILE, POLICY)
    gov = LucidGovernor(PROFILE, book.policy)
    book.roll_day("2026-11-09")
    now = ny_ns(2026, 11, 9, 10)
    book.record_trade(ClosedTrade(now - 120 * NS, now - 60 * NS, D("370"), D("2")))
    request = EntryRequest(D(57), D(114), D("0.50"), D("1.24"), D(2), 20)
    assert gov.check_entry(book.snapshot(now), request).allowed
    book.record_trade(ClosedTrade(now, now + 60 * NS, D("57"), D("2")))
    assert book.day_realized == D("427") and D("427") / D("1250") > D("0.30")


# --- 6. one contract without proof of edge -----------------------------------------------------------


def test_invariant_one_contract_unless_the_edge_is_proven_even_for_tiny_risk() -> None:
    rng = random.Random(13)
    seen_more_than_one = 0
    for _ in range(20000):
        state = rng.choice(list(EdgeState))
        snap = random_snapshot(rng, ny_ns(2026, 11, 9, 11), state)
        req = random_request(rng)
        result = GOV.check_entry(snap, req)
        if result.allowed and state is not EdgeState.PROVEN:
            assert result.contracts == POLICY.contracts_without_proof == 1, (state, result.contracts)
        if result.allowed and result.contracts > 1:
            seen_more_than_one += 1
            assert state is EdgeState.PROVEN and result.contracts <= POLICY.contracts_when_proven
    assert seen_more_than_one >= 50        # proven-tier path exercised


def test_invariant_a_real_book_never_gets_more_than_one_contract_before_100_closed_trades() -> None:
    rng = random.Random(17)
    gov = LucidGovernor(PROFILE, replace(POLICY, max_losing_streak=6))   # trades are 700 s apart
    tight = EntryRequest(D(8), D(40), D("0.50"), D("1.24"), D(2), 20)        # a stop tight enough for 3+ by risk
    for _ in range(40):
        book = GovernorBook(PROFILE, gov.policy)
        book.roll_day("2026-11-09")
        now = ny_ns(2026, 11, 9, 10)
        for index in range(99):
            r = D(rng.choice([2, -1])) if rng.random() < 0.99 else D(5)       # a sample that looks great
            book.record_trade(ClosedTrade(now + index * 700 * NS, now + index * 700 * NS + 90 * NS,
                                          r * D(20) if r > 0 else r * D(20), r))
            book.day_realized = D(0)                                         # keep the day budget out of the way
            book.balance = PROFILE.account_size
            snap = book.snapshot(now + (index + 1) * 700 * NS)
            result = gov.check_entry(snap, tight)
            if result.allowed:
                assert result.contracts == 1

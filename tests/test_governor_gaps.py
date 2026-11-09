"""Governor rules the existing tests did not pin (red-team mutation testing, 2026-11-08).

Written by the red-team against copies of the code: each test killed at least one surviving
mutant (the G/O/A ids in the comments name the mutation that went unnoticed before). Updated
for the fix pack where it deliberately changed behaviour (net-profit consistency, minimum
trading days, a bad settings file refusing entries, the tighter policy bounds)."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, time
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.paper.options import read_governor_policy
from app.risk.account_profile import DrawdownMethod, lucid_flex_25k
from app.risk.lucid_governor import (
    NEW_YORK,
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
    consistency_share,
    load_governor_policy,
)

NS = 1_000_000_000
PROFILE = lucid_flex_25k()
DAY = "2026-11-09"


def ts(hour: int, minute: int = 0, second: int = 0, day: int = 9) -> int:
    return int(datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC).timestamp()) * NS


def request(stop: int = 57, target: int = 114, cap: int = 20, commission: str = "1.24", slip: int = 2) -> EntryRequest:
    return EntryRequest(D(stop), D(target), D("0.50"), D(commission), D(slip), cap)


def fresh(policy: GovernorPolicy | None = None, profile=PROFILE):
    governor = LucidGovernor(profile, policy)
    book = GovernorBook(profile, governor.policy)
    book.roll_day(DAY)
    return governor, book


def rs(values: list[str], pnl: list[str] | None = None, start: int = ts(10)) -> list[ClosedTrade]:
    out = []
    for index, value in enumerate(values):
        net = D(pnl[index]) if pnl else D(value) * D(30)
        out.append(ClosedTrade(start + index * 700 * NS, start + index * 700 * NS + 60 * NS, net, D(value)))
    return out


def noted(names: str):
    """Marker only: documents which mutants a test is meant to kill."""
    return pytest.mark.kills(names)


# --- policy validation (G04 G05 G08 G09 G10 G11 G12 G14 G17 G18) ----------------------------------


@pytest.mark.parametrize("overrides", [
    {"max_entries_per_day": 0}, {"max_losing_streak": 0},                       # G04 G05
    {"entry_window_start": time(10, 0), "entry_window_end": time(10, 0)},       # G09
    {"min_net_reward_risk": D("-0.1")},
    {"contracts_without_proof": 0},                                             # G11
    {"hard_stop_buffer_fraction": D("0.6")}, {"retire_buffer_fraction": D("0.95")},   # G17 G18
])
def test_gap_policy_validation_rejects(overrides: dict) -> None:
    with pytest.raises(ValueError):
        GovernorPolicy(**overrides)


def test_gap_policy_validation_accepts_the_inclusive_edges() -> None:
    GovernorPolicy(entry_window_end=time(16, 40), flat_time=time(16, 40))       # G08
    GovernorPolicy(min_net_reward_risk=D("0.5"))                                # G10 (the floor is inclusive)
    GovernorPolicy(contracts_without_proof=1, contracts_when_proven=1)          # G12
    GovernorPolicy(edge_min_trades=100, edge_window_trades=100)                 # G14


# --- evidence (G22 G23 G24 G25 G26 G28 G29 G30) -----------------------------------------------------


def test_gap_edge_window_is_the_most_recent_trades() -> None:                  # G22
    old_bad = rs(["-1"] * 100)
    recent = rs(["0.9", "-0.3"] * 100, start=ts(10) + 10**5 * NS)
    assert assess_edge(old_bad + recent, GovernorPolicy()).state is EdgeState.PROVEN


def test_gap_sample_variance_uses_n_minus_one() -> None:                        # G23
    # +1.1649R / -0.8351R alternating: lower bound is just below 0 with n-1, just above with n
    sample = rs(["1.1649", "-0.8351"] * 50)
    evidence = assess_edge(sample, GovernorPolicy())
    assert evidence.lower_bound_r < 0 and evidence.state is EdgeState.INCONCLUSIVE


def test_gap_z_score_is_one_sided_95() -> None:                                 # G24
    evidence = assess_edge(rs(["1.1", "-0.9"] * 75), GovernorPolicy())          # mean 0.1R, PF 1.22
    assert evidence.profit_factor >= D("1.2") and evidence.state is EdgeState.INCONCLUSIVE


def test_gap_flat_trades_prove_nothing() -> None:                               # G25 G28
    assert assess_edge(rs(["0"] * 100, pnl=["0"] * 100), GovernorPolicy()).state is EdgeState.INCONCLUSIVE


def test_gap_a_positive_lower_bound_still_needs_a_real_profit_factor() -> None:  # G26
    pnl = ["10", "-9.5"] * 75                       # profit factor 1.05
    evidence = assess_edge(rs(["0.4", "0.6"] * 75, pnl=pnl), GovernorPolicy())
    assert evidence.lower_bound_r > 0 and evidence.profit_factor < D("1.2")
    assert evidence.state is EdgeState.INCONCLUSIVE


def test_gap_evidence_threshold_is_inclusive_and_not_lower() -> None:           # G29 G30
    good = lambda n: rs(["0.9", "-0.3"] * (n // 2))        # noqa: E731
    assert assess_edge(good(100), GovernorPolicy()).state is EdgeState.PROVEN
    assert assess_edge(good(98), GovernorPolicy()).state is EdgeState.NO_EVIDENCE
    assert assess_edge(good(60), GovernorPolicy()).state is EdgeState.NO_EVIDENCE


def test_gap_consistency_share_divides_by_the_net_account_profit() -> None:  # G35 (fix pack: net, as Lucid)
    assert consistency_share({"a": D("300"), "b": D("-100"), "c": D("100")}) == D("1")    # 300 / 300 net


def test_gap_cushion_equal_to_the_profile_share_is_refused() -> None:           # G36
    with pytest.raises(ValueError):
        LucidGovernor(replace(PROFILE, consistency_max_day_share=D("0.30")), GovernorPolicy(consistency_cushion=D("0.30")))


# --- day budget at small buffers and with odd cents (G38 G39 G41 G42 G43 G48 G49 G50) --------------


def budget(day_start: str, profile=PROFILE):
    governor, book = fresh(profile=profile)
    book.balance = book.day_start_balance = D(day_start)
    return governor.day_budget(book.snapshot(ts(10)))


def test_gap_budget_below_the_floor_has_zero_buffer_and_budget() -> None:       # G38
    assert budget("23900").start_buffer == D("0") and budget("23900").loss_budget == D("0")


def test_gap_small_buffer_budget_respects_the_hard_stop_margin() -> None:       # G39 G41 G42 G43
    small = budget("24120")                                    # $120 above the floor
    assert small.loss_budget == D("20.00")                     # min(25% = 30, 120 - 100)
    assert small.per_trade_risk_cap == D("10.00")              # budget / 2
    assert budget("24050").loss_budget == D("0.00")            # inside the margin: nothing may be risked


def test_gap_budgets_round_down_never_up() -> None:                             # G48 G49 G50
    odd = budget("24140.07")
    assert odd.loss_budget == D("35.01") and odd.per_trade_risk_cap == D("17.50")
    third = replace(PROFILE, consistency_max_day_share=D("0.3337"))
    assert budget("25000", third).profit_cap == D("292.12")    # (0.3337 - 0.10) x 1250 = 292.125


# --- status and entry rules (G52 G56 G58 G63 G71 G72 G73 G74) ---------------------------------------


def test_gap_status_counts_open_losses_toward_failure() -> None:               # G52
    governor, book = fresh()
    book.balance = D("24500")
    assert governor.status(book.snapshot(ts(11), D("-600"))) is AccountStatus.FAILED


def test_gap_exactly_thirty_percent_left_is_still_active() -> None:             # G56
    governor, book = fresh()
    book.balance = book.day_start_balance = D("24300")
    assert governor.status(book.snapshot(ts(11))) is AccountStatus.ACTIVE


def test_gap_retired_and_passed_accounts_cannot_enter() -> None:               # G58
    governor, book = fresh()
    book.balance = book.day_start_balance = D("24250")
    retired = governor.check_entry(book.snapshot(ts(10, 30)), request())
    assert not retired.allowed and retired.reason_code == "governor_account_not_active"
    governor, book = fresh()
    book.balance = D("26250")
    book.traded_days = {"2026-11-05", "2026-11-06", "2026-11-09"}   # Lucid needs 3 trading days
    assert governor.status(book.snapshot(ts(10, 30))) is AccountStatus.PASSED
    assert not governor.check_entry(book.snapshot(ts(10, 30)), request()).allowed


def test_gap_entry_blocked_when_profit_exactly_equals_the_cap() -> None:       # G63
    governor, book = fresh()
    book.record_trade(ClosedTrade(ts(9, 40), ts(9, 41), D("375.00"), D("5")))
    assert "governor_day_profit_cap" in {c.name for c in governor.check_entry(book.snapshot(ts(11)), request()).failures}


def test_gap_spacing_and_cooldown_use_their_own_clock_and_seconds() -> None:    # G71 G72 G73
    governor, book = fresh()
    book.record_entry(ts(10, 0) - 200 * NS)                              # 200 s since the last entry: spacing (120) ok
    assert governor.check_entry(book.snapshot(ts(10, 0)), request()).allowed        # G71
    governor, book = fresh()
    book.record_entry(ts(10, 0) - 1000 * NS)
    book.record_trade(ClosedTrade(ts(10, 0) - 500 * NS, ts(10, 0) - 250 * NS, D("-20"), D("-1")))   # loss 250 s ago
    result = governor.check_entry(book.snapshot(ts(10, 0)), request())
    assert "governor_loss_cooldown" in {c.name for c in result.failures}            # G72
    governor, book = fresh()
    book.record_entry(ts(10, 0) - 120 * NS)
    assert governor.check_entry(book.snapshot(ts(10, 0)), request()).allowed        # exactly 120 s: allowed (G73)
    governor, book = fresh()
    book.record_entry(ts(10, 0) - 119 * NS)
    assert not governor.check_entry(book.snapshot(ts(10, 0)), request()).allowed


def test_gap_microscalp_share_exactly_at_the_limit_blocks() -> None:            # G74
    governor, book = fresh()
    book.record_trade(ClosedTrade(ts(9, 40), ts(9, 40, 3), D("30"), D("1")))
    book.record_trade(ClosedTrade(ts(9, 50), ts(9, 51, 30), D("70"), D("1")))
    assert book.snapshot(ts(11)).micro_profit_share == D("0.3")
    assert "governor_microscalp_guard" in {c.name for c in governor.check_entry(book.snapshot(ts(11)), request()).failures}


# --- trade quality arithmetic (G75 G76 G77 G78 G79 G86) ---------------------------------------------


def test_gap_trade_quality_uses_costs_on_both_sides_in_dollars() -> None:       # G75 G76 G77
    governor, book = fresh()
    snap = book.snapshot(ts(10, 30))
    assert governor.check_entry(snap, request(stop=20, target=29)).allowed          # reward 12.26 / risk 12.24
    blocked = governor.check_entry(snap, request(stop=20, target=28))               # 11.76 / 12.24
    assert not blocked.allowed and blocked.reason_code == "governor_trade_quality"


def test_gap_trade_quality_boundary_and_zero_risk() -> None:                    # G78 G79 G86
    governor, book = fresh()
    snap = book.snapshot(ts(10, 30))
    free = request(stop=20, target=20, commission="0", slip=0)
    assert governor.check_entry(snap, free).allowed                                  # ratio exactly 1.0 passes
    zero = EntryRequest(D(0), D(10), D("0.50"), D(0), D(0), 20)
    assert not governor.check_entry(snap, zero).allowed                              # no crash on 0/0 risk
    lax, book2 = fresh(GovernorPolicy(min_net_reward_risk=D("0.5")))         # the loosest allowed
    assert not lax.check_entry(book2.snapshot(ts(10, 30)), zero).allowed             # zero-risk request is never sized


def test_gap_contracts_never_exceed_the_profile_micro_cap() -> None:            # G89
    # The policy tops out at 5 contracts now, so a profile with a 2-micro cap shows the clamp.
    small = replace(PROFILE, max_micro_contracts=2)
    governor = LucidGovernor(small, GovernorPolicy(contracts_without_proof=5, contracts_when_proven=5))
    book = GovernorBook(small, governor.policy)
    book.roll_day("2026-11-09")
    snap = replace(book.snapshot(ts(10, 30)), edge=EdgeEvidence(EdgeState.PROVEN, 150, D(1), D(1), D(1), None))
    tiny = EntryRequest(D(1), D(10), D("0.50"), D(0), D(0), 99)
    assert governor.check_entry(snap, tiny).contracts == small.max_micro_contracts


# --- open position (G95 G96 G99) ----------------------------------------------------------------------


def test_gap_position_rules_at_their_exact_boundaries() -> None:
    governor, book = fresh()
    book.balance = book.day_start_balance = D("24100")
    assert governor.check_position(book.snapshot(ts(11))).action is PositionAction.FLATTEN_AND_LOCK_DAY   # G95 ($100 exactly)
    book.balance = book.day_start_balance = D("24110")
    assert governor.check_position(book.snapshot(ts(11))).action is PositionAction.HOLD                   # G96 ($110 > $100)
    governor, book = fresh()
    assert governor.check_position(book.snapshot(ts(11), D("375"))).action is PositionAction.FLATTEN_AND_LOCK_DAY   # G99


# --- GovernorBook state (G116 G117 G118 G119 G123 G124 G125 G126 G127 G129 G131 G132) -------------


def test_gap_a_new_day_resets_every_per_day_counter() -> None:                  # G116 G117 G118 G119
    _, book = fresh()
    book.record_entry(ts(10))
    book.record_trade(ClosedTrade(ts(10), ts(10, 1), D("-5"), D("-1")))
    book.lock_day("reason")
    assert book.day_entries == 1 and book.losing_streak == 1 and book.last_entry_ts_ns and book.last_loss_ts_ns
    book.roll_day("2026-11-10")
    assert (book.day_entries, book.losing_streak, book.last_entry_ts_ns, book.last_loss_ts_ns, book.day_locked_reason) == (0, 0, None, None, "")


def test_gap_day_realised_accumulates() -> None:                                # G123
    _, book = fresh()
    book.record_trade(ClosedTrade(ts(10), ts(10, 1), D("10"), D("1")))
    book.record_trade(ClosedTrade(ts(10, 5), ts(10, 6), D("-4"), D("-1")))
    assert book.day_realized == D("6")


def test_gap_edge_evidence_refreshes_after_new_trades() -> None:                # G124
    _, book = fresh()
    assert book.snapshot(ts(10)).edge.state is EdgeState.NO_EVIDENCE
    for trade in rs(["0.9", "-0.3"] * 60):
        book.record_trade(trade)
    assert book.snapshot(ts(23)).edge.state is EdgeState.PROVEN


def test_gap_scratch_trade_is_booked_as_a_loss_with_close_time_and_micro_hold_inclusive() -> None:   # G125 G126 G127
    _, book = fresh()
    book.record_trade(ClosedTrade(ts(10), ts(10, 10), D("0"), D("0")))
    assert book.losing_streak == 1 and book.last_loss_ts_ns == ts(10, 10)
    book.record_trade(ClosedTrade(ts(11), ts(11, 0, 5), D("50"), D("1")))       # held exactly 5 s
    assert book.micro_profit == D("50")


def test_gap_first_lock_reason_is_kept_and_today_counts_for_consistency() -> None:   # G129 G131
    _, book = fresh()
    book.lock_day("first")
    book.lock_day("second")
    assert book.day_locked_reason == "first"
    book.record_trade(ClosedTrade(ts(10), ts(10, 1), D("100"), D("1")))
    assert book.snapshot(ts(11)).best_day_share == D("1")


def test_gap_trade_history_keeps_a_thousand() -> None:                          # G132
    _, book = fresh()
    for index in range(1001):
        book.record_trade(ClosedTrade(index, index + 1, D("1"), D("1")))
    assert len(book.trades) == 1000


# --- configuration (G135 O01 O11 O18 O22) --------------------------------------------------------------


def test_gap_an_empty_governor_file_means_the_defaults(tmp_path: Path) -> None:   # G135
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_governor_policy(empty) == GovernorPolicy()


def test_gap_unparseable_yaml_refuses_entries_keeps_the_strict_defaults_and_logs_an_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:                                                                       # O01 O18
    bad = tmp_path / "bad.yaml"
    bad.write_text("day_loss_fraction: [0.2\n", encoding="utf-8")                 # YAML syntax error (not a ValueError)
    with caplog.at_level(logging.WARNING):
        policy = read_governor_policy(bad)
    # Fix pack: a bad file is an error (entries refused), never a silent default.
    assert policy is not None and policy.config_error
    assert replace(policy, config_error="") == GovernorPolicy()
    assert caplog.records and all(r.levelno == logging.ERROR for r in caplog.records)   # O11


def test_gap_the_warning_names_the_offending_setting(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:   # O22
    bad = tmp_path / "bad.yaml"
    bad.write_text("day_loss_fractoin: 0.2\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        read_governor_policy(bad)
    assert "day_loss_fractoin" in caplog.text


# --- account profile (A02 A03 A04 A05 A17 A20 A22 A25 A26 A27) -----------------------------------------


def test_gap_static_profile_floor_is_account_minus_limit() -> None:             # A02
    static = replace(PROFILE, drawdown_method=DrawdownMethod.STATIC)
    assert static.trailing_threshold(D("27000")) == D("24000")


@pytest.mark.parametrize("initial,locked", [(None, D("25100")), (D("26100"), None), (None, None)])
def test_gap_missing_trail_numbers_fall_back_to_the_starting_floor(initial, locked) -> None:   # A03 A04 A05
    odd = replace(PROFILE, initial_trail_balance=initial, locked_mll_balance=locked)
    assert odd.trailing_threshold(D("27000")) == D("24000")


def test_gap_target_progress_is_rounded_to_four_places() -> None:               # A17
    assert PROFILE.target_progress(D("25333")) == D("0.2664")


def test_gap_consistency_breached_edge_cases() -> None:                         # A20 A22
    assert replace(PROFILE, consistency_max_day_share=None).consistency_breached({"a": D("900")}) is False
    assert PROFILE.consistency_breached({"a": D("40"), "b": D("30"), "c": D("30")}) is False   # 40/100 = exactly 40%
    # Fix pack: a losing day lowers the account profit, so the same best day becomes 40/90 = 44%.
    assert PROFILE.consistency_breached({"a": D("40"), "b": D("30"), "c": D("30"), "d": D("-10")}) is True


@pytest.mark.parametrize("overrides", [
    {"consistency_max_day_share": D("0")}, {"consistency_max_day_share": D("2")}, {"account_size": D("0")},
])
def test_gap_profile_validation(overrides: dict) -> None:                       # A25 A26 A27
    with pytest.raises(ValueError):
        replace(PROFILE, **overrides)

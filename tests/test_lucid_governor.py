"""The Lucid-fitted governor: every rule on its own, and the properties the rules exist to give."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, time
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.risk.account_profile import lucid_flex_25k
from app.risk.lucid_governor import (
    NEW_YORK,
    AccountStatus,
    ClosedTrade,
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
    """New York wall-clock time on a fixed November day (EST), as nanoseconds."""
    moment = datetime(2026, 11, day, hour, minute, second, tzinfo=NEW_YORK).astimezone(UTC)
    return int(moment.timestamp()) * NS


def trade(net: str, *, closed_at: int, held: int = 60, r: str | None = None) -> ClosedTrade:
    value = D(net)
    return ClosedTrade(closed_at - held * NS, closed_at, value, D(r) if r is not None else value / D("30"))


def request(stop: int = 57, target: int = 114, cap: int = 20) -> EntryRequest:
    return EntryRequest(D(stop), D(target), D("0.50"), D("1.24"), D(2), cap)


def fresh(policy: GovernorPolicy | None = None) -> tuple[LucidGovernor, GovernorBook]:
    governor = LucidGovernor(PROFILE, policy)
    book = GovernorBook(PROFILE, governor.policy)
    book.roll_day(DAY)
    return governor, book


# --- the policy refuses to be weakened ---------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"day_loss_fraction": D("0.9")},
        {"day_loss_fraction": D("0")},
        {"max_trade_risk_fraction_of_mll": D("0.5")},
        {"consistency_cushion": D("0")},                    # no cushion: a pass of exactly the target could breach
        {"consistency_cushion": D("0.5")},                  # a cushion that swallows the whole cap
        {"micro_profit_share_limit": D("0.6")},             # Lucid prohibits more than 50%
        {"trades_per_day_budget": 0},
        {"entry_window_end": time(17, 0)},                  # after the flat time
        {"contracts_without_proof": 4, "contracts_when_proven": 3},
        {"edge_min_trades": 10},
    ],
)
def test_a_policy_that_would_weaken_the_guarantees_is_rejected(overrides: dict) -> None:
    with pytest.raises(ValueError):
        GovernorPolicy(**overrides)


# --- money: floor, budgets, the geometric survival property -----------------------------------


def test_the_day_budget_is_a_fraction_of_the_buffer_above_the_end_of_day_floor() -> None:
    governor, book = fresh()
    budget = governor.day_budget(book.snapshot(ts(10)))
    assert budget.floor == D("24000") and budget.start_buffer == D("1000")
    assert budget.loss_budget == D("250.00")           # 25% of the buffer
    assert budget.per_trade_risk_cap == D("40.00")     # min(250/2, 4% of the $1,000 limit)
    # (40% single-day share - 10-point cushion) x $1,250 target: the best day of a pass stays <= 30%
    assert budget.profit_cap == D("375.00")


def test_after_a_losing_day_every_budget_shrinks_with_the_buffer() -> None:
    governor, book = fresh()
    book.record_trade(trade("-250", closed_at=ts(11)))
    book.roll_day("2026-11-10")
    budget = governor.day_budget(book.snapshot(ts(10, day=10)))
    assert budget.start_buffer == D("750.00")
    assert budget.loss_budget == D("187.50") and budget.per_trade_risk_cap == D("40.00")   # the 4% cap still binds


def test_a_typical_stop_stays_affordable_all_the_way_down_to_the_retire_threshold() -> None:
    """No frozen zone: just above 30% of the limit, one contract of a 57-tick stop still fits."""
    governor, book = fresh()
    book.balance = book.day_start_balance = D("24310")                 # $310 above the floor
    snapshot = book.snapshot(ts(10, 30))
    assert governor.status(snapshot) is AccountStatus.ACTIVE
    budget = governor.day_budget(snapshot)
    assert budget.per_trade_risk_cap >= D("30.74")                     # $30.74 = one 57-tick contract with costs
    assert governor.check_entry(snapshot, request()).allowed


def test_a_run_of_maximum_loss_days_can_never_reach_the_floor() -> None:
    """The point of the geometric day budget: ruin needs a gap beyond a stop, not a bad streak."""
    governor, book = fresh()
    retired_on = None
    for number in range(1, 40):
        day = f"2026-12-{number:02d}"
        book.roll_day(day)
        snapshot = book.snapshot(ts(10, day=9))
        if governor.status(snapshot) is AccountStatus.RETIRED:
            retired_on = number
            break
        loss = governor.day_budget(snapshot).loss_budget
        book.record_trade(trade(str(-loss), closed_at=ts(11)))
        assert book.balance > governor.floor(book.highest_closing_balance) + D("100")
    assert retired_on is not None and 4 <= retired_on <= 6   # it stops itself long before the floor


def test_the_floor_trails_the_highest_close_then_locks() -> None:
    governor, book = fresh()
    book.record_trade(trade("500", closed_at=ts(11)))
    book.roll_day("2026-11-10")
    assert governor.floor(book.highest_closing_balance) == D("24500")        # trails: 25,500 - 1,000
    book.record_trade(trade("600", closed_at=ts(11, day=10)))
    book.record_trade(trade("100", closed_at=ts(12, day=10)))
    book.roll_day("2026-11-11")                                             # closed at 26,200
    assert governor.floor(book.highest_closing_balance) == D("25100")       # locked
    book.record_trade(trade("-300", closed_at=ts(11, day=11)))
    book.roll_day("2026-11-12")
    assert governor.floor(book.highest_closing_balance) == D("25100")       # never moves back or up


def test_the_floor_only_moves_at_the_end_of_the_day_not_with_intraday_profit() -> None:
    governor, book = fresh()
    book.record_trade(trade("400", closed_at=ts(11)))
    assert governor.floor(book.snapshot(ts(12)).highest_closing_balance) == D("24000")


# --- the entry rules, one by one ---------------------------------------------------------------


def decision(governor: LucidGovernor, book: GovernorBook, now: int, req: EntryRequest | None = None, open_pnl: str = "0"):
    return governor.check_entry(book.snapshot(now, D(open_pnl)), req or request())


def test_a_normal_setup_inside_every_limit_is_approved_with_one_contract() -> None:
    governor, book = fresh()
    result = decision(governor, book, ts(10, 30))
    assert result.allowed and result.contracts == 1 and result.reason_code == "governor_approved"
    assert not result.failures and len(result.checks) >= 10


def test_every_failing_rule_is_listed_not_only_the_first() -> None:
    governor, book = fresh()
    book.lock_day("test lock")
    result = decision(governor, book, ts(20), request(target=12))
    assert not result.allowed and result.contracts == 0
    assert {c.name for c in result.failures} >= {
        "governor_day_locked", "governor_outside_entry_window", "governor_trade_quality",
    }
    assert result.reason_code == result.failures[0].name


@pytest.mark.parametrize(("hour", "minute", "allowed"), [(9, 30, False), (9, 35, True), (16, 14, True), (16, 15, False), (3, 0, False)])
def test_entries_are_only_taken_inside_the_window(hour: int, minute: int, allowed: bool) -> None:
    governor, book = fresh()
    assert decision(governor, book, ts(hour, minute)).allowed is allowed


def test_the_daily_entry_cap_blocks_the_next_entry() -> None:
    governor, book = fresh(GovernorPolicy(max_entries_per_day=2, min_seconds_between_entries=0))
    book.record_entry(ts(10, 0))
    book.record_entry(ts(10, 10))
    result = decision(governor, book, ts(10, 40))
    assert not result.allowed and "governor_daily_entries" in {c.name for c in result.failures}


def test_a_losing_streak_stops_the_day() -> None:
    governor, book = fresh(GovernorPolicy(cooldown_after_loss_seconds=0))
    for minute in (0, 10, 20):
        book.record_trade(trade("-20", closed_at=ts(10, minute)))
    result = decision(governor, book, ts(11, 0))
    assert "governor_losing_streak" in {c.name for c in result.failures}


def test_a_win_resets_the_streak() -> None:
    _, book = fresh()
    book.record_trade(trade("-20", closed_at=ts(10, 0)))
    book.record_trade(trade("-20", closed_at=ts(10, 10)))
    book.record_trade(trade("30", closed_at=ts(10, 20)))
    assert book.losing_streak == 0


def test_there_is_a_cooldown_after_a_loss_and_a_gap_between_entries() -> None:
    governor, book = fresh()
    book.record_entry(ts(10, 0))
    book.record_trade(trade("-20", closed_at=ts(10, 1)))
    soon = decision(governor, book, ts(10, 1, 30))
    assert {"governor_loss_cooldown", "governor_entry_spacing"} <= {c.name for c in soon.failures}
    later = decision(governor, book, ts(10, 30))
    assert later.allowed


def test_the_day_loss_budget_locks_new_entries() -> None:
    governor, book = fresh(GovernorPolicy(cooldown_after_loss_seconds=0, max_losing_streak=9))
    book.record_trade(trade("-250", closed_at=ts(10, 0)))
    result = decision(governor, book, ts(11, 0))
    assert "governor_day_loss_budget" in {c.name for c in result.failures}


def test_open_losses_count_against_the_day_budget_before_they_are_realised() -> None:
    governor, book = fresh()
    result = decision(governor, book, ts(11, 0), open_pnl="-250")
    assert "governor_day_loss_budget" in {c.name for c in result.failures}


def test_the_profit_cap_protects_the_consistency_rule() -> None:
    governor, book = fresh(GovernorPolicy(cooldown_after_loss_seconds=0))
    book.record_trade(trade("500", closed_at=ts(10, 0)))
    result = decision(governor, book, ts(11, 0))
    assert "governor_day_profit_cap" in {c.name for c in result.failures}


def test_the_budget_left_today_limits_size_so_the_day_loss_cannot_be_exceeded() -> None:
    governor, book = fresh(GovernorPolicy(cooldown_after_loss_seconds=0, max_losing_streak=9))
    book.record_trade(trade("-230", closed_at=ts(10, 0)))      # $20 of the $250 budget remains
    result = decision(governor, book, ts(11, 0))
    assert not result.allowed and "governor_risk_budget" in {c.name for c in result.failures}


def test_a_stop_too_wide_for_the_per_trade_cap_is_rejected_not_traded_smaller() -> None:
    governor, book = fresh()
    result = decision(governor, book, ts(10, 30), request(stop=90, target=200))
    assert not result.allowed and result.reason_code == "governor_risk_budget"
    assert "per-trade cap $40.00" in result.reason


def test_a_tighter_stop_buys_more_contracts_but_never_more_than_the_tier_cap() -> None:
    governor, book = fresh()
    one = decision(governor, book, ts(10, 30), request(stop=14, target=40))
    assert one.allowed and one.contracts == 1          # unproven edge: one contract, however tight
    proven = replace(book.snapshot(ts(10, 30)), edge=assess_edge(winners(120), governor.policy))
    sized = governor.check_entry(proven, request(stop=14, target=40))
    assert sized.contracts == 3                        # $40 / $9.24 = 4 by risk, capped at the proven tier


def test_the_governor_never_exceeds_the_engines_own_contract_cap() -> None:
    governor, book = fresh()
    proven = replace(book.snapshot(ts(10, 30)), edge=assess_edge(winners(120), governor.policy))
    assert governor.check_entry(proven, request(stop=14, target=40, cap=2)).contracts == 2


def test_costs_decide_trade_quality_not_results() -> None:
    governor, book = fresh()
    cheap = decision(governor, book, ts(10, 30), request(stop=57, target=12))
    assert not cheap.allowed and cheap.reason_code == "governor_trade_quality"
    assert decision(governor, book, ts(10, 30), request(stop=20, target=40)).allowed


def test_microscalping_share_blocks_entries_before_it_reaches_lucids_limit() -> None:
    governor, book = fresh(GovernorPolicy(cooldown_after_loss_seconds=0, min_seconds_between_entries=0))
    book.record_trade(trade("40", closed_at=ts(10, 0), held=3))
    book.record_trade(trade("40", closed_at=ts(10, 5), held=90))
    assert book.snapshot(ts(11)).micro_profit_share == D("0.5")
    result = decision(governor, book, ts(11, 0))
    assert "governor_microscalp_guard" in {c.name for c in result.failures}


# --- evidence: size only scales when the bot's own trades prove an edge ------------------------


def winners(count: int, mean: str = "0.30", spread: str = "0.2") -> list[ClosedTrade]:
    base, step = D(mean), D(spread)
    out = []
    for index in range(count):
        r = base + (step if index % 2 else -step)
        out.append(trade(str(r * D("30")), closed_at=ts(10, 0) + index * 600 * NS, r=str(r)))
    return out


def test_too_few_trades_is_no_evidence_whatever_they_earned() -> None:
    evidence = assess_edge(winners(40), GovernorPolicy())
    assert evidence.state is EdgeState.NO_EVIDENCE and evidence.live_blocked


def test_a_clearly_positive_sample_is_proven_and_only_then_unblocks_size() -> None:
    evidence = assess_edge(winners(150, spread="0.6"), GovernorPolicy())   # +0.9R winners, -0.3R losers
    assert evidence.state is EdgeState.PROVEN and not evidence.live_blocked
    assert evidence.lower_bound_r > 0 and evidence.profit_factor and evidence.profit_factor >= D("1.2")


def test_a_clearly_negative_sample_is_refuted() -> None:
    evidence = assess_edge(winners(150, mean="-0.30"), GovernorPolicy())
    assert evidence.state is EdgeState.REFUTED and evidence.live_blocked


def test_a_noisy_sample_around_zero_is_inconclusive() -> None:
    evidence = assess_edge(winners(150, mean="0.01", spread="1.5"), GovernorPolicy())
    assert evidence.state is EdgeState.INCONCLUSIVE


def test_a_refuted_strategy_is_held_to_one_contract_not_stopped_so_evidence_keeps_flowing() -> None:
    governor, book = fresh()
    snapshot = replace(book.snapshot(ts(10, 30)), edge=assess_edge(winners(150, mean="-0.30"), governor.policy))
    result = governor.check_entry(snapshot, request(stop=14, target=40))
    assert result.allowed and result.contracts == 1


# --- while a position is open -----------------------------------------------------------------


def test_the_position_is_flattened_close_to_the_floor() -> None:
    governor, book = fresh()
    book.balance = D("24150")
    book.day_start_balance = D("24150")
    verdict = governor.check_position(book.snapshot(ts(11), D("-60")))   # equity 24,090: $90 above the floor
    assert verdict.action is PositionAction.FLATTEN_AND_LOCK_DAY and verdict.reason_code == "governor_floor_proximity"


def test_the_position_is_flattened_when_the_day_loss_budget_is_used() -> None:
    governor, book = fresh()
    verdict = governor.check_position(book.snapshot(ts(11), D("-250")))
    assert verdict.action is PositionAction.FLATTEN_AND_LOCK_DAY and verdict.reason_code == "governor_day_loss_budget"


def test_the_position_is_flattened_at_the_profit_cap() -> None:
    governor, book = fresh()
    verdict = governor.check_position(book.snapshot(ts(11), D("500")))
    assert verdict.action is PositionAction.FLATTEN_AND_LOCK_DAY and verdict.reason_code == "governor_day_profit_cap"


def test_the_position_is_flat_before_lucids_auto_close() -> None:
    governor, book = fresh()
    assert governor.check_position(book.snapshot(ts(16, 39))).action is PositionAction.HOLD
    verdict = governor.check_position(book.snapshot(ts(16, 40)))
    assert verdict.action is PositionAction.FLATTEN and verdict.reason_code == "governor_flat_time"
    assert time(16, 40) < PROFILE.session_close.replace(tzinfo=None)


def test_inside_every_limit_the_position_is_held() -> None:
    governor, book = fresh()
    assert governor.check_position(book.snapshot(ts(11), D("-20"))).action is PositionAction.HOLD


# --- the account's status and the consistency rule ------------------------------------------------


def test_an_account_at_the_floor_has_failed() -> None:
    governor, book = fresh()
    book.balance = D("24000")
    assert governor.status(book.snapshot(ts(11))) is AccountStatus.FAILED


def test_reaching_the_target_passes_only_if_one_day_did_not_supply_more_than_40_percent() -> None:
    governor, book = fresh()
    book.balance = D("26250")
    for share, expected in (("0.60", AccountStatus.ACTIVE), ("0.45", AccountStatus.ACTIVE),
                            ("0.40", AccountStatus.PASSED), ("0.30", AccountStatus.PASSED)):
        snapshot = replace(book.snapshot(ts(11)), best_day_share=D(share))
        assert governor.status(snapshot) is expected, share


def test_the_profit_cap_alone_makes_a_pass_consistent() -> None:
    """Three capped days of $375 and one of $125 reach the $1,250 target with a best day of 30%."""
    assert consistency_share({"a": D("375"), "b": D("375"), "c": D("375"), "d": D("125")}) == D("0.3")
    assert D("0.3") <= PROFILE.consistency_max_day_share
    assert consistency_share({"a": D("-100")}) is None


def test_the_book_banks_the_close_and_the_consistency_ratio_when_the_day_rolls() -> None:
    _, book = fresh()
    book.record_trade(trade("300", closed_at=ts(11)))
    book.roll_day("2026-11-10")
    assert book.closing_balances[DAY] == D("25300") and book.day_pnls[DAY] == D("300")
    assert book.highest_closing_balance == D("25300") and book.day_realized == D("0")
    assert book.day_entries == 0 and not book.day_locked_reason


# --- configuration --------------------------------------------------------------------------------


def test_a_missing_config_file_means_the_safe_defaults(tmp_path: Path) -> None:
    assert load_governor_policy(tmp_path / "absent.yaml") == GovernorPolicy()


def test_the_config_file_sets_typed_values(tmp_path: Path) -> None:
    path = tmp_path / "gov.yaml"
    path.write_text("day_loss_fraction: 0.2\nmax_entries_per_day: 5\nflat_time: '16:30'\nentry_window_end: '16:00'\nenabled: true\n", encoding="utf-8")
    policy = load_governor_policy(path)
    assert policy.day_loss_fraction == D("0.2") and policy.max_entries_per_day == 5
    assert policy.flat_time == time(16, 30) and policy.entry_window_end == time(16, 0)


def test_a_typo_in_the_config_is_an_error_not_a_silent_default(tmp_path: Path) -> None:
    path = tmp_path / "gov.yaml"
    path.write_text("day_loss_fractoin: 0.2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown governor setting"):
        load_governor_policy(path)


def test_a_config_that_weakens_a_guarantee_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "gov.yaml"
    path.write_text("consistency_cushion: 0.9\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_governor_policy(path)


def test_the_shipped_config_loads_and_is_the_default_policy() -> None:
    assert load_governor_policy(Path("config/risk_governor.yaml")).enabled is True


def test_the_governor_imports_no_network_broker_or_paper_engine_code() -> None:
    source = Path("app/risk/lucid_governor.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.MULTILINE))
    assert not {name.split(".")[0] for name in imports} & {"socket", "requests", "urllib", "http"}
    assert not any(name.startswith(("app.execution", "app.paper")) for name in imports)


# --- the cap follows the profile's own consistency rule -------------------------------------------


def test_the_profit_cap_follows_the_fifty_percent_checkout_add_on() -> None:
    fifty = replace(PROFILE, consistency_max_day_share=D("0.50"))
    budget = LucidGovernor(fifty).day_budget(GovernorBook(fifty, GovernorPolicy()).snapshot(ts(10)))
    assert budget.profit_cap == D("500.00")            # (50% - 10 points) x $1,250


def test_a_profile_without_a_consistency_rule_has_no_profit_cap() -> None:
    none = replace(PROFILE, consistency_max_day_share=None)
    governor, book = LucidGovernor(none), GovernorBook(none, GovernorPolicy())
    book.roll_day(DAY)
    assert governor.day_budget(book.snapshot(ts(10))).profit_cap is None
    book.record_trade(trade("900", closed_at=ts(10, 0)))                  # a huge day is simply allowed
    assert governor.check_entry(book.snapshot(ts(11)), request()).allowed
    assert governor.check_position(book.snapshot(ts(11), D("900"))).action is PositionAction.HOLD


def test_a_cushion_as_large_as_the_profiles_share_is_refused() -> None:
    tight = replace(PROFILE, consistency_max_day_share=D("0.25"))
    with pytest.raises(ValueError, match="smaller than the profile"):
        LucidGovernor(tight, GovernorPolicy(consistency_cushion=D("0.30")))

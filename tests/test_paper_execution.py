"""Tests for causal simulated paper execution (orders, fills, positions, P&L)."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.paper.execution import (
    REASON_APPROVED,
    REASON_CONTRACT_UNRESOLVED,
    REASON_COOLDOWN,
    REASON_DUPLICATE,
    REASON_POSITION_OPEN,
    REASON_POOR_REWARD_RISK,
    REASON_RISK_ZERO_SIZE,
    REASON_STALE_BOOK,
    REASON_STOP_TOO_WIDE,
    ExecutionConfig,
    MarketTick,
    PaperExecutor,
    align_to_tick,
    size_intent,
)
from app.paper.models import (
    CloseReason,
    Direction,
    OrderStatus,
    PaperOrderIntent,
    RiskDecision,
    SetupProvenance,
    points_to_dollars,
)

SEC = 1_000_000_000


def _prov(setup_id: str = "s1", index: int = 10) -> SetupProvenance:
    return SetupProvenance(
        session_id="session_20260717T140000Z", setup_id=setup_id,
        strategy_version="order-flow-plan-v1", contract="MNQU6",
        decision_event_index=index, decision_ts_ns=index * SEC,
    )


def _long(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    # 10-point stop, 20-point target (2R), as the strategy defines.
    return PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29490.00"), target=Decimal("29520.00"),
                            provenance=_prov(setup_id, index))


def _short(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    return PaperOrderIntent(direction=Direction.SHORT, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29510.00"), target=Decimal("29480.00"),
                            provenance=_prov(setup_id, index))


def _tick(index: int, price: str, bid: str | None = None, ask: str | None = None) -> MarketTick:
    return MarketTick(event_index=index, ts_ns=index * SEC, price=Decimal(price),
                      best_bid=Decimal(bid) if bid else None,
                      best_ask=Decimal(ask) if ask else None)


def _executor(**kwargs) -> PaperExecutor:
    return PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                         config=ExecutionConfig(), **kwargs)


def _approve(contracts: int = 1) -> RiskDecision:
    return RiskDecision(approved=True, contracts=contracts, reason_code=REASON_APPROVED, reason="ok")


# --- intent validity ---------------------------------------------------------


def test_intent_requires_a_deterministic_coherent_stop_and_target() -> None:
    """A stop/target is never invented: an incoherent intent cannot exist."""
    with pytest.raises(ValueError, match="long requires stop < entry < target"):
        PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500"),
                         stop=Decimal("29520"), target=Decimal("29490"), provenance=_prov())
    with pytest.raises(ValueError, match="short requires target < entry < stop"):
        PaperOrderIntent(direction=Direction.SHORT, entry_reference=Decimal("29500"),
                         stop=Decimal("29480"), target=Decimal("29520"), provenance=_prov())
    with pytest.raises(ValueError):
        PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("0"),
                         stop=Decimal("1"), target=Decimal("2"), provenance=_prov())


def test_trade_lineage_can_never_be_blank() -> None:
    with pytest.raises(ValueError, match="lineage"):
        SetupProvenance(session_id="", setup_id="s", strategy_version="v",
                        contract="MNQU6", decision_event_index=1, decision_ts_ns=1)


# --- causality ---------------------------------------------------------------


def test_order_cannot_fill_from_the_same_event_that_generated_it() -> None:
    """The signal event may never also be the fill event."""
    ex = _executor()
    order = ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="2026-07-17")
    assert order.status is OrderStatus.PENDING
    # Replaying the SAME event index must not fill it.
    ex.on_tick(_tick(10, "29500.00"))
    assert ex.position is None
    assert ex.pending is not None


def test_a_later_event_fills_the_order_causally() -> None:
    ex = _executor()
    ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="2026-07-17")
    ex.on_tick(_tick(11, "29500.00"))
    assert ex.pending is None
    assert ex.position is not None
    assert ex.position.opened_event_index == 11
    # Entry slippage is adverse for a long: filled ABOVE the reference.
    assert ex.position.entry_price == Decimal("29500.25")


def test_position_never_resolves_from_its_own_entry_event() -> None:
    """Stop/target cannot trigger on the same event that opened the position."""
    ex = _executor()
    ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="2026-07-17")
    ex.on_tick(_tick(11, "29400.00"))  # fills AND is far below the stop
    assert ex.position is not None, "the entry event must not also close the trade"
    assert ex.trades == []


# --- P&L correctness ---------------------------------------------------------


def test_long_target_pnl_uses_mnq_economics() -> None:
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="2026-07-17")
    ex.on_tick(_tick(11, "29500.00"))          # entry 29500.25
    trade = ex.on_tick(_tick(12, "29520.00"))  # target
    assert trade is not None
    assert trade.close_reason is CloseReason.TARGET
    assert trade.exit_price == Decimal("29520.00")
    # 19.75 points * $2/point = $39.50 gross, minus $1.24 commission.
    assert trade.gross_pnl == Decimal("39.50")
    assert trade.commission == Decimal("1.24")
    assert trade.net_pnl == Decimal("38.26")
    assert trade.won is True
    assert ex.balance == Decimal("25038.26")


def test_short_stop_pnl_is_signed_correctly() -> None:
    ex = _executor()
    ex.submit(_short(), _approve(1), _tick(10, "29500.00"), trading_day="2026-07-17")
    ex.on_tick(_tick(11, "29500.00"))          # short entry 29499.75 (adverse)
    trade = ex.on_tick(_tick(12, "29510.00"))  # stop hit
    assert trade is not None and trade.close_reason is CloseReason.STOP
    assert trade.net_pnl < 0, "a short stopped out above entry must lose money"
    assert ex.balance < Decimal("25000")


def test_points_to_dollars_uses_decimal_mnq_economics() -> None:
    assert points_to_dollars(Decimal("1"), 1) == Decimal("2.00")     # 4 ticks * $0.50
    assert points_to_dollars(Decimal("10"), 2) == Decimal("40.00")
    assert points_to_dollars(Decimal("-5"), 1) == Decimal("-10.00")
    assert isinstance(points_to_dollars(Decimal("1"), 1), Decimal)


def test_contracts_scale_pnl() -> None:
    one = _executor()
    one.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    one.on_tick(_tick(11, "29500.00"))
    single = one.on_tick(_tick(12, "29520.00"))

    many = _executor()
    many.submit(_long(), _approve(3), _tick(10, "29500.00"), trading_day="d")
    many.on_tick(_tick(11, "29500.00"))
    triple = many.on_tick(_tick(12, "29520.00"))
    assert triple.gross_pnl == single.gross_pnl * 3
    assert triple.commission == single.commission * 3


# --- conservative ambiguity --------------------------------------------------


def test_same_event_stop_and_target_is_ambiguous_and_never_a_win() -> None:
    """If both are reachable in one event we cannot know which printed first.

    The honest resolution is to refuse the win: book it at the stop and label the
    close AMBIGUOUS, so an unknowable tape can never flatter the results.
    """
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    # Force a genuinely ambiguous event: one price satisfying BOTH barriers.
    ex.position.stop = Decimal("29520.00")
    ex.position.target = Decimal("29520.00")
    trade = ex.on_tick(_tick(12, "29520.00"))
    assert trade is not None
    assert trade.close_reason is CloseReason.AMBIGUOUS
    # Booked at the stop side (with adverse slippage), never at the target.
    assert trade.exit_price <= Decimal("29520.00")


def test_gap_through_the_stop_fills_at_the_gapped_price_not_the_stop() -> None:
    """A stop-market cannot fill at a price that never traded."""
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    trade = ex.on_tick(_tick(12, "29450.00"))  # gapped far below the 29490 stop
    assert trade.close_reason is CloseReason.STOP
    assert trade.exit_price == Decimal("29449.75"), "fill must be at the gap, plus slippage"
    assert trade.exit_price < Decimal("29490.00")


def test_time_stop_closes_a_stalled_position() -> None:
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                       config=ExecutionConfig(time_stop_ns=5 * SEC))
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    trade = ex.on_tick(_tick(30, "29505.00"))  # long after the time stop
    assert trade is not None and trade.close_reason is CloseReason.TIME_STOP


def test_mae_and_mfe_are_tracked() -> None:
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    ex.on_tick(_tick(12, "29495.00"))  # adverse
    ex.on_tick(_tick(13, "29515.00"))  # favourable
    trade = ex.on_tick(_tick(14, "29520.00"))
    assert trade.mae_points < 0 and trade.mfe_points > 0


# --- constraints -------------------------------------------------------------


def test_one_position_maximum_is_enforced() -> None:
    ex = _executor()
    ex.submit(_long("s1"), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    blocked = ex.gate(_long("s2", 12), _tick(12, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == REASON_POSITION_OPEN


def test_duplicate_setup_occurrence_cannot_trade_twice() -> None:
    ex = _executor()
    ex.submit(_long("dup"), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    ex.on_tick(_tick(12, "29520.00"))  # close it so only duplication blocks
    blocked = ex.gate(_long("dup", 20), _tick(400, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == REASON_DUPLICATE


def test_cooldown_prevents_immediate_re_entry() -> None:
    ex = _executor()
    ex.submit(_long("a"), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    ex.on_tick(_tick(12, "29520.00"))
    blocked = ex.gate(_long("b", 13), _tick(13, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == REASON_COOLDOWN


def test_daily_entry_and_loss_locks_stop_new_orders() -> None:
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                       config=ExecutionConfig(max_entries_per_day=1, cooldown_ns=0))
    ex.submit(_long("a"), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    ex.on_tick(_tick(12, "29520.00"))
    blocked = ex.gate(_long("b", 900), _tick(900, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == "daily_entry_lock"


def test_lockout_blocks_entries() -> None:
    ex = _executor()
    ex.lock_out()
    blocked = ex.gate(_long(), _tick(10, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == "drawdown_lock"


def test_crossed_book_and_unresolved_contract_block_entry() -> None:
    ex = _executor()
    crossed = ex.gate(_long(), _tick(10, "29500.00", bid="29501.00", ask="29499.00"), trading_day="d")
    assert crossed is not None and crossed.reason_code == REASON_STALE_BOOK

    unresolved = PaperOrderIntent(
        direction=Direction.LONG, entry_reference=Decimal("29500"), stop=Decimal("29490"),
        target=Decimal("29520"),
        provenance=SetupProvenance(session_id="s", setup_id="x", strategy_version="v",
                                   contract="unknown", decision_event_index=1, decision_ts_ns=1),
    )
    ex2 = _executor()
    blocked = ex2.gate(unresolved, _tick(10, "29500.00"), trading_day="d")
    assert blocked is not None and blocked.reason_code == REASON_CONTRACT_UNRESOLVED


def test_risk_rejected_candidate_creates_no_order() -> None:
    ex = _executor()
    rejection = RiskDecision.reject(REASON_RISK_ZERO_SIZE, "risk sizing permits zero contracts")
    order = ex.submit(_long(), rejection, _tick(10, "29500.00"), trading_day="d")
    assert order.status is OrderStatus.REJECTED_BY_RISK
    assert ex.pending is None and ex.position is None
    assert ex.rejections and ex.rejections[-1].reason_code == REASON_RISK_ZERO_SIZE
    ex.on_tick(_tick(11, "29500.00"))
    assert ex.position is None, "a risk-rejected candidate must never fill"


def test_risk_sizing_uses_the_project_engine_and_caps_at_the_profile_limit() -> None:
    decision = size_intent(_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"))
    assert decision.approved is True
    assert 0 < decision.contracts <= 20
    capped = size_intent(_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                         max_contracts=1, commission_per_contract=Decimal("1.24"))
    assert capped.contracts == 1  # the account cap always binds


def test_zero_size_is_rejected_not_rounded_up() -> None:
    decision = size_intent(_long(), balance=Decimal("1"), drawdown_room=Decimal("1"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"))
    assert decision.approved is False
    assert decision.reason_code == REASON_RISK_ZERO_SIZE


def _narrow_stop_long(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    # 8-point stop (32 ticks): risk_per_contract = 32*0.50 + 1.24 + 0.50 = 17.74,
    # so 4 contracts = $70.96 - under the $80 fixed-size cap.
    return PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29492.00"), target=Decimal("29516.00"),
                            provenance=_prov(setup_id, index))


def test_fixed_size_approves_exactly_four_contracts_when_stop_is_narrow_enough() -> None:
    # Narrow enough natural stop: never invented or resized, just measured against
    # the fixed-size cap.
    decision = size_intent(_narrow_stop_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"),
                           fixed_contracts=4, max_risk_per_trade_usd=Decimal("80"))
    assert decision.approved is True
    assert decision.contracts == 4
    assert decision.reason_code == REASON_APPROVED


@pytest.mark.parametrize("invalid_cap", [Decimal("Infinity"), Decimal("NaN")])
def test_execution_config_rejects_non_finite_fixed_risk_caps(invalid_cap: Decimal) -> None:
    with pytest.raises(ValueError):
        ExecutionConfig(fixed_contracts=4, max_risk_per_trade_usd=invalid_cap)


def test_fixed_size_sizes_down_to_fit_the_cap_instead_of_rejecting() -> None:
    # _long() has a 10-point (40-tick) stop: risk_per_contract = 40*0.50 + 1.24 + 0.50
    # = 21.74, so the full ceiling of 4 = $86.96 exceeds the $80 cap. Rather than
    # reject, the position is sized DOWN to 3 contracts ($65.22 < $80) - the real
    # stop is untouched and the cap is never relaxed.
    decision = size_intent(_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"),
                           fixed_contracts=4, max_risk_per_trade_usd=Decimal("80"))
    assert decision.approved is True
    assert decision.contracts == 3
    assert decision.reason_code == REASON_APPROVED


def _wide_stop_long(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    # 40-point stop (160 ticks): risk_per_contract = 160*0.50 + 1.24 + 0.50 = 81.74,
    # so even ONE contract exceeds the $80 cap - genuinely too wide to trade.
    return PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29460.00"), target=Decimal("29580.00"),
                            provenance=_prov(setup_id, index))


def test_fixed_size_rejects_only_when_a_single_contract_exceeds_the_cap() -> None:
    # A single contract already risks $81.74 > $80: the stop is genuinely too
    # wide, so the trade is rejected outright (never resized onto a fake stop).
    decision = size_intent(_wide_stop_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"),
                           fixed_contracts=4, max_risk_per_trade_usd=Decimal("80"))
    assert decision.approved is False
    assert decision.reason_code == REASON_STOP_TOO_WIDE
    assert decision.contracts == 0


def test_fixed_size_disabled_by_default_leaves_dynamic_sizing_untouched() -> None:
    # fixed_contracts defaults to 0 - the exact same call as the pre-existing
    # dynamic-sizing regression test above, confirming the new parameters are
    # fully backward compatible when omitted.
    decision = size_intent(_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"))
    assert decision.approved is True
    assert 0 < decision.contracts <= 20
    assert decision.reason_code == REASON_APPROVED


def test_fixed_size_still_respects_the_account_contract_ceiling() -> None:
    # Even when fixed_contracts asks for more than the account allows, the hard
    # max_contracts ceiling still wins.
    decision = size_intent(_narrow_stop_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=2, commission_per_contract=Decimal("1.24"),
                           fixed_contracts=4, max_risk_per_trade_usd=Decimal("80"))
    assert decision.approved is True
    assert decision.contracts == 2


def _poor_reward_risk_long(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    # 10-point stop (40 ticks) but only a 1-point target (4 ticks): reward:risk 0.1,
    # exactly the tiny-target geometry that costs erase.
    return PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29490.00"), target=Decimal("29501.00"),
                            provenance=_prov(setup_id, index))


def test_min_reward_risk_skips_a_tiny_target_setup() -> None:
    decision = size_intent(_poor_reward_risk_long(), balance=Decimal("25000"),
                           drawdown_room=Decimal("1000"), max_contracts=20,
                           commission_per_contract=Decimal("1.24"), min_reward_risk=Decimal("1.5"))
    assert decision.approved is False
    assert decision.reason_code == REASON_POOR_REWARD_RISK
    assert decision.contracts == 0


def test_min_reward_risk_allows_a_setup_that_meets_the_bar() -> None:
    # _long() is a 10-pt stop / 20-pt target = reward:risk 2.0, above the 1.5 bar.
    decision = size_intent(_long(), balance=Decimal("25000"), drawdown_room=Decimal("1000"),
                           max_contracts=20, commission_per_contract=Decimal("1.24"),
                           min_reward_risk=Decimal("1.5"))
    assert decision.approved is True
    assert decision.reason_code == REASON_APPROVED


def test_min_reward_risk_disabled_by_default_takes_any_setup() -> None:
    # min_reward_risk omitted (0): the poor 0.1 setup is NOT filtered - unchanged.
    decision = size_intent(_poor_reward_risk_long(), balance=Decimal("25000"),
                           drawdown_room=Decimal("1000"), max_contracts=20,
                           commission_per_contract=Decimal("1.24"))
    assert decision.approved is True


def test_widen_target_aims_further_but_never_closer() -> None:
    from app.paper.streaming_engine import _widen_target

    # LONG: entry 100, stop 98 (risk 2 pts), tiny DOL target 101 -> 2R pushes to 104.
    assert _widen_target(Direction.LONG, Decimal("100"), Decimal("98"),
                         Decimal("101"), Decimal("2")) == Decimal("104")
    # SHORT: entry 100, stop 102 (risk 2), DOL target 99 -> 2R pushes to 96.
    assert _widen_target(Direction.SHORT, Decimal("100"), Decimal("102"),
                         Decimal("99"), Decimal("2")) == Decimal("96")
    # Never closer than the strategy's own target: a far DOL wins over a small multiple.
    assert _widen_target(Direction.LONG, Decimal("100"), Decimal("98"),
                         Decimal("120"), Decimal("2")) == Decimal("120")
    # Disabled (0) leaves the strategy target unchanged.
    assert _widen_target(Direction.LONG, Decimal("100"), Decimal("98"),
                         Decimal("101"), Decimal("0")) == Decimal("101")


# --- liquidation & isolation ---------------------------------------------------


def test_session_close_liquidates_open_state() -> None:
    ex = _executor()
    ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    trade = ex.liquidate(_tick(12, "29505.00"))
    assert trade is not None and trade.close_reason is CloseReason.SESSION_CLOSE
    assert ex.position is None


def test_synthetic_fixture_trades_are_flagged_and_separable() -> None:
    """Test trades must never be mistakable for canonical results."""
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                       is_synthetic_fixture=True)
    ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    trade = ex.on_tick(_tick(12, "29520.00"))
    assert trade.is_synthetic_fixture is True
    assert trade.to_record()["is_synthetic_fixture"] is True

    canonical = _executor()
    canonical.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="d")
    canonical.on_tick(_tick(11, "29500.00"))
    assert canonical.on_tick(_tick(12, "29520.00")).is_synthetic_fixture is False


def test_trade_record_is_fully_traceable_and_money_is_never_float() -> None:
    ex = _executor()
    ex.submit(_long(), _approve(), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00"))
    record = ex.on_tick(_tick(12, "29520.00")).to_record()
    for key in ("session_id", "setup_id", "strategy_version", "contract",
                "opened_event_index", "closed_event_index", "close_reason", "net_pnl"):
        assert key in record
    for money in ("entry_price", "exit_price", "gross_pnl", "commission", "net_pnl", "balance_after"):
        assert isinstance(record[money], str), f"{money} must serialize as a string, not a float"


def test_no_paper_execution_module_can_reach_a_broker() -> None:
    """Structural proof: delayed paper cannot route an order anywhere."""
    banned = ("execution.orders", "execution.brackets", "execution.gateway",
              "live_execution", "tradovate", "broker", "user_sync", "order_lifecycle")
    for module in ("app/paper/execution.py", "app/paper/models.py", "app/paper/streaming_engine.py"):
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        assert not any(any(b in n.lower() for b in banned) for n in names), f"{module}: {names}"


# --- fills must be prices that could actually have traded ------------------------


def test_align_to_tick_rounds_adversely_onto_the_mnq_grid() -> None:
    """MNQ trades in 0.25 only; rounding must never invent a better price."""
    assert align_to_tick(Decimal("29500.875"), round_up=True) == Decimal("29501.00")
    assert align_to_tick(Decimal("29500.875"), round_up=False) == Decimal("29500.75")
    # An already-valid tick must survive untouched in both directions.
    assert align_to_tick(Decimal("29500.25"), round_up=True) == Decimal("29500.25")
    assert align_to_tick(Decimal("29500.25"), round_up=False) == Decimal("29500.25")


def test_long_entry_lifts_the_offer_not_the_mid() -> None:
    """A market buy transacts at the ask; the mid is often not even tradeable."""
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    # mid would be 29500.625 (an impossible price); the offer is 29501.00.
    ex.on_tick(_tick(11, "29500.625", bid="29500.25", ask="29501.00"))
    # ask 29501.00 + 1 tick adverse slippage = 29501.25
    assert ex.position.entry_price == Decimal("29501.25")


def test_short_entry_hits_the_bid_not_the_mid() -> None:
    """A market sell transacts at the bid, with slippage against us."""
    ex = _executor()
    intent = PaperOrderIntent(
        direction=Direction.SHORT, entry_reference=Decimal("29500.00"),
        stop=Decimal("29510.00"), target=Decimal("29480.00"), provenance=_prov("short1"),
    )
    ex.submit(intent, _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.625", bid="29500.25", ask="29501.00"))
    # bid 29500.25 - 1 tick adverse slippage = 29500.00
    assert ex.position.entry_price == Decimal("29500.00")


def test_every_simulated_price_lands_on_the_quarter_point_grid() -> None:
    """No fill, stop, or exit may ever be a price MNQ cannot trade."""
    ex = _executor()
    ex.submit(_long(), _approve(2), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.625", bid="29500.125", ask="29500.875"))
    trade = ex.on_tick(_tick(12, "29489.37", bid="29489.31", ask="29489.44"))
    assert trade is not None
    for label, price in (("entry", trade.entry_price), ("exit", trade.exit_price)):
        assert price % Decimal("0.25") == 0, f"{label} {price} is not a tradeable MNQ price"


def test_market_exit_sells_the_bid_on_a_time_stop() -> None:
    """A time-stop exit is a market order and must not book the mid."""
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                       config=ExecutionConfig(time_stop_ns=5 * SEC))
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(_tick(11, "29500.00", bid="29499.75", ask="29500.00"))
    trade = ex.on_tick(MarketTick(event_index=12, ts_ns=11 * SEC + 6 * SEC,
                                  price=Decimal("29500.625"),
                                  best_bid=Decimal("29500.25"), best_ask=Decimal("29501.00")))
    assert trade.close_reason is CloseReason.TIME_STOP
    assert trade.exit_price == Decimal("29500.25"), "a long exits into the bid"


def test_fill_falls_back_to_the_trade_price_when_the_book_is_absent() -> None:
    """A trade-only tick still has authoritative price evidence."""
    ex = _executor()
    ex.submit(_long(), _approve(1), _tick(10, "29500.00"), trading_day="d")
    ex.on_tick(MarketTick(event_index=11, ts_ns=11 * SEC, price=Decimal("29500.00")))
    assert ex.position.entry_price == Decimal("29500.25")  # traded price + slippage

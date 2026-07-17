"""Tests for the fixed, quality-gated real Bookmap paper ledger."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.research.paper_ledger import run_real_paper_ledger
from app.research.real_episodes import CompletedRealOutcome


def test_fixed_account_uses_actual_net_pnl_and_never_resets() -> None:
    """The account is the SELECTED profile (Lucid Flex 25K), not a fabricated $100k."""
    from app.risk.account_profile import lucid_flex_25k

    start = lucid_flex_25k().account_size
    win = _outcome("a", 1, pnl=Decimal("18.00"), r=Decimal("0.8"))
    loss = _outcome("b", 10, pnl=Decimal("-22.00"), r=Decimal("-1"))
    result = run_real_paper_ledger((win, loss))
    first, second = result.trades
    assert result.starting_balance == start == Decimal("25000")
    assert first.balance_after == start + Decimal(first.contracts) * Decimal("18")
    assert second.balance_after == first.balance_after + Decimal(second.contracts) * Decimal("-22")


def test_position_size_never_exceeds_the_profile_contract_cap() -> None:
    """Lucid Flex 25K permits 20 micros; sizing may never exceed the account's cap."""
    from app.risk.account_profile import lucid_flex_25k

    cap = lucid_flex_25k().max_micro_contracts
    result = run_real_paper_ledger((_outcome("a", 1, pnl=Decimal("18.00"), r=Decimal("0.8")),))
    assert all(t.contracts <= cap for t in result.trades)


def test_daily_entry_limit_and_one_position_limit_are_enforced() -> None:
    outcomes = (
        _outcome("a", 1, exit_offset=100),
        _outcome("overlap", 2, exit_offset=3),
        _outcome("b", 110),
        _outcome("c", 120),
        _outcome("daily-fourth", 130),
    )
    result = run_real_paper_ledger(outcomes)
    assert len(result.trades) == 3
    reasons = [skipped.reason for skipped in result.skipped]
    assert any("one-position maximum" in reason for reason in reasons)
    assert any("daily entry lock" in reason for reason in reasons)


def test_non_real_provenance_cannot_enter_ledger() -> None:
    synthetic = replace(_outcome("synthetic", 1), provenance="SYNTHETIC")
    result = run_real_paper_ledger((synthetic,))
    assert result.trades == ()


def _outcome(
    setup_id: str,
    entry_offset: int,
    *,
    exit_offset: int = 5,
    pnl: Decimal = Decimal("18.00"),
    r: Decimal = Decimal("0.8"),
) -> CompletedRealOutcome:
    base = 1_752_537_751_000_000_000
    return CompletedRealOutcome(
        session_id="session_real",
        setup_id=setup_id,
        provenance="REAL_DELAYED",
        direction="long",
        trading_day="2026-07-15",
        decision_ts_ns=base + entry_offset - 1,
        entry_ts_ns=base + entry_offset,
        exit_ts_ns=base + entry_offset + exit_offset,
        defended_price=Decimal("100"),
        entry_reference_price=Decimal("100.25"),
        entry=Decimal("100.50"),
        stop=Decimal("90"),
        target=Decimal("102"),
        exit_reference_price=Decimal("102"),
        exit=Decimal("101.75"),
        commission=Decimal("1.24"),
        slippage_cost=Decimal("1.00"),
        gross_pnl_per_contract=Decimal("3.50"),
        net_pnl_per_contract=pnl,
        risk_per_contract=Decimal("22.24"),
        r_multiple=r,
        outcome="target_first" if pnl > 0 else "stop_first",
        strategy_version="order-flow-plan-v1",
        builder_version="real-episodes-v2",
        source_event_range=(1, 10),
        decision_hash="d" * 64,
        input_hash="i" * 64,
        ordering_mode="receive_sequence",
    )

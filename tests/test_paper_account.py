"""Tests for the paper-trading account simulator and consistency scoring."""

from __future__ import annotations

from decimal import Decimal

from app.discovery.consistency import score_run
from app.discovery.metrics import TradeResult
from app.discovery.paper_account import (
    DEFAULT_STARTING_BALANCE,
    PaperAccountConfig,
    run_paper_accounts,
)


def _trade(r: str, day: str = "2026-07-13") -> TradeResult:
    return TradeResult(r_multiple=Decimal(r), session="new_york_open", regime_tag="trending/high_vol", trade_date=day)


def test_stop_is_forty_ticks_from_mentor_ten_points() -> None:
    """The mentor's 10-point stop is 40 MNQ ticks."""
    config = PaperAccountConfig()
    assert config.stop_distance_ticks == Decimal("40")
    assert config.risk_per_contract() > 0


def test_winning_sequence_grows_one_100k_account() -> None:
    """A positive-expectancy sequence stays on one profitable account."""
    trades = tuple(_trade("2") for _ in range(30))

    result = run_paper_accounts(trades, PaperAccountConfig(max_trades_per_day=100, max_losses_per_day=100))

    assert result.profitable is True
    assert result.attempts_until_profit == 1
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.blew_up is False
    assert account.ending_balance > DEFAULT_STARTING_BALANCE
    assert account.wins == 30
    assert all(trade.won for trade in account.trades)


def test_blown_account_opens_a_fresh_hundred_k() -> None:
    """A catastrophic losing run blows the account and opens a new $100k."""
    # Each trade risks ~1% and loses 50R -> guarantees blowup quickly.
    trades = tuple(_trade("-50") for _ in range(200))

    result = run_paper_accounts(
        trades,
        PaperAccountConfig(max_trades_per_day=100, max_losses_per_day=100, daily_loss_fraction=Decimal("1")),
    )

    assert len(result.accounts) >= 2
    assert result.accounts[0].blew_up is True
    assert result.accounts[1].starting_balance == DEFAULT_STARTING_BALANCE
    assert result.profitable is False


def test_daily_limits_cap_trades_and_losses() -> None:
    """No more than the configured trades/losses are taken per day."""
    trades = tuple(_trade("-1", day="2026-07-13") for _ in range(20))

    result = run_paper_accounts(
        trades,
        PaperAccountConfig(max_trades_per_day=3, max_losses_per_day=3),
    )

    day_trades = [t for account in result.accounts for t in account.trades if t.trade_date == "2026-07-13"]
    assert len(day_trades) <= 3


def test_money_math_is_decimal_and_pnl_scales_with_contracts() -> None:
    """P&L is Decimal and proportional to size and R."""
    result = run_paper_accounts(
        (_trade("1"),),
        PaperAccountConfig(max_trades_per_day=10, max_losses_per_day=10),
    )
    trade = result.accounts[0].trades[0]

    assert isinstance(trade.pnl, Decimal)
    assert trade.contracts >= 1
    expected = (Decimal("1") * PaperAccountConfig().risk_per_contract() * Decimal(trade.contracts)).quantize(
        Decimal("0.01"),
    )
    assert trade.pnl == expected


def test_consistency_report_scores_profitable_run() -> None:
    """A clean profitable run scores high with readable progress lines."""
    trades = tuple(_trade("1.5") for _ in range(40))

    result = run_paper_accounts(trades, PaperAccountConfig(max_trades_per_day=100, max_losses_per_day=100))
    report = score_run(result)

    assert report.profitable is True
    assert report.total_trades == 40
    assert report.win_rate == Decimal("1.0000")
    assert report.consistency_score >= Decimal("50")
    assert any("profitable after" in line for line in report.summary_lines())


def test_consistency_report_scores_blown_run_low() -> None:
    """A blown, unprofitable run scores low and reports the loss streak."""
    trades = tuple(_trade("-50") for _ in range(200))

    result = run_paper_accounts(
        trades,
        PaperAccountConfig(max_trades_per_day=100, max_losses_per_day=100, daily_loss_fraction=Decimal("1")),
    )
    report = score_run(result)

    assert report.profitable is False
    assert report.consistency_score < Decimal("50")
    assert report.max_loss_streak >= 1


def test_attempt_cap_is_respected() -> None:
    """The run never exceeds the configured account-attempt cap."""
    trades = tuple(_trade("-50") for _ in range(10000))

    result = run_paper_accounts(
        trades,
        PaperAccountConfig(max_trades_per_day=100, max_losses_per_day=100, daily_loss_fraction=Decimal("1"), max_account_attempts=5),
    )

    assert len(result.accounts) <= 5

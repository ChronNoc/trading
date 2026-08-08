"""Passive (maker) limit-entry scalping: honest fills, honest misses.

A limit entry rests at the near touch and fills AT that price when the market
trades to it - no spread paid, no adverse entry slippage - which is how live
limit-scalping on Tradovate fills. Crucially, a resting order that never gets
reached is CANCELLED (a missed scalp), never counted as a trade, and a fill that
immediately runs to the stop is booked as the loss it is (no hidden edge).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.paper.options import read_entry_settings, read_scalping_cadence
from app.research.episode_builder import EpisodeConfig
from app.paper.execution import (
    REASON_APPROVED,
    REASON_LIMIT_UNFILLED,
    ExecutionConfig,
    MarketTick,
    PaperExecutor,
)
from app.paper.models import CloseReason, Direction, OrderStatus, PaperOrderIntent, RiskDecision, SetupProvenance

SEC = 1_000_000_000


def _prov(setup_id: str = "s1", index: int = 10) -> SetupProvenance:
    return SetupProvenance(
        session_id="session_20260717T140000Z", setup_id=setup_id,
        strategy_version="order-flow-plan-v1", contract="MNQU6",
        decision_event_index=index, decision_ts_ns=index * SEC,
    )


def _long(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    return PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29490.00"), target=Decimal("29520.00"),
                            provenance=_prov(setup_id, index))


def _short(setup_id: str = "s1", index: int = 10) -> PaperOrderIntent:
    return PaperOrderIntent(direction=Direction.SHORT, entry_reference=Decimal("29500.00"),
                            stop=Decimal("29510.00"), target=Decimal("29480.00"),
                            provenance=_prov(setup_id, index))


def _tick(index: int, price: str, bid: str | None = None, ask: str | None = None,
          *, ts: int | None = None) -> MarketTick:
    return MarketTick(event_index=index, ts_ns=(ts if ts is not None else index * SEC),
                      price=Decimal(price),
                      best_bid=Decimal(bid) if bid else None,
                      best_ask=Decimal(ask) if ask else None)


def _approve(contracts: int = 1) -> RiskDecision:
    return RiskDecision(approved=True, contracts=contracts, reason_code=REASON_APPROVED, reason="ok")


def _limit_cfg(**over: object) -> ExecutionConfig:
    base: dict = dict(entry_order_type="limit", entry_limit_timeout_ns=60 * SEC,
                      entry_limit_cancel_ticks=Decimal("8"))
    base.update(over)
    return ExecutionConfig(**base)


def _limit_executor(**over: object) -> PaperExecutor:
    return PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20, config=_limit_cfg(**over))


def _market_executor() -> PaperExecutor:
    return PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20, config=ExecutionConfig())


# --- config safety ------------------------------------------------------------


def test_limit_mode_requires_a_cancel_or_timeout() -> None:
    """An unfilled resting order with no cap would block every future entry."""
    with pytest.raises(ValueError):
        ExecutionConfig(entry_order_type="limit")  # no timeout, no cancel distance


# --- fills --------------------------------------------------------------------


def test_limit_buy_rests_at_the_bid_and_fills_there_without_paying_the_spread() -> None:
    ex = _limit_executor()
    # bid 29500.00 / ask 29500.25 -> a passive buy joins the bid at 29500.00
    order = ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"),
                      trading_day="2026-07-17")
    assert order.entry_limit_price == Decimal("29500.00")
    # price holds above the bid -> no fill, order still resting
    ex.on_tick(_tick(11, "29500.125", "29500.00", "29500.25"))
    assert ex.position is None
    assert ex.pending is not None
    # the offer reaches our bid (a seller is now at 29500) -> maker fill AT 29500.00
    ex.on_tick(_tick(12, "29500.00", "29499.75", "29500.00"))
    assert ex.pending is None
    assert ex.position is not None
    assert ex.position.entry_price == Decimal("29500.00")  # no spread, no slippage


def test_limit_sell_rests_at_the_ask_and_fills_when_the_bid_reaches_it() -> None:
    ex = _limit_executor()
    order = ex.submit(_short(), _approve(), _tick(10, "29500.125", "29499.75", "29500.00"),
                      trading_day="2026-07-17")
    assert order.entry_limit_price == Decimal("29500.00")  # joins the ask
    ex.on_tick(_tick(11, "29500.125", "29499.75", "29500.00"))
    assert ex.position is None
    # the bid rises to our ask -> maker fill AT 29500.00
    ex.on_tick(_tick(12, "29500.00", "29500.00", "29500.25"))
    assert ex.position is not None
    assert ex.position.entry_price == Decimal("29500.00")


def test_offset_rests_deeper_in_the_book() -> None:
    ex = _limit_executor(entry_limit_offset_ticks=Decimal("2"))
    order = ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"),
                      trading_day="2026-07-17")
    assert order.entry_limit_price == Decimal("29499.50")  # bid - 2 ticks


def test_require_trade_through_does_not_fill_on_a_mere_touch() -> None:
    ex = _limit_executor(entry_require_trade_through=True)
    ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"), trading_day="2026-07-17")
    # ask merely touches the limit -> no fill under the strict rule
    ex.on_tick(_tick(11, "29500.00", "29499.75", "29500.00"))
    assert ex.position is None
    # ask trades strictly through -> fill
    ex.on_tick(_tick(12, "29499.75", "29499.50", "29499.75"))
    assert ex.position is not None
    assert ex.position.entry_price == Decimal("29500.00")


# --- honest misses (never a trade) -------------------------------------------


def test_unfilled_limit_is_cancelled_on_timeout_not_traded() -> None:
    ex = _limit_executor(entry_limit_timeout_ns=5 * SEC, entry_limit_cancel_ticks=Decimal("0"))
    order = ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25", ts=10 * SEC),
                      trading_day="2026-07-17")
    ex.on_tick(_tick(11, "29500.125", "29500.00", "29500.25", ts=11 * SEC))  # 1s: still resting
    assert ex.pending is not None
    ex.on_tick(_tick(12, "29500.125", "29500.00", "29500.25", ts=16 * SEC))  # 6s > 5s: cancel
    assert ex.pending is None
    assert ex.position is None
    assert order.status is OrderStatus.CANCELLED
    assert order.reason_code == REASON_LIMIT_UNFILLED
    assert len(ex.trades) == 0
    assert len(ex.cancellations) == 1
    assert len(ex.rejections) == 0  # a miss is not a risk rejection


def test_unfilled_limit_is_cancelled_when_price_runs_away() -> None:
    ex = _limit_executor(entry_limit_cancel_ticks=Decimal("2"), entry_limit_timeout_ns=0)
    ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"), trading_day="2026-07-17")
    # bid runs up 3 ticks above our resting price -> the setup left without us
    ex.on_tick(_tick(11, "29500.875", "29500.75", "29501.00"))
    assert ex.position is None
    assert ex.pending is None
    assert len(ex.cancellations) == 1
    assert len(ex.trades) == 0


# --- adverse selection is not hidden -----------------------------------------


def test_a_maker_fill_that_runs_to_the_stop_is_booked_as_the_loss() -> None:
    """Resting orders fill as price trades through them; the sim must not pretend
    that fill was free of the move that caused it."""
    ex = _limit_executor()
    ex.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"), trading_day="2026-07-17")
    # offer trades down to our bid -> filled at 29500.00, but the move continues...
    ex.on_tick(_tick(11, "29500.00", "29499.75", "29500.00"))
    assert ex.position is not None
    # ...straight down through the stop at 29490 -> a real loss, not a phantom win
    trade = ex.on_tick(_tick(12, "29489.50", "29489.25", "29489.50"))
    assert trade is not None
    assert trade.close_reason is CloseReason.STOP
    assert trade.net_pnl < 0


# --- the whole point: maker beats taker on the same winning scalp -------------


def test_maker_entry_beats_market_entry_on_an_identical_winning_scalp() -> None:
    path = [
        _tick(11, "29500.00", "29499.75", "29500.00"),   # both fill here
        _tick(12, "29520.00", "29519.75", "29520.00"),   # target reached -> both win
    ]
    market = _market_executor()
    market.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"), trading_day="2026-07-17")
    for t in path:
        market.on_tick(t)

    limit = _limit_executor()
    limit.submit(_long(), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"), trading_day="2026-07-17")
    for t in path:
        limit.on_tick(t)

    assert len(market.trades) == 1 and len(limit.trades) == 1
    # The maker joined the bid (29500.00); the taker lifted the offer + slippage.
    assert limit.trades[0].entry_price < market.trades[0].entry_price
    assert limit.trades[0].entry_price == Decimal("29500.00")
    # Same exit, cheaper entry -> the identical move nets strictly more.
    assert limit.trades[0].net_pnl > market.trades[0].net_pnl


# --- config wiring (fail-closed) ---------------------------------------------


def _write_cfg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "production_config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_read_entry_settings_defaults_to_market(tmp_path: Path) -> None:
    assert read_entry_settings(tmp_path / "missing.yaml")["entry_order_type"] == "market"
    assert read_entry_settings(_write_cfg(tmp_path, "paper_entry_order_type: market\n"))[
        "entry_order_type"] == "market"


def test_read_entry_settings_enables_limit_with_a_cap(tmp_path: Path) -> None:
    settings = read_entry_settings(_write_cfg(
        tmp_path,
        "paper_entry_order_type: limit\n"
        "paper_entry_limit_timeout_seconds: 30\n"
        "paper_entry_limit_offset_ticks: 1\n"
        "paper_entry_require_trade_through: true\n",
    ))
    assert settings["entry_order_type"] == "limit"
    assert settings["entry_limit_timeout_seconds"] == Decimal("30")
    assert settings["entry_limit_offset_ticks"] == Decimal("1")
    assert settings["entry_require_trade_through"] is True
    # It splats straight into EpisodeConfig without error.
    EpisodeConfig(**settings)  # type: ignore[arg-type]


def test_read_entry_settings_ignores_uncapped_limit(tmp_path: Path) -> None:
    """A limit with no timeout and no cancel would stall the engine -> fail closed."""
    assert read_entry_settings(_write_cfg(tmp_path, "paper_entry_order_type: limit\n"))[
        "entry_order_type"] == "market"


def test_episode_config_rejects_an_uncapped_limit() -> None:
    with pytest.raises(ValueError):
        EpisodeConfig(entry_order_type="limit")
    EpisodeConfig(entry_order_type="limit", entry_limit_timeout_seconds=Decimal("30"))  # ok


# --- scalping cadence (cooldown + time stop) ---------------------------------


def test_read_scalping_cadence_defaults_and_overrides(tmp_path: Path) -> None:
    default = read_scalping_cadence(tmp_path / "missing.yaml")
    assert default == {"entry_cooldown_seconds": Decimal("300"), "time_stop_seconds": Decimal("900")}
    fast = read_scalping_cadence(_write_cfg(
        tmp_path, "paper_entry_cooldown_seconds: 5\npaper_time_stop_seconds: 60\n"))
    assert fast["entry_cooldown_seconds"] == Decimal("5")
    assert fast["time_stop_seconds"] == Decimal("60")
    EpisodeConfig(**fast)  # type: ignore[arg-type]  # splats cleanly


def test_read_scalping_cadence_fails_closed_on_a_bad_time_stop(tmp_path: Path) -> None:
    # A zero cooldown is honoured (back-to-back scalps); a non-positive time stop
    # keeps the safe default rather than removing the cap.
    cfg = read_scalping_cadence(_write_cfg(
        tmp_path, "paper_entry_cooldown_seconds: 0\npaper_time_stop_seconds: 0\n"))
    assert cfg["entry_cooldown_seconds"] == Decimal("0")
    assert cfg["time_stop_seconds"] == Decimal("900")


def test_a_short_cooldown_lets_the_next_scalp_enter_quickly() -> None:
    """The 300s default would block a trade 9s later; a 5s cooldown does not."""
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=20,
                       config=ExecutionConfig(cooldown_ns=5 * SEC, max_entries_per_day=0))
    ex.submit(_long("a", 10), _approve(), _tick(10, "29500.125", "29500.00", "29500.25"),
              trading_day="d")
    ex.on_tick(_tick(11, "29500.25", "29500.25", "29500.50"))          # taker fill @ ts 11s
    assert ex.on_tick(_tick(12, "29520.00", "29519.75", "29520.00")) is not None  # target win, now flat
    # A fresh setup 9s after the entry: not cooldown-blocked at 5s (would be at 300s).
    later = _tick(20, "29500.125", "29500.00", "29500.25")
    assert ex.gate(_long("b", 20), later, trading_day="d") is None

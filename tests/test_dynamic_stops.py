"""Break-even and trailing stop management: reduces risk, never widens it."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.paper.execution import ExecutionConfig, MarketTick, PaperExecutor, _tighter_stop
from app.paper.models import Direction, PaperOrderIntent, RiskDecision, SetupProvenance

TICK = Decimal("0.25")


def _provenance() -> SetupProvenance:
    return SetupProvenance(session_id="s", setup_id="s:1", strategy_version="v",
                           contract="MNQ", decision_event_index=0, decision_ts_ns=0,
                           conditions_passed=())


def _tick(i: int, price: str, *, bid: str | None = None, ask: str | None = None) -> MarketTick:
    return MarketTick(event_index=i, ts_ns=i * 1_000_000_000, price=Decimal(price),
                      best_bid=Decimal(bid) if bid else Decimal(price) - TICK,
                      best_ask=Decimal(ask) if ask else Decimal(price) + TICK)


def _open_long(executor: PaperExecutor, entry: str, stop: str, target: str) -> None:
    intent = PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal(entry),
                              stop=Decimal(stop), target=Decimal(target), provenance=_provenance())
    executor.submit(intent, RiskDecision(approved=True, contracts=1,
                    reason_code="ok", reason="ok"), _tick(0, entry), trading_day="2026-07-15")
    executor.on_tick(_tick(1, entry))  # fill next event


def test_tighter_stop_never_loosens() -> None:
    assert _tighter_stop(Decimal("100"), Decimal("102"), is_long=True) == Decimal("102")
    assert _tighter_stop(Decimal("100"), Decimal("98"), is_long=True) == Decimal("100")
    assert _tighter_stop(Decimal("100"), Decimal("98"), is_long=False) == Decimal("98")
    assert _tighter_stop(Decimal("100"), Decimal("102"), is_long=False) == Decimal("100")


def test_break_even_moves_stop_to_entry_after_favourable_move() -> None:
    cfg = ExecutionConfig(break_even_trigger_ticks=Decimal("8"),  # +2 pts
                          break_even_lock_ticks=Decimal("2"))     # lock +0.5 pt
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=5, config=cfg)
    _open_long(ex, "29200.00", "29190.00", "29230.00")  # 10-pt initial risk
    assert ex.position is not None
    entry = ex.position.entry_price
    original_stop = ex.position.stop
    # Price runs well past the 8-tick (2-pt) trigger.
    ex.on_tick(_tick(2, "29205.00"))
    # Stop is now entry + lock ticks (break-even), far tighter than the original.
    assert ex.position.stop == entry + Decimal("2") * TICK
    assert ex.position.stop > original_stop


def test_break_even_turns_a_would_be_loss_into_a_tiny_win() -> None:
    cfg = ExecutionConfig(break_even_trigger_ticks=Decimal("8"),
                          break_even_lock_ticks=Decimal("2"),
                          entry_slippage_ticks=Decimal("0"), stop_slippage_ticks=Decimal("0"))
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=5, config=cfg)
    _open_long(ex, "29200.00", "29190.00", "29230.00")
    entry = ex.position.entry_price
    ex.on_tick(_tick(2, "29205.00"))            # trigger break-even
    raised = entry + Decimal("2") * TICK
    assert ex.position.stop == raised
    trade = ex.on_tick(_tick(3, str(raised - Decimal("1.00"))))  # pull back through it
    assert trade is not None
    # Without break-even this would have run to 29190 (a ~$20 loss on MNQ);
    # instead it exits near break-even - the downside is capped, not the full stop.
    assert trade.net_pnl > Decimal("-5"), "downside is capped near break-even"


def test_trailing_stop_follows_the_best_price() -> None:
    cfg = ExecutionConfig(trail_activation_ticks=Decimal("8"), trail_distance_ticks=Decimal("8"),
                          entry_slippage_ticks=Decimal("0"), stop_slippage_ticks=Decimal("0"))
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=5, config=cfg)
    _open_long(ex, "29200.00", "29190.00", "29260.00")
    ex.on_tick(_tick(2, "29210.00"))  # +10 pts: trail arms, stop -> 29208 (8 ticks behind)
    assert ex.position is not None and ex.position.stop == Decimal("29208.00")
    # Price advances further; the trail tightens but never loosens.
    ex.on_tick(_tick(3, "29215.00"))
    assert ex.position.stop == Decimal("29213.00")
    ex.on_tick(_tick(4, "29214.00"))  # small pullback does NOT loosen the stop
    assert ex.position.stop == Decimal("29213.00")


def test_disabled_by_default_leaves_the_stop_untouched() -> None:
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=5)  # default config: all 0
    _open_long(ex, "29200.00", "29190.00", "29230.00")
    ex.on_tick(_tick(2, "29210.00"))
    assert ex.position is not None and ex.position.stop == Decimal("29190.00")


def test_config_rejects_trailing_without_distance() -> None:
    import pytest

    with pytest.raises(ValueError):
        ExecutionConfig(trail_activation_ticks=Decimal("10"), trail_distance_ticks=Decimal("0"))


def test_daily_limit_reader_and_wiring(tmp_path: Path) -> None:
    from app.paper.options import read_daily_limits

    assert read_daily_limits(tmp_path / "missing.yaml") == {
        "max_entries_per_day": 3, "max_losses_per_day": 3}
    cfg = tmp_path / "c.yaml"
    cfg.write_text("paper_max_entries_per_day: 10\npaper_max_losses_per_day: 3\n", encoding="utf-8")
    limits = read_daily_limits(cfg)
    assert limits["max_entries_per_day"] == 10 and limits["max_losses_per_day"] == 3
    # 0 is valid and means unlimited; a negative or non-numeric is rejected.
    zero = tmp_path / "zero.yaml"
    zero.write_text("paper_max_entries_per_day: 0\n", encoding="utf-8")
    assert read_daily_limits(zero)["max_entries_per_day"] == 0, "0 = unlimited"
    bad = tmp_path / "bad.yaml"
    bad.write_text("paper_max_entries_per_day: -5\n", encoding="utf-8")
    assert read_daily_limits(bad)["max_entries_per_day"] == 3, "reject negative, keep default"
    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        assert "read_daily_limits" in Path(launcher).read_text(encoding="utf-8")


def test_engine_honours_the_configured_daily_entry_limit() -> None:
    from app.paper.streaming_engine import DelayedPaperEngine
    from app.research.episode_builder import EpisodeConfig

    engine = DelayedPaperEngine(config=EpisodeConfig(max_entries_per_day=10))
    assert engine._exec_config.max_entries_per_day == 10  # threaded config -> executor


def test_zero_daily_cap_means_unlimited() -> None:
    """A cap of 0 (learning stage) never blocks on the daily limit."""
    cfg = ExecutionConfig(max_entries_per_day=0, max_losses_per_day=0)
    ex = PaperExecutor(starting_balance=Decimal("25000"), max_contracts=5, config=cfg)
    # Simulate having already entered/lost far more than the old cap of 3.
    ex._day_entries = 50
    ex._day_losses = 50
    intent = PaperOrderIntent(direction=Direction.LONG, entry_reference=Decimal("29200.00"),
                              stop=Decimal("29199.50"), target=Decimal("29202.00"),
                              provenance=_provenance())
    decision = ex.gate(intent, _tick(1, "29200.00"), trading_day="2026-07-15")
    # gate() returns None (not blocked) when nothing rejects; the daily caps must
    # NOT be what stops it here.
    assert decision is None or decision.reason_code not in ("daily_entry_lock", "daily_loss_lock")


def test_reader_accepts_zero_as_unlimited(tmp_path: Path) -> None:
    from app.paper.options import read_daily_limits

    cfg = tmp_path / "u.yaml"
    cfg.write_text("paper_max_entries_per_day: 0\npaper_max_losses_per_day: 0\n", encoding="utf-8")
    limits = read_daily_limits(cfg)
    assert limits["max_entries_per_day"] == 0 and limits["max_losses_per_day"] == 0


def test_config_reader_and_launcher_wiring(tmp_path: Path) -> None:
    from app.paper.options import read_stop_settings

    assert read_stop_settings(tmp_path / "missing.yaml")["break_even_trigger_ticks"] == Decimal("0")
    cfg = tmp_path / "c.yaml"
    cfg.write_text("paper_break_even_trigger_ticks: 24\npaper_trail_activation_ticks: 40\n"
                   "paper_trail_distance_ticks: 24\n", encoding="utf-8")
    settings = read_stop_settings(cfg)
    assert settings["break_even_trigger_ticks"] == Decimal("24")
    assert settings["trail_activation_ticks"] == Decimal("40")
    for launcher in ("tools/start_backend.py", "tools/start_assistant.py"):
        assert "read_stop_settings" in Path(launcher).read_text(encoding="utf-8")

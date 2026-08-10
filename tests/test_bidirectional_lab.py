"""Tests for the isolated Bidirectional Paper Trading Lab engine.

Covers the experiment's critical guarantees: pre-armed activation levels, atomic
paired long+short entry from a single market event, independent tight-stop legs,
honest gap/overshoot handling, costs, break-even, monotonic trailing, and - the
hard safety rule - that the lab cannot import or reach any live-execution code.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal as D

import pytest

from app.labs.bidirectional import (
    ARMED,
    CANCELLED,
    COMPLETED,
    AccountConfig,
    ActivationSpec,
    BreakEvenConfig,
    LabEngine,
    MarketEvent,
    TrailConfig,
)
from app.labs.bidirectional.statistics import compute_statistics


def _acct(**over: object) -> AccountConfig:
    base: dict = dict(starting_balance=D("100000"), tick_size=D("0.25"), tick_value=D("0.50"),
                      commission_per_contract=D("0"), stop_slippage_ticks=D("0"))
    # Convenience: coerce numeric string overrides (e.g. commission) to Decimal.
    base.update({k: (D(v) if isinstance(v, str) else v) for k, v in over.items()})
    return AccountConfig(**base)


def _engine(**over: object) -> LabEngine:
    return LabEngine(_acct(**over))


def _ev(ts: int, last: str | None = None, bid: str | None = None, ask: str | None = None) -> MarketEvent:
    return MarketEvent(ts_ns=ts, last=D(last) if last else None,
                       bid=D(bid) if bid else None, ask=D(ask) if ask else None)


def _paired(activation_id: str = "1", price: str = "20000.00", long_qty: int = 100,
            short_qty: int = 100, stop_ticks: str = "2", **over: object) -> ActivationSpec:
    return ActivationSpec(activation_id=activation_id, price=D(price), long_qty=long_qty,
                          short_qty=short_qty, stop_ticks=D(stop_ticks), **over)


# --- activation levels: arm, wait, trigger -----------------------------------


def test_armed_level_does_nothing_before_price_reaches_it() -> None:
    e = _engine()
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    e.arm(_paired(price="20000.00"))
    e.on_event(_ev(2, "20005.00", "20004.75", "20005.00"))  # still above the level
    assert e.setups == []
    assert e.levels["1"].status == ARMED


def test_downward_activation_triggers_when_price_falls_into_the_level() -> None:
    e = _engine()
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    e.arm(_paired(price="20000.00"))
    e.on_event(_ev(2, "20000.00", "19999.75", "20000.00"))
    assert len(e.setups) == 1
    assert e.levels["1"].status == COMPLETED


def test_upward_activation_triggers_when_price_rises_into_the_level() -> None:
    e = _engine()
    e.on_event(_ev(1, "20000.00", "19999.75", "20000.00"))
    e.arm(_paired(price="20010.00"))  # auto -> at_or_above (level is above market)
    e.on_event(_ev(2, "20010.00", "20009.75", "20010.00"))
    assert len(e.setups) == 1


def test_touch_and_cross_conditions() -> None:
    touch = _engine()
    touch.on_event(_ev(1, "20001.00"))
    touch.arm(_paired(price="20000.00", comparison="touch"))
    touch.on_event(_ev(2, "20000.00"))
    assert len(touch.setups) == 1

    cross = _engine()
    cross.on_event(_ev(1, "20001.00"))
    cross.arm(_paired(price="20000.00", comparison="cross_down"))
    cross.on_event(_ev(2, "20000.50"))  # above -> no cross yet
    assert cross.setups == []
    cross.on_event(_ev(3, "19999.75"))  # crossed from above -> trigger
    assert len(cross.setups) == 1


# --- the critical atomic paired entry ----------------------------------------


def test_paired_legs_open_from_the_same_event_same_timestamp_and_seq() -> None:
    e = _engine()
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    e.arm(_paired(price="20000.00", long_qty=100, short_qty=100))
    e.on_event(_ev(20000, "20000.00", "19999.75", "20000.00"))
    s = e.setups[0]
    assert s.long_leg is not None and s.short_leg is not None
    assert s.long_leg.setup_id == s.short_leg.setup_id == s.setup_id
    assert s.long_leg.entry_ts_ns == s.short_leg.entry_ts_ns == s.trigger_ts_ns == 20000
    assert s.long_leg.entry_seq == s.short_leg.entry_seq == s.trigger_seq  # one event, both legs
    assert s.long_leg.activation_id == s.short_leg.activation_id == "1"


def test_long_fills_the_ask_and_short_fills_the_bid_spread_is_visible() -> None:
    e = _engine()
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    e.arm(_paired(price="20000.00"))
    e.on_event(_ev(2, "20000.00", "19999.75", "20000.25"))
    s = e.setups[0]
    assert s.long_leg.entry_price == D("20000.25")   # lifted the offer
    assert s.short_leg.entry_price == D("19999.75")  # hit the bid
    assert s.long_leg.entry_price > s.short_leg.entry_price  # paid the spread, not identical


def test_trigger_price_and_activation_price_are_stored_separately_with_overshoot() -> None:
    e = _engine()
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    e.arm(_paired(price="20000.00"))
    # gap straight through the level: previous 20001, this event 19998
    e.on_event(_ev(2, "20001.00", "20000.75", "20001.00"))
    e.on_event(_ev(3, "19998.00", "19997.75", "19998.00"))
    s = e.setups[0]
    assert s.activation_price == D("20000.00")
    assert s.trigger_price == D("19998.00")            # the real market, not the level
    assert s.overshoot_points == D("-2.00")            # gapped 2 points past the level
    # fills follow the real book, never fabricated at the level
    assert s.long_leg.entry_price == D("19998.00")
    assert s.short_leg.entry_price == D("19997.75")


# --- one-shot / repeat / re-arm ----------------------------------------------


def test_one_shot_level_cannot_trigger_twice() -> None:
    e = _engine()
    e.on_event(_ev(1, "20001.00"))
    e.arm(_paired(price="20000.00", one_shot=True))
    e.on_event(_ev(2, "20000.00"))
    e.on_event(_ev(3, "20000.00"))
    assert len(e.setups) == 1


def test_repeat_activation_requires_leaving_and_returning() -> None:
    e = _engine()
    e.on_event(_ev(1, "20001.00"))
    e.arm(_paired(price="20000.00", one_shot=False, max_activations=5))
    e.on_event(_ev(2, "20000.00"))  # trigger #1
    e.on_event(_ev(3, "20000.00"))  # sitting on the level -> must NOT fire again
    e.on_event(_ev(4, "20000.00"))
    assert len(e.setups) == 1
    e.on_event(_ev(5, "20002.00"))  # leaves the level (condition false)
    e.on_event(_ev(6, "20000.00"))  # returns -> trigger #2
    assert len(e.setups) == 2


def test_require_leave_and_reenter_by_distance() -> None:
    e = _engine()
    e.on_event(_ev(1, "20001.00"))
    e.arm(_paired(price="20000.00", one_shot=False, max_activations=5,
                  require_leave_reenter=True, leave_distance_ticks=D("4")))
    e.on_event(_ev(2, "20000.00"))       # trigger #1
    e.on_event(_ev(3, "20000.50"))       # only 2 ticks away -> not enough to re-arm
    e.on_event(_ev(4, "20000.00"))
    assert len(e.setups) == 1
    e.on_event(_ev(5, "20001.00"))       # 4 ticks away -> re-armed
    e.on_event(_ev(6, "20000.00"))       # returns -> trigger #2
    assert len(e.setups) == 2


def test_rearm_and_cancel_and_disable() -> None:
    e = _engine()
    e.on_event(_ev(1, "20001.00"))
    e.arm(_paired(activation_id="c", price="20000.00"))
    e.cancel("c")
    e.on_event(_ev(2, "20000.00"))
    assert e.setups == [] and e.levels["c"].status == CANCELLED

    e.arm(_paired(activation_id="d", price="20000.00", enabled=False))
    e.on_event(_ev(3, "20000.00"))
    assert e.setups == []  # disabled cannot trigger
    e.set_enabled("d", True)
    e.on_event(_ev(4, "20000.00"))
    assert len(e.setups) == 1


def test_multiple_independent_levels() -> None:
    e = _engine()
    e.on_event(_ev(1, "20060.00"))
    for aid, px in (("a", "20050.00"), ("b", "20025.00"), ("c", "20000.00")):
        e.arm(_paired(activation_id=aid, price=px))
    e.on_event(_ev(2, "20050.00"))
    e.on_event(_ev(3, "20025.00"))
    e.on_event(_ev(4, "20000.00"))
    assert len(e.setups) == 3
    assert {s.activation_id for s in e.setups} == {"a", "b", "c"}


# --- independent leg management ----------------------------------------------


def _armed_pair(**over: object) -> LabEngine:
    e = _engine(**{k: v for k, v in over.items() if k in {"commission_per_contract", "stop_slippage_ticks"}})
    e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
    spec_over = {k: v for k, v in over.items() if k not in {"commission_per_contract", "stop_slippage_ticks"}}
    e.arm(_paired(price="20000.00", **spec_over))
    e.on_event(_ev(2, "20000.00", "19999.75", "20000.00"))  # trigger
    return e


def test_price_down_stops_long_and_leaves_short_open() -> None:
    e = _armed_pair(stop_ticks="2")
    e.on_event(_ev(3, "19999.00", "19998.75", "19999.00"))  # below long stop 19999.50
    s = e.setups[0]
    assert s.long_leg.open is False and s.long_leg.exit_reason == "stop"
    assert s.short_leg.open is True


def test_price_up_stops_short_and_leaves_long_open() -> None:
    e = _armed_pair(stop_ticks="2")
    # short entry 19999.75, stop = 19999.75 + 0.50 = 20000.25
    e.on_event(_ev(3, "20001.00", "20000.75", "20001.00"))
    s = e.setups[0]
    assert s.short_leg.open is False and s.short_leg.exit_reason == "stop"
    assert s.long_leg.open is True


def test_one_tick_stop_is_allowed() -> None:
    e = _armed_pair(stop_ticks="1")
    s = e.setups[0]
    assert s.long_leg.stop == s.long_leg.entry_price - D("0.25")
    e.on_event(_ev(3, "19999.50", "19999.25", "19999.50"))  # one tick down stops the long
    assert s.long_leg.open is False


def test_combined_paired_net_includes_commission() -> None:
    e = _armed_pair(stop_ticks="2", commission_per_contract="0.62")
    s = e.setups[0]
    # walk price down: long stops, short runs then we flatten
    for p in ("19999.00", "19995.00", "19990.00"):
        e.on_event(_ev(int(float(p)), p, str(float(p) - 0.25), p))
    e.flatten("session_close")
    assert s.closed
    # net = long loss + short profit - all commission; deterministic and finite
    assert s.net_pnl == s.long_leg.net_pnl + s.short_leg.net_pnl
    assert s.long_leg.commission == D("124.00") and s.short_leg.commission == D("124.00")


# --- break-even and trailing -------------------------------------------------


def test_break_even_moves_the_stop_to_entry() -> None:
    e = _engine()
    e.on_event(_ev(1, "20000.00", "19999.75", "20000.00"))
    e.arm(_paired(activation_id="be", price="20000.00", long_qty=0, short_qty=10, stop_ticks="10",
                  break_even=BreakEvenConfig(enabled=True, trigger_ticks=D("3"), offset_ticks=D("0"))))
    e.on_event(_ev(2, "20000.00", "19999.75", "20000.00"))  # short entry 19999.75
    short = e.setups[0].short_leg
    assert short.stop == D("19999.75") + D("2.50")  # 10-tick stop above entry
    e.on_event(_ev(3, "19999.00", "19998.75", "19999.00"))  # 3 ticks favourable -> break-even
    assert short.break_even_active is True
    assert short.stop == short.entry_price  # locked at entry


def test_trailing_only_tightens_never_widens() -> None:
    e = _engine()
    e.on_event(_ev(1, "20000.00", "19999.75", "20000.00"))
    e.arm(_paired(activation_id="tr", price="20000.00", long_qty=0, short_qty=10, stop_ticks="20",
                  trailing=TrailConfig(enabled=True, activation_ticks=D("2"), distance_ticks=D("2"))))
    e.on_event(_ev(2, "20000.00", "19999.75", "20000.00"))
    short = e.setups[0].short_leg
    stops = []
    for p in ("19998.00", "19996.00", "19997.00", "19994.00"):  # down, down, up, down
        e.on_event(_ev(int(float(p)), p, str(float(p) - 0.25), p))
        stops.append(short.stop)
    # a short's trailing stop may only move DOWN (favourable), never back up
    assert all(later <= earlier for earlier, later in zip(stops, stops[1:]))


def test_large_quantities_work() -> None:
    for qty in (1, 5, 100, 500, 1000):
        e = _engine(max_gross_contracts=10_000)
        e.on_event(_ev(1, "20010.00", "20009.75", "20010.00"))
        e.arm(_paired(price="20000.00", long_qty=qty, short_qty=qty))
        e.on_event(_ev(2, "20000.00", "19999.75", "20000.00"))
        s = e.setups[0]
        assert s.long_leg.qty == qty and s.short_leg.qty == qty


def test_manual_enter_both_uses_the_same_paired_engine() -> None:
    e = _engine()
    e.on_event(_ev(1, "20000.00", "19999.75", "20000.00"))
    s = e.enter_both(long_qty=50, short_qty=50, stop_ticks=D("2"))
    assert s is not None
    assert s.long_leg.entry_seq == s.short_leg.entry_seq == s.trigger_seq
    assert s.long_leg.entry_ts_ns == s.short_leg.entry_ts_ns


def test_account_equity_and_drawdown_update() -> None:
    e = _armed_pair(stop_ticks="2", commission_per_contract="0.62")
    e.on_event(_ev(3, "19999.00", "19998.75", "19999.00"))  # long stops -> realised loss
    assert e.balance < e.account.starting_balance
    assert e.max_drawdown > 0


def test_statistics_are_computable_and_honest() -> None:
    e = _armed_pair(stop_ticks="2", commission_per_contract="0.62")
    for p in ("19999.00", "19995.00", "19990.00"):
        e.on_event(_ev(int(float(p)), p, str(float(p) - 0.25), p))
    e.flatten("session_close")
    stats = compute_statistics(e)
    assert stats["general"]["total_trades"] == 2
    assert stats["paired"]["paired_setups"] == 1
    assert stats["general"]["total_commission"] == D("248.00")
    # net is gross minus every cost; never fabricated
    assert stats["general"]["net_pnl"] == e.realized_pnl


def test_open_positions_and_recent_trades_expose_the_live_orders() -> None:
    """The live orders view: one open surviving leg with mgmt state, one closed loser."""
    from app.labs.bidirectional.statistics import open_positions, recent_trades

    e = _armed_pair(stop_ticks="2", commission_per_contract="0.62")
    e.on_event(_ev(3, "19999.00", "19998.75", "19999.00"))  # long stops, short survives
    positions = open_positions(e)
    assert len(positions) == 1
    survivor = positions[0]
    assert survivor["side"] == "short"
    assert survivor["qty"] == 100
    # unrealized is a numeric string the GUI renders as-is (survivor is in profit here)
    assert D(str(survivor["unrealized"])) > 0
    assert set(survivor) >= {"setup_id", "side", "qty", "entry", "stop",
                             "break_even", "trailing", "mfe_ticks", "unrealized"}

    trades = recent_trades(e)
    assert len(trades) == 1  # the stopped long is the only closed leg so far
    assert trades[0]["side"] == "long"
    assert trades[0]["reason"] == "stop"
    assert D(str(trades[0]["net"])) < 0  # honest: the loser is a loss net of costs


def test_live_publish_payload_includes_positions_and_recent_trades(tmp_path: pathlib.Path) -> None:
    from app.labs.bidirectional.live import LabConfigFile, LabLiveRunner, LabStateFile

    LabConfigFile(tmp_path).write({
        "enabled": True, "levels": ["20000.00"], "long": 100, "short": 100,
        "stop_ticks": "2", "commission": "0.62",
    })
    runner = LabLiveRunner(tmp_path)
    runner.observe(_trade("20010.00", 1), _FakeState(1, "20009.75", "20010.00"))  # arm
    runner.observe(_trade("20000.00", 2), _FakeState(2, "19999.75", "20000.00"))  # trigger pair
    runner.observe(_trade("19999.00", 3), _FakeState(3, "19998.75", "19999.00"))  # long stops
    runner._publish()  # bypass the time-throttled publish so the assertions see final state
    published = LabStateFile(tmp_path).read()
    assert published is not None
    assert "positions" in published and "recent_trades" in published
    assert any(p["side"] == "short" for p in published["positions"])
    assert any(t["side"] == "long" and t["reason"] == "stop" for t in published["recent_trades"])


# --- HARD SAFETY RULE: no live execution -------------------------------------


_REPO = pathlib.Path(__file__).resolve().parent.parent
_LAB_DIR = _REPO / "app" / "labs" / "bidirectional"
_FORBIDDEN_MODULES = ("app.execution", "app.market.receiver", "app.database.recorder",
                      "app.runtime.controller", "app.paper.execution")
_FORBIDDEN_NAMES = ("submit_order", "place_order", "place_bracket_order", "send_order",
                    "order_router", "live_order", "TradovateHttpClient", "TradovateAckClient",
                    "DemoConnectionService", "TradovateDemoGateway")


def test_lab_has_no_live_execution_imports_or_calls() -> None:
    """AST scan (docstrings/comments ignored): no import of, or reference to, live execution."""
    import ast

    for path in sorted(_LAB_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _is_forbidden_module(alias.name), f"{path.name}: import {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not _is_forbidden_module(module), f"{path.name}: from {module} import ..."
            elif isinstance(node, ast.Attribute):
                assert node.attr not in _FORBIDDEN_NAMES, f"{path.name}: .{node.attr}"
            elif isinstance(node, ast.Name):
                assert node.id not in _FORBIDDEN_NAMES, f"{path.name}: {node.id}"


def _is_forbidden_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in _FORBIDDEN_MODULES)


def test_importing_the_lab_pulls_in_no_live_execution_in_a_clean_interpreter() -> None:
    """The strong guarantee: in a fresh interpreter, importing the lab loads no live-exec module."""
    import subprocess
    import sys

    probe = (
        "import sys, app.labs.bidirectional, app.labs.bidirectional.statistics\n"
        "bad=[m for m in sys.modules if m.startswith('app.execution') "
        "or m in ('app.market.receiver','app.database.recorder')]\n"
        "print('LEAK:'+','.join(sorted(bad)))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=str(_REPO),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "LEAK:\n" in result.stdout + "\n" or result.stdout.strip().endswith("LEAK:"), result.stdout


# --- live tap (read-only feed sink) ------------------------------------------


class _FakeState:
    """Minimal stand-in for the backend MarketState the feed sink receives."""

    def __init__(self, ts: int, bid: str | None, ask: str | None) -> None:
        self.timestamp_ns = ts
        self.best_bid = D(bid) if bid else None
        self.best_ask = D(ask) if ask else None


def _trade(price: str, ts: int) -> dict:
    return {"timestamp_ns": ts, "sequence_id": 1, "price": price, "size": "1"}


def test_market_event_from_stream_distinguishes_trade_and_depth() -> None:
    from app.labs.bidirectional.live import market_event_from_stream

    st = _FakeState(1000, "19999.75", "20000.00")
    trade = market_event_from_stream(_trade("20000.00", 1000), st)
    assert trade.kind == "trade" and trade.last == D("20000.00")
    assert trade.bid == D("19999.75") and trade.ask == D("20000.00")
    depth = market_event_from_stream(
        {"type": "depth_update", "side": "bid", "price": "19999.75", "new_size": "5", "timestamp": 1000}, st)
    assert depth.kind == "depth" and depth.last is None and depth.bid == D("19999.75")


def test_lab_config_and_state_files_round_trip(tmp_path: pathlib.Path) -> None:
    from app.labs.bidirectional.live import LabConfigFile, LabStateFile

    cfg = LabConfigFile(tmp_path)
    cfg.write({"enabled": True, "levels": ["20000"]})
    assert cfg.read()["enabled"] is True
    state = LabStateFile(tmp_path)
    state.write({"stats": {"x": 1}})
    assert state.read()["stats"]["x"] == 1


def test_live_runner_arms_from_config_triggers_and_publishes(tmp_path: pathlib.Path) -> None:
    from app.labs.bidirectional.live import LabConfigFile, LabLiveRunner, LabStateFile

    LabConfigFile(tmp_path).write({
        "enabled": True, "starting_balance": "100000", "tick_size": "0.25", "tick_value": "0.50",
        "commission": "0", "stop_slip": "0", "long": 100, "short": 100, "stop_ticks": "2",
        "be_trigger": "0", "trail_dist": "0", "one_shot": True, "levels": ["20000.00"],
    })
    runner = LabLiveRunner(tmp_path)
    runner.observe(_trade("20010.00", 1), _FakeState(1, "20009.75", "20010.00"))  # loads config + primes
    runner.observe(_trade("20000.00", 2), _FakeState(2, "19999.75", "20000.00"))  # reaches the level
    assert runner._engine is not None
    assert len(runner._engine.setups) == 1  # atomic paired setup created from the live stream
    runner._publish()
    published = LabStateFile(tmp_path).read()
    assert published["stats"]["general"]["total_setups"] == 1
    assert published["levels"][0]["status"] in ("COMPLETED", "TRIGGERED")


def test_disabled_config_does_not_trade(tmp_path: pathlib.Path) -> None:
    from app.labs.bidirectional.live import LabConfigFile, LabLiveRunner

    LabConfigFile(tmp_path).write({"enabled": False, "levels": ["20000.00"], "stop_ticks": "2"})
    runner = LabLiveRunner(tmp_path)
    runner.observe(_trade("20000.00", 1), _FakeState(1, "19999.75", "20000.00"))
    assert runner._engine is None  # never armed while disabled


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

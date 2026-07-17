"""Paper ledger durability: append-only, recoverable, never silently overwritten.

Every test writes to pytest's ``tmp_path`` (an external temporary directory), so
no real recording, dataset, or user-owned ledger is touched.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.paper.ledger import PaperLedger
from app.paper.models import CloseReason, Direction, PaperTrade, SetupProvenance


def _trade(*, net: str = "38.26", balance: str = "25038.26", synthetic: bool = False) -> PaperTrade:
    """Build a deterministic closed trade for ledger tests only."""
    return PaperTrade(
        provenance=SetupProvenance(
            session_id="s1", setup_id="s1:long:100", strategy_version="v1",
            contract="MNQU5", decision_event_index=100, decision_ts_ns=1,
        ),
        direction=Direction.LONG, contracts=1,
        entry_price=Decimal("29500.25"), exit_price=Decimal("29520.00"),
        stop=Decimal("29490.00"), target=Decimal("29520.00"),
        opened_ts_ns=1, closed_ts_ns=2, opened_event_index=101, closed_event_index=150,
        close_reason=CloseReason.TARGET, gross_pnl=Decimal("39.50"),
        commission=Decimal("1.24"), slippage_cost=Decimal("1.00"),
        net_pnl=Decimal(net), r_multiple=Decimal("1.8"),
        mae_points=Decimal("-2"), mfe_points=Decimal("20"),
        balance_after=Decimal(balance), is_synthetic_fixture=synthetic,
    )


def test_append_persists_a_readable_record(tmp_path: Path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    ledger.append(_trade())
    lines = (tmp_path / "paper.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["session_id"] == "s1"
    assert record["close_reason"] == "target"
    assert record["schema_version"] == 1


def test_money_round_trips_as_text_never_float(tmp_path: Path) -> None:
    """A float in the ledger would silently corrupt P&L; assert the JSON types."""
    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    ledger.append(_trade())
    record = json.loads((tmp_path / "paper.jsonl").read_text(encoding="utf-8").strip())
    for field in ("net_pnl", "gross_pnl", "commission", "entry_price", "exit_price", "balance_after"):
        assert isinstance(record[field], str), f"{field} must be text, got {type(record[field])}"
    assert Decimal(record["net_pnl"]) == Decimal("38.26")


def test_reopening_never_overwrites_and_continues_the_file(tmp_path: Path) -> None:
    """Opening an existing ledger must preserve every prior row."""
    path = tmp_path / "paper.jsonl"
    first = PaperLedger(path, fsync=False)
    first.append(_trade())
    reopened = PaperLedger(path, fsync=False)
    assert reopened.recovered.records, "existing rows must be recovered, not discarded"
    reopened.append(_trade(net="-20.00", balance="25018.26"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    assert reopened.count == 2


def test_recovery_restores_balance_and_realized_pnl(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    ledger = PaperLedger(path, fsync=False)
    ledger.append(_trade(net="38.26", balance="25038.26"))
    ledger.append(_trade(net="-20.00", balance="25018.26"))
    recovery = PaperLedger.recover(path)
    assert recovery.realized_pnl == Decimal("18.26")
    assert recovery.last_balance(Decimal("25000")) == Decimal("25018.26")


def test_torn_final_line_from_a_crash_keeps_intact_records(tmp_path: Path) -> None:
    """A process killed mid-write must not cost us the whole history."""
    path = tmp_path / "paper.jsonl"
    ledger = PaperLedger(path, fsync=False)
    ledger.append(_trade())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"session_id": "s1", "net_pnl": "1.0')  # torn write
    recovery = PaperLedger.recover(path)
    assert len(recovery.records) == 1, "the intact record must survive"
    assert recovery.damaged_tail is True, "damage must be reported, not hidden"


def test_synthetic_fixtures_are_excluded_from_real_statistics(tmp_path: Path) -> None:
    """Fixture trades may exist in the file but may never count as real results."""
    path = tmp_path / "paper.jsonl"
    ledger = PaperLedger(path, fsync=False)
    ledger.append(_trade(net="500.00", balance="25500.00", synthetic=True))
    ledger.append(_trade(net="38.26", balance="25038.26"))
    recovery = PaperLedger.recover(path)
    assert len(recovery.records) == 2
    assert len(recovery.real_records) == 1
    assert recovery.realized_pnl == Decimal("38.26"), "synthetic P&L must not inflate results"


def test_missing_ledger_recovers_empty_without_creating_anything(tmp_path: Path) -> None:
    recovery = PaperLedger.recover(tmp_path / "absent.jsonl")
    assert recovery.records == ()
    assert not (tmp_path / "absent.jsonl").exists()


def test_engine_closed_trades_flow_into_the_ledger(tmp_path: Path) -> None:
    """The engine's trade-closed sink is what makes persistence automatic."""
    from app.paper.streaming_engine import DelayedPaperEngine

    ledger = PaperLedger(tmp_path / "paper.jsonl", fsync=False)
    engine = DelayedPaperEngine(is_synthetic_fixture=True)
    engine.on_trade_closed(ledger.append)
    trade = _trade(synthetic=True)
    for callback in engine._on_trade_closed:  # noqa: SLF001 - verifying the wiring
        callback(trade)
    assert PaperLedger.recover(tmp_path / "paper.jsonl").records, "closed trades must persist"

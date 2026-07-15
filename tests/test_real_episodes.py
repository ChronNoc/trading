"""Tests for the real completed-outcome loader (data/processed)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.research.real_episodes import load_completed_real_outcomes

_REAL = {
    "session_id": "session_20260715T002231Z", "setup_id": "s1", "provenance": "REAL_DELAYED",
    "direction": "long", "trading_day": "2026-07-15",
    "decision_ts_ns": 1_752_537_751_000_000_000, "entry_ts_ns": 1_752_537_752_000_000_000,
    "exit_ts_ns": 1_752_537_811_000_000_000, "defended_price": "29450.00",
    "entry_reference_price": "29451.00", "entry": "29451.25",
    "stop": "29441.25", "target": "29471.25", "exit_reference_price": "29471.25",
    "exit": "29471.00", "commission": "1.24", "slippage_cost": "1.00",
    "gross_pnl_per_contract": "40.50", "net_pnl_per_contract": "38.26",
    "risk_per_contract": "22.24", "r_multiple": "1.7203",
    "outcome": "target_first", "strategy_version": "order-flow-plan-v1",
    "builder_version": "real-episodes-v2", "source_event_range": [1, 200],
    "decision_hash": "d" * 64, "input_hash": "i" * 64,
    "ordering_mode": "receive_sequence", "ordering_ambiguous": False,
    "data_quality_ok": True, "eligible_for_ledger": True,
    "decision": "accepted", "strategy_accepted": True,
}


def _write(processed: Path, records: list[dict]) -> None:
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "session_test.episodes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8",
    )


def test_empty_processed_returns_no_outcomes(tmp_path: Path) -> None:
    assert load_completed_real_outcomes(tmp_path / "nope") == ()
    (tmp_path / "processed").mkdir()
    assert load_completed_real_outcomes(tmp_path / "processed") == ()


def test_real_outcome_is_loaded_with_decimal_and_lineage(tmp_path: Path) -> None:
    _write(tmp_path / "processed", [_REAL])
    outcomes = load_completed_real_outcomes(tmp_path / "processed")
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.provenance == "REAL_DELAYED"
    assert isinstance(o.r_multiple, Decimal) and o.r_multiple == Decimal("1.7203")
    assert o.session_id == "session_20260715T002231Z"
    assert o.eligible_for_ledger is True


def test_synthetic_provenance_is_structurally_rejected(tmp_path: Path) -> None:
    """The real loader refuses any SYNTHETIC-classified record."""
    fake = {**_REAL, "provenance": "SYNTHETIC"}
    _write(tmp_path / "processed", [fake])
    assert load_completed_real_outcomes(tmp_path / "processed") == ()


def test_ambiguous_and_unfinished_outcomes_excluded(tmp_path: Path) -> None:
    """Ambiguous / unfinished outcomes never reach the ledger."""
    ambiguous = {**_REAL, "outcome": "ambiguous"}
    unfinished = {**_REAL, "outcome": "open"}
    _write(tmp_path / "processed", [ambiguous, unfinished])
    assert load_completed_real_outcomes(tmp_path / "processed") == ()


def test_quality_or_ordering_failure_is_structurally_rejected(tmp_path: Path) -> None:
    bad_quality = {**_REAL, "data_quality_ok": False}
    ambiguous_order = {**_REAL, "ordering_ambiguous": True}
    _write(tmp_path / "processed", [bad_quality, ambiguous_order])
    assert load_completed_real_outcomes(tmp_path / "processed") == ()

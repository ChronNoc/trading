"""Tests for the honest profitability progress meter (evidence ladder)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.research.paper_ledger import run_real_paper_ledger
from app.research.profitability_progress import (
    STATUS_BLOCKED,
    STATUS_INSUFFICIENT,
    STATUS_PASSED,
    ProgressConfig,
    compute_progress,
)
from app.research.real_episodes import CompletedRealOutcome
from app.research.session_catalog import SessionEntry


def _session(idx: int, *, clean: bool = True, eligible: bool = True) -> SessionEntry:
    return SessionEntry(
        session_id=f"session_{idx}",
        manifest_path=Path(f"data/raw/2026-07-15/session_{idx}/session_manifest.json"),
        provenance="REAL_DELAYED",
        is_delayed=True,
        finalized=True,
        active=False,
        continuity_status="clean" if clean else "gaps_detected",
        depth_updates=10_000,
        trades=2_000,
        dropped_message_count=0,
        malformed_event_count=0,
        rejected_event_count=0,
        missed_trade_event_count=0,
        utc_start="2026-07-15T00:00:00+00:00",
        utc_end="2026-07-15T01:00:00+00:00",
        eligible_for_analysis=True,
        eligible_for_order_flow_replay=eligible,
        valid_for_live_decisions=False,
        reasons=(),
    )


def _outcome(idx: int, *, day: str, direction: str, won: bool) -> CompletedRealOutcome:
    net = Decimal("38.00") if won else Decimal("-21.00")
    r = Decimal("2.0000") if won else Decimal("-1.0000")
    ts = 1_752_537_751_000_000_000 + idx * 1_000_000_000
    return CompletedRealOutcome(
        session_id=f"session_{idx}", setup_id=f"s{idx}", provenance="REAL_DELAYED", direction=direction,
        trading_day=day, decision_ts_ns=ts, entry_ts_ns=ts + 1, exit_ts_ns=ts + 2,
        defended_price=Decimal("29450"), entry_reference_price=Decimal("29451"), entry=Decimal("29451.25"),
        stop=Decimal("29441.25"), target=Decimal("29471.25"), exit_reference_price=Decimal("29471.25"),
        exit=Decimal("29471.25") if won else Decimal("29441.25"),
        commission=Decimal("1.24"), slippage_cost=Decimal("0.50"),
        gross_pnl_per_contract=net + Decimal("1.74"), net_pnl_per_contract=net,
        risk_per_contract=Decimal("20.00"), r_multiple=r,
        outcome="target_first" if won else "stop_first", strategy_version="order_flow-v1",
        builder_version="v2", source_event_range=(1, 2), decision_hash="d" * 16, input_hash="i" * 16,
        ordering_mode="receive_order",
    )


def test_empty_state_is_honest_data_collection_stage() -> None:
    """No sessions, no outcomes: data-collection stage, no profit claim, low meter."""
    progress = compute_progress(catalog=[], outcomes=[], ledger=None)
    assert progress.profitable_claim_supported is False
    assert progress.completed_gates == 0
    assert progress.percent == 0
    assert "data-collection" in progress.headline.lower() or "zero" in progress.headline.lower()
    # Every performance gate must read insufficient_evidence, never a fake pass/fail.
    perf = {g.gate_id for g in progress.gates if g.status == STATUS_INSUFFICIENT}
    assert {"net_expectancy", "profit_factor", "max_drawdown", "prop_compliance"} <= perf


def test_zero_completed_setups_blocks_sample_gate_but_capture_can_pass() -> None:
    """With clean eligible sessions but no completed setups, the sample gate blocks."""
    catalog = [_session(i) for i in range(25)]
    progress = compute_progress(catalog=catalog, outcomes=[], ledger=None)
    by_id = {g.gate_id: g for g in progress.gates}
    assert by_id["data_capture"].status == STATUS_PASSED
    assert by_id["session_coverage"].status == STATUS_PASSED
    assert by_id["completed_sample"].status == STATUS_BLOCKED
    assert progress.profitable_claim_supported is False
    # Meter reflects only the two passed prefix gates, not the whole ladder.
    assert progress.completed_gates == 2


def test_insufficient_sample_marks_performance_gates_insufficient() -> None:
    """A few completed setups (< threshold) cannot be judged for performance."""
    catalog = [_session(i) for i in range(25)]
    outcomes = [_outcome(i, day=f"2026-07-{10 + i:02d}", direction="long" if i % 2 else "short", won=True) for i in range(5)]
    progress = compute_progress(catalog=catalog, outcomes=outcomes, ledger=run_real_paper_ledger(outcomes))
    by_id = {g.gate_id: g for g in progress.gates}
    assert by_id["completed_sample"].status != STATUS_PASSED  # 5 < 100
    assert by_id["net_expectancy"].status == STATUS_INSUFFICIENT


def test_full_evidence_ladder_passes_and_supports_claim() -> None:
    """A complete, positive, well-spread sample passes every gate and supports the claim."""
    catalog = [_session(i) for i in range(30)]
    outcomes = []
    for i in range(120):
        day = f"2026-07-{1 + (i % 24):02d}"  # 24 distinct days
        direction = "long" if i % 2 == 0 else "short"  # balanced sides
        won = (i % 5) != 0  # ~80% win rate -> strong positive expectancy
        outcomes.append(_outcome(i, day=day, direction=direction, won=won))
    ledger = run_real_paper_ledger(outcomes)
    cfg = ProgressConfig(min_eligible_sessions=20, min_completed_setups=100, min_independent_days=20)
    progress = compute_progress(catalog=catalog, outcomes=outcomes, ledger=ledger)
    failed = [(g.gate_id, g.status, g.observed) for g in progress.gates if not g.passed]
    assert progress.profitable_claim_supported is True, f"unexpected failed gates: {failed}"
    assert progress.percent == 100
    assert progress.completed_gates == progress.total_gates


def test_meter_caps_at_first_unmet_gate() -> None:
    """The fraction counts only consecutive passed gates from the start."""
    # Sessions pass gates 1-2, but zero outcomes block gate 3 -> meter stops at 2.
    catalog = [_session(i) for i in range(25)]
    progress = compute_progress(catalog=catalog, outcomes=[], ledger=None)
    assert 0 < progress.fraction < Decimal("0.30")
    assert progress.stage_label.startswith("Stage 3")

"""Order-flow traceability (correction #9): every condition is explainable, no lookahead."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.research.episode_builder import build_episodes

# Order-flow capabilities the strategy must represent, each mapped to a named,
# derived condition or context field (no visual Bookmap bubbles/heatmap claimed).
REQUIRED_CAPABILITIES = {
    "persistent_blocks": "durable_defending_block",
    "block_hold": "defending_block_holds",
    "block_stability": "defending_block_stable",
    "controlling_side": "controlling_side_known",
    "cvd_direction": "cvd_supports_direction",
    "aggressive_bubbles": "market_bubbles_support_direction",
    "absorption": "absorption_confirmed",
    "reload_iceberg": "reload_confirmed",
    "opening_observation": "opening_observation_complete",
    "valid_stop": "valid_stop_location",
}


def _fixture(tmp_path: Path) -> Path:
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")
    for i in range(300):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 1_000_000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 50 + 1)})
        rec.record({"timestamp_ns": base + i * 1_000_000 + 1, "price": f"{price:.2f}", "size": "2",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)
    return rec.session_dir


def test_every_decision_records_pass_fail_evidence_and_provenance(tmp_path: Path) -> None:
    """Each evaluated setup logs per-condition pass/fail, a message, timestamps, provenance."""
    result = build_episodes(_fixture(tmp_path), session_id="s", provenance="REAL_DELAYED")
    assert result.decisions, "the builder must log every evaluated setup"
    for decision in result.decisions:
        assert decision.checks, "each decision records per-condition pass/fail"
        # Every failing condition carries an explanatory message (observed vs threshold).
        for name, passed in decision.checks.items():
            if not passed:
                assert name in decision.condition_messages and decision.condition_messages[name]
        assert decision.provenance == "REAL_DELAYED"
        assert decision.ordering_mode  # source provenance of ordering is recorded
        start, end = decision.source_event_range
        assert start <= end


def test_no_lookahead_evidence_ends_at_decision(tmp_path: Path) -> None:
    """Evidence for a decision never extends past the decision event index (no lookahead)."""
    result = build_episodes(_fixture(tmp_path), session_id="s", provenance="REAL_DELAYED")
    for decision in result.decisions:
        _, end = decision.source_event_range
        assert end <= decision.decision_event_index, "condition evidence must not use future events"


def test_all_required_order_flow_capabilities_are_represented(tmp_path: Path) -> None:
    """Every requested order-flow capability maps to a real named strategy condition."""
    result = build_episodes(_fixture(tmp_path), session_id="s", provenance="REAL_DELAYED")
    seen_conditions: set[str] = set()
    for decision in result.decisions:
        seen_conditions.update(decision.checks.keys())
    seen_conditions.update(result.rejected_condition_tally.keys())
    missing = {cap: cond for cap, cond in REQUIRED_CAPABILITIES.items() if cond not in seen_conditions}
    assert not missing, f"unrepresented order-flow capabilities: {missing}"

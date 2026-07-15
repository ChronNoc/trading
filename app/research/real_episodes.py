"""Strict loader for traceable, completed real Bookmap setup outcomes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_REAL_PROVENANCES = frozenset({"REAL_DELAYED", "REAL_REPLAY", "REAL_REALTIME"})
_COMPLETED_OUTCOMES = frozenset({"target_first", "stop_first", "timeout_exit"})


@dataclass(frozen=True, slots=True)
class CompletedRealOutcome:
    """One quality-gated outcome with lineage back to raw Parquet inputs."""

    session_id: str
    setup_id: str
    provenance: str
    direction: str
    trading_day: str
    decision_ts_ns: int
    entry_ts_ns: int
    exit_ts_ns: int
    defended_price: Decimal
    entry_reference_price: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit_reference_price: Decimal
    exit: Decimal
    commission: Decimal
    slippage_cost: Decimal
    gross_pnl_per_contract: Decimal
    net_pnl_per_contract: Decimal
    risk_per_contract: Decimal
    r_multiple: Decimal
    outcome: str
    strategy_version: str
    builder_version: str
    source_event_range: tuple[int, int]
    decision_hash: str
    input_hash: str
    ordering_mode: str

    @property
    def eligible_for_ledger(self) -> bool:
        """Return whether all structural real-ledger gates remain satisfied."""
        return (
            self.provenance == "REAL_DELAYED"
            and self.outcome in _COMPLETED_OUTCOMES
            and self.direction in {"long", "short"}
        )


def load_completed_real_outcomes(processed_root: Path) -> tuple[CompletedRealOutcome, ...]:
    """Load only v2 episode files whose own provenance gates are all true."""
    if not processed_root.is_dir():
        return ()
    outcomes: list[CompletedRealOutcome] = []
    seen_setup_ids: set[tuple[str, str]] = set()
    for path in sorted(processed_root.glob("*.episodes.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            outcome = _parse(record)
            if outcome is None or not outcome.eligible_for_ledger:
                continue
            identity = (outcome.session_id, outcome.setup_id)
            if identity in seen_setup_ids:
                continue
            seen_setup_ids.add(identity)
            outcomes.append(outcome)
    return tuple(sorted(outcomes, key=lambda item: (item.decision_ts_ns, item.session_id, item.setup_id)))


def _parse(record: dict[str, object]) -> CompletedRealOutcome | None:
    try:
        if record.get("eligible_for_ledger") is not True:
            return None
        if record.get("decision") != "accepted" or record.get("strategy_accepted") is not True:
            return None
        if record.get("ordering_ambiguous") is not False:
            return None
        if record.get("data_quality_ok") is not True:
            return None
        provenance = str(record["provenance"])
        if provenance not in _REAL_PROVENANCES:
            return None
        source_range = record["source_event_range"]
        if not isinstance(source_range, list) or len(source_range) != 2:
            return None
        return CompletedRealOutcome(
            session_id=str(record["session_id"]),
            setup_id=str(record["setup_id"]),
            provenance=provenance,
            direction=str(record["direction"]),
            trading_day=str(record["trading_day"]),
            decision_ts_ns=int(record["decision_ts_ns"]),
            entry_ts_ns=int(record["entry_ts_ns"]),
            exit_ts_ns=int(record["exit_ts_ns"]),
            defended_price=Decimal(str(record["defended_price"])),
            entry_reference_price=Decimal(str(record["entry_reference_price"])),
            entry=Decimal(str(record["entry"])),
            stop=Decimal(str(record["stop"])),
            target=Decimal(str(record["target"])),
            exit_reference_price=Decimal(str(record["exit_reference_price"])),
            exit=Decimal(str(record["exit"])),
            commission=Decimal(str(record["commission"])),
            slippage_cost=Decimal(str(record["slippage_cost"])),
            gross_pnl_per_contract=Decimal(str(record["gross_pnl_per_contract"])),
            net_pnl_per_contract=Decimal(str(record["net_pnl_per_contract"])),
            risk_per_contract=Decimal(str(record["risk_per_contract"])),
            r_multiple=Decimal(str(record["r_multiple"])),
            outcome=str(record["outcome"]),
            strategy_version=str(record["strategy_version"]),
            builder_version=str(record["builder_version"]),
            source_event_range=(int(source_range[0]), int(source_range[1])),
            decision_hash=str(record["decision_hash"]),
            input_hash=str(record["input_hash"]),
            ordering_mode=str(record["ordering_mode"]),
        )
    except (KeyError, ValueError, TypeError):
        return None

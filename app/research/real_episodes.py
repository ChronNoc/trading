"""Load completed, labeled REAL setup outcomes for the paper ledger.

Reads ``data/processed/`` records that were derived by replaying real
recorded Bookmap sessions through the deterministic strategy and labeling
outcomes without lookahead. Structurally refuses synthetic provenance:
synthetic artifacts can never enter real performance metrics.

Until the offline replay/labeler has produced records, this returns an empty
tuple - which is the truthful state, not a bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

_REAL_PROVENANCES = frozenset({"REAL_DELAYED", "REAL_REPLAY", "REAL_REALTIME"})
_ELIGIBLE_OUTCOMES = frozenset({"target_first", "stop_first", "timeout_exit", "session_exit"})
STRATEGY_VERSION_KEY = "strategy_version"


@dataclass(frozen=True, slots=True)
class CompletedRealOutcome:
    """One completed real setup with full lineage back to raw evidence."""

    session_id: str
    setup_id: str
    provenance: str
    direction: str
    trading_day: str
    decision_ts_ns: int
    entry_ts_ns: int
    exit_ts_ns: int
    defended_price: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit: Decimal
    r_multiple: Decimal
    outcome: str
    strategy_version: str

    @property
    def eligible_for_ledger(self) -> bool:
        """A completed, non-ambiguous, real outcome may enter the ledger."""
        return (
            self.provenance in _REAL_PROVENANCES
            and self.outcome in _ELIGIBLE_OUTCOMES
            and self.direction in {"long", "short"}
        )


def load_completed_real_outcomes(processed_root: Path) -> tuple[CompletedRealOutcome, ...]:
    """Load and validate completed real outcomes from ``processed_root``.

    Any record whose provenance is not real (e.g. ``SYNTHETIC``) or whose
    outcome is ambiguous/unfinished is rejected - it never reaches the ledger.
    """
    if not processed_root.is_dir():
        return ()
    outcomes: list[CompletedRealOutcome] = []
    for path in sorted(processed_root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            outcome = _parse(record)
            if outcome is not None and outcome.eligible_for_ledger:
                outcomes.append(outcome)
    return tuple(outcomes)


def _parse(record: dict[str, object]) -> CompletedRealOutcome | None:
    try:
        provenance = str(record["provenance"])
        # Hard structural refusal: synthetic never enters real metrics.
        if provenance not in _REAL_PROVENANCES:
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
            entry=Decimal(str(record["entry"])),
            stop=Decimal(str(record["stop"])),
            target=Decimal(str(record["target"])),
            exit=Decimal(str(record["exit"])),
            r_multiple=Decimal(str(record["r_multiple"])),
            outcome=str(record["outcome"]),
            strategy_version=str(record.get(STRATEGY_VERSION_KEY, "unknown")),
        )
    except (KeyError, ValueError, TypeError):
        return None
